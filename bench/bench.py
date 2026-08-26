#!/usr/bin/env python3
"""Open-loop primary benchmark and optional closed-loop stress driver.

The driver uses a stable pool of virtual user IDs. It validates every response,
so an HTTP 200 containing OpenFeature fallback values is not counted as a
successful measurement.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import argparse
import asyncio
import json
import math
import statistics
import time
from collections import Counter
from typing import Any

import httpx

from app.flag_catalog import file_sha256, load_catalog, select_flags, value_digest
from bench.contract import validate_checkout_payload


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(int(len(ordered) * p), len(ordered) - 1)
    return ordered[index]


def rounded(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def planned_request_count(rate: float, duration: float) -> int:
    """Return arrivals in the half-open interval [0, duration)."""
    if rate <= 0:
        raise ValueError("rate must be greater than zero")
    if duration <= 0:
        raise ValueError("duration must be greater than zero")
    return max(1, math.ceil(rate * duration - 1e-12))


class Collector:
    def __init__(self) -> None:
        self.latencies_ms: list[float] = []
        self.server_eval_ms: list[float] = []
        self.launch_lag_ms: list[float] = []
        self.response_bytes: list[int] = []
        self.errors: Counter[str] = Counter()
        self.sent = 0
        self.planned_requests: int | None = None
        self.schedule_duration_s: float | None = None
        self.launch_span_s: float | None = None

    @property
    def ok(self) -> int:
        return len(self.latencies_ms)


async def one_request(
    client: httpx.AsyncClient,
    url: str,
    user: str,
    expected_count: int,
    expected_digest: str,
    coll: Collector,
) -> None:
    started = time.perf_counter()
    try:
        response = await client.get(f"{url}/checkout", headers={"X-User-Id": user})
    except Exception as exc:  # noqa: BLE001
        coll.errors[f"transport:{type(exc).__name__}"] += 1
        return

    elapsed_ms = (time.perf_counter() - started) * 1000
    coll.response_bytes.append(len(response.content))
    try:
        payload = response.json()
    except ValueError:
        coll.errors["response:not-json"] += 1
        return

    contract_errors = validate_checkout_payload(
        payload,
        expected_count=expected_count,
        expected_digest=expected_digest,
    )
    if response.status_code != 200:
        coll.errors[f"http:{response.status_code}"] += 1
    for error in contract_errors:
        coll.errors[f"semantic:{error}"] += 1
    if response.status_code != 200 or contract_errors:
        return

    coll.latencies_ms.append(elapsed_ms)
    eval_ms = payload.get("flag_eval_ms")
    if isinstance(eval_ms, (int, float)):
        coll.server_eval_ms.append(float(eval_ms))


async def open_loop(
    client: httpx.AsyncClient,
    url: str,
    rate: float,
    duration: float,
    users: int,
    expected_count: int,
    expected_digest: str,
    coll: Collector,
) -> None:
    interval = 1.0 / rate
    start = time.perf_counter()
    pending: set[asyncio.Task[None]] = set()
    planned_requests = planned_request_count(rate, duration)
    coll.planned_requests = planned_requests
    first_launch: float | None = None
    last_launch: float | None = None

    def record_completion(completed: asyncio.Task[None]) -> None:
        pending.discard(completed)
        if completed.cancelled():
            coll.errors["internal:cancelled"] += 1
            return
        error = completed.exception()
        if error is not None:
            coll.errors[f"internal:{type(error).__name__}"] += 1

    # Launch every arrival in the configured schedule. If the generator falls
    # behind, requests are launched late rather than silently omitted; launch
    # lag and the actual launch span then cause validation to fail.
    for request_index in range(planned_requests):
        target = start + request_index * interval
        now = time.perf_counter()
        if target > now:
            await asyncio.sleep(target - now)
        launched = time.perf_counter()
        first_launch = launched if first_launch is None else first_launch
        last_launch = launched
        coll.launch_lag_ms.append(max(0.0, (launched - target) * 1000))
        user = f"user-{request_index % users:05d}"
        task = asyncio.create_task(
            one_request(client, url, user, expected_count, expected_digest, coll)
        )
        pending.add(task)
        task.add_done_callback(record_completion)
        coll.sent += 1

    coll.schedule_duration_s = time.perf_counter() - start
    if first_launch is not None and last_launch is not None:
        coll.launch_span_s = max(0.0, last_launch - first_launch)
    if pending:
        await asyncio.gather(*pending)


async def closed_loop(
    client: httpx.AsyncClient,
    url: str,
    concurrency: int,
    duration: float,
    users: int,
    expected_count: int,
    expected_digest: str,
    coll: Collector,
) -> None:
    deadline = time.perf_counter() + duration

    async def worker(worker_index: int) -> None:
        user = f"user-{worker_index % users:05d}"
        while time.perf_counter() < deadline:
            coll.sent += 1
            await one_request(client, url, user, expected_count, expected_digest, coll)

    await asyncio.gather(*(worker(index) for index in range(concurrency)))


async def ping_sampler(
    client: httpx.AsyncClient, url: str, duration: float, out: list[float]
) -> None:
    deadline = time.perf_counter() + duration
    while time.perf_counter() < deadline:
        started = time.perf_counter()
        try:
            response = await client.get(f"{url}/ping")
            if response.status_code == 200:
                out.append((time.perf_counter() - started) * 1000)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.05)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.users < 1:
        raise ValueError("--users must be at least 1")
    if args.duration <= 0:
        raise ValueError("--duration must be greater than zero")
    if args.warmup < 0:
        raise ValueError("--warmup cannot be negative")
    if args.timeout <= 0:
        raise ValueError("--timeout must be greater than zero")
    if args.max_connections < 1:
        raise ValueError("--max-connections must be at least 1")
    if not math.isfinite(args.rate) or args.rate < 0:
        raise ValueError("--rate must be a finite non-negative number")
    if not args.rate and args.concurrency < 1:
        raise ValueError("--concurrency must be at least 1")
    if not args.rate and args.concurrency > args.max_connections:
        raise ValueError("--max-connections must be at least --concurrency")

    limits = httpx.Limits(
        max_connections=args.max_connections,
        max_keepalive_connections=args.max_connections,
    )
    timeout = httpx.Timeout(args.timeout)
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        info_response = await client.get(f"{args.url}/info")
        info_response.raise_for_status()
        info = info_response.json()
        if not isinstance(info, dict) or info.get("schema_version") != 2:
            raise RuntimeError("/info did not return a schema-v2 object")
        app_label = info.get("label")
        if not isinstance(app_label, str) or not app_label:
            raise RuntimeError("/info label is missing or invalid")
        if args.label is not None and args.label != app_label:
            raise RuntimeError(
                f"requested label {args.label!r} does not match app label {app_label!r}"
            )
        result_label = args.label or app_label
        expected_count = int(info["flags_per_request"])
        catalog = load_catalog(args.manifest)
        selected = select_flags(catalog, expected_count)
        selected_keys = [flag.key for flag in selected]
        expected_digest = value_digest(selected)
        local_manifest_hash = file_sha256(args.manifest)

        if info.get("manifest_sha256") != local_manifest_hash:
            raise RuntimeError("/info manifest hash differs from --manifest")
        if info.get("catalog_size") != len(catalog):
            raise RuntimeError("/info catalog size differs from --manifest")
        if info.get("selected_keys") != selected_keys:
            raise RuntimeError("/info selected keys differ from --manifest")
        if info.get("expected_value_digest") != expected_digest:
            raise RuntimeError("/info expected-value digest differs from --manifest")

        if args.warmup > 0:
            warmup = Collector()
            await open_loop(
                client,
                args.url,
                rate=min(args.rate or 5.0, 5.0),
                duration=args.warmup,
                users=args.users,
                expected_count=expected_count,
                expected_digest=expected_digest,
                coll=warmup,
            )
            if warmup.errors:
                raise RuntimeError(
                    f"warmup failed semantic/transport checks: {warmup.errors}"
                )

        await client.post(f"{args.url}/metrics/reset")
        proc_before = (await client.get(f"{args.url}/metrics/proc")).json()

        coll = Collector()
        pings: list[float] = []
        loadgen_cpu_before = time.process_time()
        started = time.perf_counter()
        driver = (
            open_loop(
                client,
                args.url,
                args.rate,
                args.duration,
                args.users,
                expected_count,
                expected_digest,
                coll,
            )
            if args.rate
            else closed_loop(
                client,
                args.url,
                args.concurrency,
                args.duration,
                args.users,
                expected_count,
                expected_digest,
                coll,
            )
        )
        await asyncio.gather(
            driver, ping_sampler(client, args.url, args.duration, pings)
        )
        wall_seconds = time.perf_counter() - started
        loadgen_cpu_seconds = time.process_time() - loadgen_cpu_before

        loop_stats = (await client.get(f"{args.url}/metrics/loop")).json()
        proc_after = (await client.get(f"{args.url}/metrics/proc")).json()

    app_cpu_seconds = proc_after["cpu_seconds"] - proc_before["cpu_seconds"]
    measurement_ok = coll.ok > 0 and not coll.errors
    return {
        "schema_version": 2,
        "measurement_ok": measurement_ok,
        "label": result_label,
        "backend": info.get("backend"),
        "strategy": info.get("strategy"),
        "provider_path": info.get("provider_path"),
        "flipt_environment": info.get("flipt_environment"),
        "flipt_namespace": info.get("flipt_namespace"),
        "driver": "open_loop" if args.rate else "closed_loop",
        "offered_rps": args.rate or None,
        "concurrency": None if args.rate else args.concurrency,
        "virtual_users": args.users,
        "configured_duration_s": args.duration,
        "duration_s": round(wall_seconds, 3),
        "schedule_duration_s": (
            round(coll.schedule_duration_s, 3)
            if coll.schedule_duration_s is not None
            else None
        ),
        "drain_duration_s": (
            round(max(0.0, wall_seconds - args.duration), 3) if args.rate else None
        ),
        "catalog_size": info.get("catalog_size"),
        "flags_per_request": expected_count,
        "manifest_path": str(args.manifest),
        "manifest_sha256": local_manifest_hash,
        "expected_value_digest": expected_digest,
        "deadline_ms": info.get("deadline_ms"),
        "flagd_rpc_cache": info.get("flagd_rpc_cache"),
        "flag_pool_size": info.get("flag_pool_size"),
        "runtime": info.get("runtime"),
        "planned_requests": coll.planned_requests,
        "sent": coll.sent,
        "ok": coll.ok,
        "failed_requests": coll.sent - coll.ok,
        "errors": coll.sent - coll.ok,
        "error_observations": sum(coll.errors.values()),
        "error_kinds": dict(coll.errors),
        "achieved_rps": round(coll.ok / wall_seconds, 3),
        "launch_span_s": (
            round(coll.launch_span_s, 3) if coll.launch_span_s is not None else None
        ),
        "scheduled_rps": (
            round((coll.sent - 1) / coll.launch_span_s, 3)
            if coll.sent > 1 and coll.launch_span_s
            else None
        ),
        "p50_ms": rounded(percentile(coll.latencies_ms, 0.50)),
        "p95_ms": rounded(percentile(coll.latencies_ms, 0.95)),
        "p99_ms": rounded(percentile(coll.latencies_ms, 0.99)),
        "max_ms": rounded(max(coll.latencies_ms) if coll.latencies_ms else None),
        "mean_ms": rounded(
            statistics.fmean(coll.latencies_ms) if coll.latencies_ms else None
        ),
        "server_eval_p50_ms": rounded(percentile(coll.server_eval_ms, 0.50)),
        "server_eval_p95_ms": rounded(percentile(coll.server_eval_ms, 0.95)),
        "server_eval_p99_ms": rounded(percentile(coll.server_eval_ms, 0.99)),
        "ping_samples": len(pings),
        "ping_p50_ms": rounded(percentile(pings, 0.50)),
        "ping_p99_ms": rounded(percentile(pings, 0.99)),
        "open_loop_launch_lag_p50_ms": rounded(percentile(coll.launch_lag_ms, 0.50)),
        "open_loop_launch_lag_p99_ms": rounded(percentile(coll.launch_lag_ms, 0.99)),
        "loop_lag_p50_ms": loop_stats.get("p50_ms"),
        "loop_lag_p99_ms": loop_stats.get("p99_ms"),
        "loop_lag_max_ms": loop_stats.get("max_ms"),
        "stalls_over_50ms": loop_stats.get("over_50ms"),
        "app_cpu_ms_per_success": (
            round(app_cpu_seconds * 1000 / coll.ok, 4) if coll.ok else None
        ),
        "app_cpu_pct_of_one_core": round(app_cpu_seconds / wall_seconds * 100, 2),
        "loadgen_cpu_pct_of_one_core": round(
            loadgen_cpu_seconds / wall_seconds * 100, 2
        ),
        "rss_mib": proc_after.get("rss_mib"),
        "rss_kind": proc_after.get("rss_kind"),
        "threads": proc_after.get("threads"),
        "thread_kind": proc_after.get("thread_kind"),
        "response_bytes_mean": rounded(
            statistics.fmean(coll.response_bytes) if coll.response_bytes else None
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument(
        "--manifest", type=Path, default=REPO_ROOT / "flags" / "manifest.json"
    )
    parser.add_argument(
        "--label",
        default=None,
        help="configuration label; when supplied it must match the app's /info label",
    )
    parser.add_argument(
        "--rate", type=float, default=0, help="open-loop offered requests/sec"
    )
    parser.add_argument(
        "--concurrency", type=int, default=20, help="closed-loop client count"
    )
    parser.add_argument("--users", type=int, default=5000)
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--warmup", type=float, default=3.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-connections", type=int, default=2000)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    result = asyncio.run(run(args))
    print(json.dumps(result, indent=2))
    if args.json_out:
        with open(args.json_out, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(result, sort_keys=True) + "\n")
    if not result["measurement_ok"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

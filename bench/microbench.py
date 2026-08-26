#!/usr/bin/env python3
"""Supporting microbenchmark for sequential batches of distinct flags.

The web benchmark is primary. This script isolates provider evaluation cost by
measuring 1, 3, 10, 25, and 50 distinct boolean evaluations in one process.
Provider order rotates across rounds to reduce fixed-order bias.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import argparse
import json
import statistics
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from app.evaluation_context import build_evaluation_context
from app.flag_catalog import (
    FlagSpec,
    file_sha256,
    load_catalog,
    select_flags,
    value_digest,
)

ProviderFactory = Callable[[], Any]


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(int(len(ordered) * p), len(ordered) - 1)]


def evaluate_batch(client: Any, flags: list[FlagSpec], user: str) -> None:
    ctx = build_evaluation_context(user)
    for flag in flags:
        details = client.get_boolean_details(flag.key, flag.default, ctx)
        if details.error_code is not None:
            raise RuntimeError(
                f"{flag.key}: OpenFeature error {details.error_code}: "
                f"{details.error_message}"
            )
        if details.value is not flag.expected:
            raise RuntimeError(
                f"{flag.key}: value {details.value!r}, expected {flag.expected!r}"
            )


def run_round(
    name: str,
    factory: ProviderFactory,
    catalog: list[FlagSpec],
    counts: list[int],
    batches: int,
    warmup_batches: int,
    round_index: int,
    samples: dict[tuple[str, int], dict[str, list[float]]],
) -> None:
    from openfeature import api

    api.clear_providers()
    api.set_provider_and_wait(factory())
    client = api.get_client()
    try:
        for count in counts:
            flags = select_flags(catalog, count)
            for warmup_index in range(warmup_batches):
                evaluate_batch(
                    client, flags, f"warm-{round_index}-{count}-{warmup_index}"
                )

            for batch_index in range(batches):
                wall_start = time.perf_counter()
                cpu_start = time.process_time()
                evaluate_batch(
                    client, flags, f"user-{round_index}-{count}-{batch_index}"
                )
                wall_ms = (time.perf_counter() - wall_start) * 1000
                cpu_ms = (time.process_time() - cpu_start) * 1000
                samples[(name, count)]["wall_ms"].append(wall_ms)
                samples[(name, count)]["cpu_ms"].append(cpu_ms)
    finally:
        api.shutdown()


def summarize(
    samples: dict[tuple[str, int], dict[str, list[float]]],
    rounds: int,
    catalog: list[FlagSpec],
    manifest_sha256: str,
    flipt_environment: str,
    flipt_namespace: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for (name, count), sample in sorted(samples.items()):
        wall = sample["wall_ms"]
        cpu = sample["cpu_ms"]
        results.append(
            {
                "schema_version": 2,
                "provider_path": name,
                "flag_count": count,
                "catalog_size": len(catalog),
                "manifest_sha256": manifest_sha256,
                "expected_value_digest": value_digest(select_flags(catalog, count)),
                "flipt_environment": (
                    flipt_environment if name == "flipt_ofrep_http" else None
                ),
                "flipt_namespace": (
                    flipt_namespace if name == "flipt_ofrep_http" else None
                ),
                "flagd_rpc_cache": ("disabled" if name == "flagd_rpc_grpc" else None),
                "rounds": rounds,
                "batches_per_round": len(wall) // rounds,
                "samples": len(wall),
                "evaluations": len(wall) * count,
                "cpu_scope": "process-wide CPU across all threads",
                "batch_wall_p50_ms": round(percentile(wall, 0.50), 4),
                "batch_wall_p95_ms": round(percentile(wall, 0.95), 4),
                "batch_wall_p99_ms": round(percentile(wall, 0.99), 4),
                "batch_wall_mean_ms": round(statistics.fmean(wall), 4),
                "batch_cpu_mean_ms": round(statistics.fmean(cpu), 4),
                "wall_mean_ms_per_evaluation": round(statistics.fmean(wall) / count, 6),
                "cpu_mean_ms_per_evaluation": round(statistics.fmean(cpu) / count, 6),
            }
        )
    return results


def parse_counts(raw: str) -> list[int]:
    counts = [int(item.strip()) for item in raw.split(",") if item.strip()]
    if not counts or any(count < 1 for count in counts):
        raise ValueError("--flag-counts must contain positive integers")
    return sorted(set(counts))


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--flipt-url", default="http://localhost:8080")
    parser.add_argument("--flipt-environment", default="default")
    parser.add_argument("--flipt-namespace", default="default")
    parser.add_argument("--flagd-host", default="localhost")
    parser.add_argument("--flagd-port", type=int, default=8013)
    parser.add_argument("--flag-file", default=str(repo_root / "flagd" / "flags.json"))
    parser.add_argument(
        "--manifest", default=str(repo_root / "flags" / "manifest.json")
    )
    parser.add_argument("--deadline-ms", type=int, default=500)
    parser.add_argument("--flag-counts", default="1,3,10,25,50")
    parser.add_argument("--batches", type=int, default=30)
    parser.add_argument("--warmup-batches", type=int, default=3)
    parser.add_argument("--rounds", type=int, default=6)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    from openfeature.contrib.provider.flagd import FlagdProvider
    from openfeature.contrib.provider.flagd.config import CacheType, ResolverType
    from openfeature.contrib.provider.flipt import FliptProvider

    if args.deadline_ms < 1:
        raise ValueError("--deadline-ms must be at least 1")
    if args.batches < 1 or args.warmup_batches < 0 or args.rounds < 1:
        raise ValueError(
            "batches/rounds must be positive and warmup cannot be negative"
        )

    catalog = load_catalog(args.manifest)
    counts = parse_counts(args.flag_counts)
    select_flags(catalog, max(counts))

    providers: list[tuple[str, ProviderFactory]] = [
        (
            "flipt_ofrep_http",
            lambda: FliptProvider(
                base_url=args.flipt_url,
                namespace=args.flipt_namespace,
                headers_factory=lambda: {"X-Flipt-Environment": args.flipt_environment},
                timeout=args.deadline_ms / 1000.0,
            ),
        ),
        (
            "flagd_rpc_grpc",
            lambda: FlagdProvider(
                host=args.flagd_host,
                port=args.flagd_port,
                resolver_type=ResolverType.RPC,
                deadline_ms=args.deadline_ms,
                cache=CacheType.DISABLED,
                retry_grace_period=15,
            ),
        ),
        (
            "flagd_inprocess",
            lambda: FlagdProvider(
                resolver_type=ResolverType.FILE,
                offline_flag_source_path=args.flag_file,
                cache=CacheType.DISABLED,
            ),
        ),
    ]

    samples: dict[tuple[str, int], dict[str, list[float]]] = defaultdict(
        lambda: {"wall_ms": [], "cpu_ms": []}
    )
    for round_index in range(args.rounds):
        offset = round_index % len(providers)
        provider_order = providers[offset:] + providers[:offset]
        count_order = counts if round_index % 2 == 0 else list(reversed(counts))
        for name, factory in provider_order:
            run_round(
                name,
                factory,
                catalog,
                count_order,
                args.batches,
                args.warmup_batches,
                round_index,
                samples,
            )

    results = summarize(
        samples,
        args.rounds,
        catalog,
        file_sha256(args.manifest),
        args.flipt_environment,
        args.flipt_namespace,
    )
    rendered = json.dumps(results, indent=2)
    print(rendered)
    if args.json_out:
        Path(args.json_out).write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

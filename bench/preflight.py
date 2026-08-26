#!/usr/bin/env python3
"""Fail-fast semantic verification before a timed benchmark run."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import argparse
import asyncio
import json
from typing import Any

import httpx

from app.flag_catalog import file_sha256, load_catalog, select_flags, value_digest
from bench.contract import validate_checkout_payload


def validate_info_expectations(
    info: Any,
    *,
    expected_label: str | None = None,
    expected_count: int | None = None,
    expected_pid: int | None = None,
) -> list[str]:
    if not isinstance(info, dict):
        return ["/info JSON is not an object"]

    failures: list[str] = []
    if info.get("schema_version") != 2:
        failures.append("app info schema_version is not 2")
    if expected_label is not None and info.get("label") != expected_label:
        failures.append(f"app label={info.get('label')!r}, expected {expected_label!r}")
    if expected_count is not None and info.get("flags_per_request") != expected_count:
        failures.append(
            "app flags_per_request="
            f"{info.get('flags_per_request')!r}, expected {expected_count}"
        )
    if expected_pid is not None and info.get("pid") != expected_pid:
        failures.append(f"app pid={info.get('pid')!r}, expected {expected_pid}")
    return failures


async def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.samples < 1:
        raise ValueError("--samples must be at least 1")
    if args.timeout <= 0:
        raise ValueError("--timeout must be greater than zero")

    manifest = load_catalog(args.manifest)
    local_hash = file_sha256(args.manifest)

    async with httpx.AsyncClient(timeout=args.timeout) as client:
        info_response = await client.get(f"{args.url}/info")
        info_response.raise_for_status()
        info = info_response.json()

        failures = validate_info_expectations(
            info,
            expected_label=args.expected_label,
            expected_count=args.expected_count,
            expected_pid=args.expected_pid,
        )
        expected_count = int(info["flags_per_request"])
        selected = select_flags(manifest, expected_count)
        expected_keys = [flag.key for flag in selected]
        expected_digest = value_digest(selected)

        if info.get("manifest_sha256") != local_hash:
            failures.append("app manifest hash does not match the local manifest")
        if info.get("catalog_size") != len(manifest):
            failures.append("app catalog size does not match the local manifest")
        if info.get("selected_keys") != expected_keys:
            failures.append("app selected keys do not match the local manifest prefix")
        if len(expected_keys) != len(set(expected_keys)):
            failures.append("selected keys are not distinct")
        if info.get("expected_value_digest") != expected_digest:
            failures.append("app expected-value digest does not match the manifest")

        sample_results: list[dict[str, Any]] = []
        for index in range(args.samples):
            user = f"preflight-user-{index}"
            response = await client.get(
                f"{args.url}/checkout",
                params={"include_details": "true"},
                headers={"X-User-Id": user},
            )
            try:
                payload = response.json()
            except ValueError:
                failures.append(f"{user}: response was not JSON")
                continue

            payload_errors = validate_checkout_payload(
                payload,
                expected_count=expected_count,
                expected_keys=expected_keys,
                expected_digest=expected_digest,
                require_details=True,
            )
            if response.status_code != 200:
                payload_errors.append(f"HTTP status {response.status_code}")
            failures.extend(f"{user}: {error}" for error in payload_errors)
            sample_results.append(
                {
                    "user": user,
                    "status_code": response.status_code,
                    "semantic_ok": payload.get("semantic_ok"),
                    "value_digest": payload.get("value_digest"),
                }
            )

    result = {
        "ok": not failures,
        "url": args.url,
        "label": info.get("label"),
        "provider_path": info.get("provider_path"),
        "catalog_size": len(manifest),
        "flags_per_request": expected_count,
        "manifest_sha256": local_hash,
        "expected_value_digest": expected_digest,
        "samples": sample_results,
        "failures": failures,
    }
    return result


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument(
        "--manifest", type=Path, default=repo_root / "flags" / "manifest.json"
    )
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--expected-label", default=None)
    parser.add_argument("--expected-count", type=int, default=None)
    parser.add_argument("--expected-pid", type=int, default=None)
    args = parser.parse_args()

    result = asyncio.run(run(args))
    print(json.dumps(result, indent=2))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

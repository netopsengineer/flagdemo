#!/usr/bin/env python3
"""Validate schema-v2 completeness, semantics, and generator health."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.flag_catalog import load_catalog, select_flags, value_digest

VALID_BACKENDS = {"flipt", "flagd_rpc", "flagd_inprocess"}
VALID_STRATEGIES = {"direct", "threaded_batch", "threaded_parallel"}


def validate_configuration_identity(row: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    label = row.get("label")
    backend = row.get("backend")
    strategy = row.get("strategy")

    if not isinstance(label, str) or not label:
        failures.append("label is missing or invalid")
    if backend not in VALID_BACKENDS:
        failures.append("backend is missing or invalid")
    if strategy not in VALID_STRATEGIES:
        failures.append("strategy is missing or invalid")
    if backend in VALID_BACKENDS and strategy in VALID_STRATEGIES:
        expected_label = f"{backend}+{strategy}"
        if label != expected_label:
            failures.append(f"label={label!r}, expected {expected_label!r}")
    if not isinstance(row.get("provider_path"), str) or not row.get("provider_path"):
        failures.append("provider_path is missing or invalid")
    return failures


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def split_values(raw: str | None, cast: type = str) -> list[Any]:
    if not raw:
        return []
    return [cast(item) for item in raw.replace(",", " ").split() if item]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument(
        "--manifest", type=Path, default=REPO_ROOT / "flags" / "manifest.json"
    )
    parser.add_argument("--expected-configs", default=None)
    parser.add_argument("--expected-rates", default=None)
    parser.add_argument("--expected-flag-counts", default=None)
    parser.add_argument("--expected-open-reps", type=int, default=None)
    parser.add_argument("--expected-stress-reps", type=int, default=None)
    parser.add_argument("--write", type=Path, default=None)
    args = parser.parse_args()

    rows = [
        json.loads(line)
        for line in args.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    failures: list[str] = []
    warnings: list[str] = []

    if not rows:
        failures.append("result file is empty")

    catalog = load_catalog(args.manifest)
    local_manifest_hash = sha256(args.manifest)
    manifest_hashes = {row.get("manifest_sha256") for row in rows}
    if None in manifest_hashes or len(manifest_hashes) != 1:
        failures.append("manifest hash is missing or inconsistent")
    elif next(iter(manifest_hashes)) != local_manifest_hash:
        failures.append("result manifest hash does not match --manifest")

    digests_by_count: dict[int, set[str]] = {}
    for row in rows:
        count = row.get("flags_per_request")
        digest = row.get("expected_value_digest")
        if isinstance(count, int) and isinstance(digest, str):
            digests_by_count.setdefault(count, set()).add(digest)

    for index, row in enumerate(rows, start=1):
        prefix = f"row {index} ({row.get('label')})"
        failures.extend(
            f"{prefix}: {failure}" for failure in validate_configuration_identity(row)
        )
        if row.get("driver") not in {"open_loop", "closed_loop"}:
            failures.append(f"{prefix}: driver is missing or invalid")
        backend = row.get("backend")
        if backend == "flagd_rpc":
            if row.get("flagd_rpc_cache") not in {"disabled", "lru"}:
                failures.append(f"{prefix}: flagd_rpc_cache is missing or invalid")
        elif row.get("flagd_rpc_cache") is not None:
            failures.append(f"{prefix}: non-RPC row has flagd_rpc_cache metadata")
        if backend == "flipt":
            if not isinstance(row.get("flipt_environment"), str):
                failures.append(f"{prefix}: flipt_environment is missing")
            if not isinstance(row.get("flipt_namespace"), str):
                failures.append(f"{prefix}: flipt_namespace is missing")
        if row.get("schema_version") != 2:
            failures.append(f"{prefix}: schema_version is not 2")
        if row.get("measurement_ok") is not True:
            failures.append(f"{prefix}: measurement_ok is not true")
        if row.get("errors") != 0:
            failures.append(f"{prefix}: errors={row.get('errors')!r}")
        if row.get("errors") == 0 and row.get("error_observations") != 0:
            failures.append(f"{prefix}: error_observations is nonzero")
        if row.get("errors") == 0 and row.get("error_kinds") not in ({}, None):
            failures.append(f"{prefix}: error_kinds is nonempty")
        if row.get("failed_requests") != row.get("errors"):
            failures.append(f"{prefix}: failed_requests does not match errors")
        if row.get("ok") != row.get("sent"):
            failures.append(
                f"{prefix}: ok={row.get('ok')!r} differs from sent={row.get('sent')!r}"
            )
        elif isinstance(row.get("sent"), int) and row.get("failed_requests") != 0:
            failures.append(f"{prefix}: failed_requests is nonzero")
        flag_count = row.get("flags_per_request")
        catalog_size = row.get("catalog_size")
        if not isinstance(flag_count, int) or flag_count < 1:
            failures.append(f"{prefix}: invalid flags_per_request")
        if not isinstance(catalog_size, int) or (
            isinstance(flag_count, int) and catalog_size < flag_count
        ):
            failures.append(f"{prefix}: invalid catalog_size")
        elif catalog_size != len(catalog):
            failures.append(
                f"{prefix}: catalog_size={catalog_size} differs from manifest "
                f"size {len(catalog)}"
            )
        expected_digest = row.get("expected_value_digest")
        if (
            not isinstance(expected_digest, str)
            or len(expected_digest) != 64
            or any(char not in "0123456789abcdef" for char in expected_digest)
        ):
            failures.append(f"{prefix}: invalid expected_value_digest")
        elif isinstance(flag_count, int) and 1 <= flag_count <= len(catalog):
            manifest_digest = value_digest(select_flags(catalog, flag_count))
            if expected_digest != manifest_digest:
                failures.append(
                    f"{prefix}: expected_value_digest does not match the manifest"
                )

        if row.get("driver") == "open_loop":
            offered = row.get("offered_rps")
            scheduled = row.get("scheduled_rps")
            lag_p99 = row.get("open_loop_launch_lag_p99_ms")
            planned = row.get("planned_requests")
            if not isinstance(planned, int) or planned < 1:
                failures.append(f"{prefix}: planned_requests is missing or invalid")
            elif row.get("sent") != planned:
                failures.append(
                    f"{prefix}: sent={row.get('sent')!r} differs from "
                    f"planned_requests={planned}"
                )
            configured_duration = row.get("configured_duration_s")
            if (
                isinstance(planned, int)
                and isinstance(offered, (int, float))
                and offered > 0
                and isinstance(configured_duration, (int, float))
                and configured_duration > 0
            ):
                expected_planned = max(
                    1, math.ceil(offered * configured_duration - 1e-12)
                )
                if planned != expected_planned:
                    failures.append(
                        f"{prefix}: planned_requests={planned} differs from "
                        f"rate/duration schedule {expected_planned}"
                    )
            if not isinstance(offered, (int, float)) or offered <= 0:
                failures.append(f"{prefix}: invalid offered_rps")
            elif (
                isinstance(planned, int)
                and planned > 1
                and not isinstance(scheduled, (int, float))
            ):
                failures.append(f"{prefix}: scheduled_rps is missing")
            elif (
                isinstance(scheduled, (int, float))
                and abs(scheduled - offered) / offered > 0.02
            ):
                failures.append(
                    f"{prefix}: scheduled_rps={scheduled} differs from "
                    f"offered_rps={offered} by more than 2 percent"
                )

            if isinstance(offered, (int, float)) and offered > 0:
                allowed_lag = max(2.0, (1000.0 / offered) * 0.25)
                if not isinstance(lag_p99, (int, float)) or not math.isfinite(
                    float(lag_p99)
                ):
                    failures.append(f"{prefix}: launch lag p99 is missing")
                elif lag_p99 > allowed_lag:
                    failures.append(
                        f"{prefix}: launch lag p99 {lag_p99} ms exceeds "
                        f"{allowed_lag:.3f} ms generator-health limit"
                    )

        loadgen_cpu = row.get("loadgen_cpu_pct_of_one_core")
        if isinstance(loadgen_cpu, (int, float)) and loadgen_cpu > 80:
            warnings.append(f"{prefix}: load generator used {loadgen_cpu}% of one core")
        if row.get("ping_samples", 0) < 1:
            warnings.append(f"{prefix}: no successful ping samples")

    expected_configs = split_values(args.expected_configs)
    expected_rates = split_values(args.expected_rates, float)
    expected_counts = split_values(args.expected_flag_counts, int)

    open_rows = [row for row in rows if row.get("driver") == "open_loop"]
    stress_rows = [row for row in rows if row.get("driver") == "closed_loop"]

    signatures: dict[str, set[tuple[Any, ...]]] = {}
    for row in rows:
        label = row.get("label")
        if not isinstance(label, str):
            continue
        signature = (
            row.get("backend"),
            row.get("strategy"),
            row.get("provider_path"),
            row.get("flipt_environment"),
            row.get("flipt_namespace"),
            row.get("flagd_rpc_cache"),
        )
        signatures.setdefault(label, set()).add(signature)
    for label, observed_signatures in signatures.items():
        if len(observed_signatures) != 1:
            failures.append(
                f"configuration label {label!r} maps to multiple provider settings"
            )

    if expected_configs:
        observed_configs = {
            row.get("label") for row in rows if isinstance(row.get("label"), str)
        }
        if observed_configs != set(expected_configs):
            failures.append(
                f"configuration set differs: observed={sorted(observed_configs)} "
                f"expected={sorted(expected_configs)}"
            )
    if expected_rates:
        observed_rates = {
            float(row["offered_rps"])
            for row in open_rows
            if isinstance(row.get("offered_rps"), (int, float))
        }
        if observed_rates != set(expected_rates):
            failures.append(
                f"open-loop rate set differs: observed={sorted(observed_rates)} "
                f"expected={sorted(expected_rates)}"
            )
    if expected_counts:
        observed_counts = {
            int(row["flags_per_request"])
            for row in rows
            if isinstance(row.get("flags_per_request"), int)
        }
        if observed_counts != set(expected_counts):
            failures.append(
                f"flag-count set differs: observed={sorted(observed_counts)} "
                f"expected={sorted(expected_counts)}"
            )

    if (
        expected_configs
        and expected_rates
        and expected_counts
        and args.expected_open_reps is not None
    ):
        observed = Counter(
            (
                row["label"],
                float(row["offered_rps"]),
                int(row["flags_per_request"]),
            )
            for row in open_rows
            if isinstance(row.get("label"), str)
            and isinstance(row.get("offered_rps"), (int, float))
            and isinstance(row.get("flags_per_request"), int)
        )
        for config in expected_configs:
            for rate in expected_rates:
                for count in expected_counts:
                    actual = observed[(config, rate, count)]
                    if actual != args.expected_open_reps:
                        failures.append(
                            f"open-loop matrix {config}, rate={rate:g}, "
                            f"flags={count}: expected "
                            f"{args.expected_open_reps}, got {actual}"
                        )

    if args.expected_stress_reps is not None and expected_configs and expected_counts:
        observed_stress = Counter(
            (row["label"], int(row["flags_per_request"]))
            for row in stress_rows
            if isinstance(row.get("label"), str)
            and isinstance(row.get("flags_per_request"), int)
        )
        for config in expected_configs:
            for count in expected_counts:
                actual = observed_stress[(config, count)]
                if actual != args.expected_stress_reps:
                    failures.append(
                        f"stress matrix {config}, flags={count}: expected "
                        f"{args.expected_stress_reps}, got {actual}"
                    )
    elif stress_rows:
        warnings.append(
            "stress rows exist but no expected stress repetition count was supplied"
        )

    for count, digests in sorted(digests_by_count.items()):
        if len(digests) != 1:
            failures.append(
                f"flags_per_request={count}: expected-value digest is inconsistent"
            )

    summary = {
        "ok": not failures,
        "path": str(args.path),
        "sha256": sha256(args.path),
        "rows": len(rows),
        "drivers": dict(Counter(row.get("driver") for row in rows)),
        "flag_counts": sorted(
            {
                row.get("flags_per_request")
                for row in rows
                if row.get("flags_per_request") is not None
            }
        ),
        "configurations": sorted(
            {row.get("label") for row in rows if row.get("label") is not None}
        ),
        "manifest": str(args.manifest),
        "manifest_sha256": local_manifest_hash,
        "warnings": warnings,
        "failures": failures,
    }
    rendered = json.dumps(summary, indent=2, sort_keys=True)
    print(rendered)
    if args.write:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(rendered + "\n", encoding="utf-8")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

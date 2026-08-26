#!/usr/bin/env python3
"""Aggregate schema-v2 benchmark rows into Markdown median tables."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

COLUMNS = [
    ("runs", "runs"),
    ("achieved_rps", "achieved rps"),
    ("scheduled_rps", "scheduled rps"),
    ("open_loop_launch_lag_p99_ms", "launch lag p99 ms"),
    ("p50_ms", "p50 ms"),
    ("p95_ms", "p95 ms"),
    ("p99_ms", "p99 ms"),
    ("server_eval_p50_ms", "eval p50 ms"),
    ("ping_p50_ms", "ping p50 ms"),
    ("loop_lag_p99_ms", "loop lag p99 ms"),
    ("stalls_over_50ms", "stalls >50 ms"),
    ("app_cpu_ms_per_success", "app CPU ms/success"),
    ("loadgen_cpu_pct_of_one_core", "loadgen CPU % core"),
    ("errors", "failed requests"),
]


def median(rows: list[dict[str, Any]], key: str) -> Any:
    values = [row[key] for row in rows if row.get(key) is not None]
    if not values:
        return None
    value = statistics.median(values)
    return round(value, 3) if isinstance(value, float) else value


def configuration_label(row: dict[str, Any]) -> str:
    label = str(row["label"])
    if row.get("backend") == "flagd_rpc":
        label += f" [cache={row.get('flagd_rpc_cache')}]"
    return label


def print_group(title: str, groups: dict[str, list[dict[str, Any]]]) -> None:
    print(f"## {title}\n")
    headers = ["configuration"] + [heading for _, heading in COLUMNS]
    print("| " + " | ".join(headers) + " |")
    print("|" + "|".join("---" for _ in headers) + "|")
    for label in sorted(groups):
        rows = groups[label]
        cells = [label]
        for key, _ in COLUMNS:
            cells.append(str(len(rows)) if key == "runs" else str(median(rows, key)))
        print("| " + " | ".join(cells) + " |")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aggregate schema-v2 benchmark rows into Markdown median tables."
    )
    parser.add_argument(
        "path", type=Path, nargs="?", default=Path("results/latest.jsonl")
    )
    args = parser.parse_args()
    path = args.path
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        raise SystemExit("no rows found")

    invalid = [row for row in rows if row.get("measurement_ok") is not True]
    if invalid:
        print(
            f"> WARNING: excluded {len(invalid)} rows that failed measurement "
            "validation. The run-level validator must still pass.\n"
        )
    rows = [row for row in rows if row.get("measurement_ok") is True]

    grouped: dict[
        tuple[str, float | int | None, int],
        dict[str, list[dict[str, Any]]],
    ] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        load = row.get("offered_rps") or row.get("concurrency")
        key = (row["driver"], load, row["flags_per_request"])
        grouped[key][configuration_label(row)].append(row)

    for (driver, load, flag_count), configs in sorted(grouped.items()):
        load_text = (
            f"{load} offered req/s" if driver == "open_loop" else f"concurrency {load}"
        )
        print_group(
            f"{driver.replace('_', ' ').title()}: "
            f"{flag_count} distinct flags/request, {load_text}",
            configs,
        )

    if not rows:
        print("No valid measurement rows were available for aggregation.\n")

    print("## Interpretation rules\n")
    print("- Use repeated open-loop groups for latency and CPU comparisons.")
    print("- Do not compare closed-loop latency across configurations.")
    print("- Treat any error or measurement_ok=false row as invalid evidence.")
    print("- Check generator launch lag and CPU before claiming a server ceiling.")
    print("- App CPU excludes the Flipt and flagd service processes.")


if __name__ == "__main__":
    main()

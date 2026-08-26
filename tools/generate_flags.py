#!/usr/bin/env python3
"""Generate one canonical flag catalog for both Flipt and flagd.

The benchmark intentionally uses distinct, static boolean flags. Every flag is
configured to return True, while the caller supplies False as its default.
That makes a provider fallback observable instead of silently looking valid.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_PREFIX = "checkout-flag"


def build_manifest(count: int, prefix: str = DEFAULT_PREFIX) -> dict[str, Any]:
    if count < 1:
        raise ValueError("count must be at least 1")
    if not prefix or any(char.isspace() for char in prefix):
        raise ValueError("prefix must be non-empty and contain no whitespace")

    flags = [
        {
            "key": f"{prefix}-{index:03d}",
            "type": "boolean",
            "expected": True,
            "default": False,
        }
        for index in range(1, count + 1)
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "catalog_name": "distinct-enabled-booleans",
        "description": (
            "Distinct enabled boolean flags. Each configured value is true and "
            "the caller default is false so fallback results are detectable."
        ),
        "selection": "prefix: each request evaluates the first N distinct keys",
        "flags": flags,
    }


def build_flagd_document(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "$schema": "https://flagd.dev/schema/v0/flags.json",
        "flags": {
            item["key"]: {
                "state": "ENABLED",
                "variants": {"on": True, "off": False},
                "defaultVariant": "on",
            }
            for item in manifest["flags"]
        },
    }


def render_json(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=False) + "\n"


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(content)
        temp_name = handle.name
    os.replace(temp_name, path)


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--prefix", default=DEFAULT_PREFIX)
    parser.add_argument(
        "--manifest", type=Path, default=repo_root / "flags" / "manifest.json"
    )
    parser.add_argument(
        "--flagd", type=Path, default=repo_root / "flagd" / "flags.json"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify that existing files match the generated documents",
    )
    args = parser.parse_args()

    manifest_text = render_json(build_manifest(args.count, args.prefix))
    manifest = json.loads(manifest_text)
    flagd_text = render_json(build_flagd_document(manifest))

    if args.check:
        mismatches: list[str] = []
        for path, expected in (
            (args.manifest, manifest_text),
            (args.flagd, flagd_text),
        ):
            if not path.exists() or path.read_text(encoding="utf-8") != expected:
                mismatches.append(str(path))
        if mismatches:
            raise SystemExit("generated files are stale: " + ", ".join(mismatches))
    else:
        write_atomic(args.manifest, manifest_text)
        write_atomic(args.flagd, flagd_text)

    print(
        json.dumps(
            {
                "count": args.count,
                "manifest": str(args.manifest),
                "manifest_sha256": sha256_text(manifest_text),
                "flagd": str(args.flagd),
                "flagd_sha256": sha256_text(flagd_text),
                "checked": args.check,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

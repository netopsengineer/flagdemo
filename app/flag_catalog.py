"""Load and validate the benchmark's canonical flag catalog."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FlagSpec:
    key: str
    expected: bool
    default: bool


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_catalog(path: str | Path) -> list[FlagSpec]:
    manifest_path = Path(path)
    try:
        document: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"flag manifest not found: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"flag manifest is not valid JSON: {manifest_path}: {exc}"
        ) from exc

    if document.get("schema_version") != 1:
        raise ValueError("flag manifest schema_version must be 1")

    raw_flags = document.get("flags")
    if not isinstance(raw_flags, list) or not raw_flags:
        raise ValueError("flag manifest must contain a non-empty flags list")

    specs: list[FlagSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_flags):
        if not isinstance(item, dict):
            raise ValueError(f"flag entry {index} must be an object")
        if item.get("type") != "boolean":
            raise ValueError(f"flag entry {index} must have type=boolean")

        key = item.get("key")
        expected = item.get("expected")
        default = item.get("default")
        if not isinstance(key, str) or not key:
            raise ValueError(f"flag entry {index} has an invalid key")
        if key in seen:
            raise ValueError(f"duplicate flag key: {key}")
        if not isinstance(expected, bool) or not isinstance(default, bool):
            raise ValueError(f"flag {key} expected/default must be booleans")
        if expected == default:
            raise ValueError(
                f"flag {key} default must oppose expected so fallbacks are detectable"
            )

        seen.add(key)
        specs.append(FlagSpec(key=key, expected=expected, default=default))

    return specs


def value_digest(flags: list[FlagSpec], *, use_expected: bool = True) -> str:
    """Hash the ordered key/value set used by compact benchmark responses."""
    digest_input = "\n".join(
        f"{flag.key}={int(flag.expected if use_expected else flag.default)}"
        for flag in flags
    )
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()


def select_flags(catalog: list[FlagSpec], count: int) -> list[FlagSpec]:
    if count < 1:
        raise ValueError("FLAGS_PER_REQUEST must be at least 1")
    if count > len(catalog):
        raise ValueError(
            f"FLAGS_PER_REQUEST={count} exceeds catalog size {len(catalog)}"
        )
    selected = catalog[:count]
    if len({flag.key for flag in selected}) != count:
        raise ValueError("selected flag keys are not distinct")
    return selected

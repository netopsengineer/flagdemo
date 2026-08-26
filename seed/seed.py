#!/usr/bin/env python3
"""Seed Flipt v2 from the same canonical manifest used by the benchmark app."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

FLIPT_URL = os.getenv("FLIPT_URL", "http://localhost:8080").rstrip("/")
ENVIRONMENT = os.getenv("FLIPT_ENVIRONMENT", "default")
NAMESPACE = os.getenv("FLIPT_NAMESPACE", "default")
MANIFEST_PATH = Path(os.getenv("FLAG_MANIFEST_PATH", "/seed/manifest.json"))
API = f"{FLIPT_URL}/api/v2/environments/{ENVIRONMENT}/namespaces/{NAMESPACE}/resources"
FLAG_TYPE = "flipt.core.Flag"


def call(method: str, url: str, body: dict[str, Any] | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def wait_for_flipt(timeout_seconds: int = 90) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{FLIPT_URL}/health", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    raise SystemExit(
        f"Flipt did not become healthy at {FLIPT_URL} within {timeout_seconds}s"
    )


def put_flag(key: str, expected: bool) -> None:
    payload = {
        "@type": FLAG_TYPE,
        "key": key,
        "name": key,
        "description": (
            "Generated benchmark flag. Configured value opposes the caller default."
        ),
        "type": "BOOLEAN_FLAG_TYPE",
        "enabled": expected,
    }
    body = {"key": key, "payload": payload}
    status, text = call("POST", API, body)
    action = "created"
    if status == 409:
        resource_url = (
            f"{API}/{urllib.parse.quote(FLAG_TYPE, safe='')}"
            f"/{urllib.parse.quote(key, safe='')}"
        )
        get_status, get_text = call("GET", resource_url)
        if get_status >= 300:
            raise SystemExit(
                f"failed to read existing flag/{key}: HTTP {get_status} {get_text}"
            )
        try:
            revision = json.loads(get_text)["revision"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise SystemExit(
                f"existing flag/{key} response did not contain a revision: {get_text}"
            ) from exc
        body["revision"] = revision
        status, text = call("PUT", API, body)
        action = "updated"
    if status >= 300:
        raise SystemExit(f"failed to seed flag/{key}: HTTP {status} {text}")
    print(f"{action} flag/{key}")


def load_manifest() -> dict[str, Any]:
    try:
        document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot load {MANIFEST_PATH}: {exc}") from exc
    if document.get("schema_version") != 1 or not isinstance(
        document.get("flags"), list
    ):
        raise SystemExit("unsupported or invalid flag manifest")
    return document


def main() -> None:
    wait_for_flipt()
    document = load_manifest()
    flags = document["flags"]
    keys: set[str] = set()
    for item in flags:
        key = item.get("key")
        expected = item.get("expected")
        default = item.get("default")
        if not isinstance(key, str) or not isinstance(expected, bool):
            raise SystemExit(f"invalid flag manifest entry: {item!r}")
        if not isinstance(default, bool) or default == expected:
            raise SystemExit(f"flag {key} does not have an opposing caller default")
        if key in keys:
            raise SystemExit(f"duplicate flag key in manifest: {key}")
        keys.add(key)
        put_flag(key, expected)

    digest = hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()
    print(
        json.dumps(
            {
                "seeded": len(flags),
                "environment": ENVIRONMENT,
                "namespace": NAMESPACE,
                "manifest_sha256": digest,
            }
        )
    )


if __name__ == "__main__":
    main()

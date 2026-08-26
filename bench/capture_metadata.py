#!/usr/bin/env python3
"""Capture reproducibility metadata before a benchmark run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    candidates: list[Path] = []
    for relative in (
        ".github",
        "app",
        "bench",
        "docs",
        "flagd",
        "flags",
        "seed",
        "tests",
        "tools",
    ):
        candidates.extend(
            path for path in (root / relative).rglob("*") if path.is_file()
        )
    candidates.extend(
        root / name
        for name in (
            ".dockerignore",
            ".gitignore",
            ".python-version",
            "AGENTS.md",
            "README.md",
            "docker-compose.yml",
            "prepare.sh",
            "requirements-dev.txt",
            "ruff.toml",
            "run_experiment.sh",
        )
        if (root / name).is_file()
    )
    for path in sorted(candidates, key=lambda item: str(item.relative_to(root))):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = str(path.relative_to(root)).encode("utf-8")
        digest.update(relative + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def command_output(command: list[str]) -> dict[str, Any]:
    if shutil.which(command[0]) is None:
        return {"available": False, "command": command}
    try:
        process = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {"available": True, "command": command, "error": str(exc)}
    return {
        "available": True,
        "command": command,
        "returncode": process.returncode,
        "stdout": process.stdout.strip(),
        "stderr": process.stderr.strip(),
    }


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--manifest", type=Path, default=repo_root / "flags" / "manifest.json"
    )
    parser.add_argument(
        "--flagd", type=Path, default=repo_root / "flagd" / "flags.json"
    )
    args = parser.parse_args()

    compose_images = command_output(
        [
            "docker",
            "compose",
            "-f",
            str(repo_root / "docker-compose.yml"),
            "config",
            "--images",
        ]
    )
    image_inspect: dict[str, Any] = {}
    if compose_images.get("returncode") == 0:
        for image in compose_images.get("stdout", "").splitlines():
            if image:
                image_inspect[image] = command_output(
                    [
                        "docker",
                        "image",
                        "inspect",
                        "--format",
                        "{{json .RepoDigests}} {{.Id}}",
                        image,
                    ]
                )

    benchmark_env_names = (
        "FLAG_CATALOG_SIZE",
        "FLAG_COUNTS",
        "RATES",
        "BACKENDS",
        "STRATEGIES",
        "REPS",
        "DURATION",
        "WARMUP",
        "VIRTUAL_USERS",
        "MAX_CONNECTIONS",
        "FLAG_POOL_SIZE",
        "FLAG_DEADLINE_MS",
        "FLAGD_RPC_CACHE",
        "FLIPT_ENVIRONMENT",
        "FLIPT_NAMESPACE",
        "RUN_MICRO",
        "MICRO_FLAG_COUNTS",
        "MICRO_ROUNDS",
        "RUN_STRESS",
        "STRESS_CONCURRENCY",
        "STRESS_DURATION",
        "STRESS_REPS",
    )

    document = {
        "schema_version": 1,
        "captured_utc": datetime.now(UTC).isoformat(),
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "uname": list(platform.uname()),
        "cpu_count": os.cpu_count(),
        "active_source_tree_sha256": tree_sha256(repo_root),
        "benchmark_environment": {
            name: os.environ.get(name) for name in benchmark_env_names
        },
        "manifest": {
            "path": str(args.manifest),
            "sha256": file_sha256(args.manifest),
        },
        "flagd_file": {
            "path": str(args.flagd),
            "sha256": file_sha256(args.flagd),
        },
        "lscpu": command_output(["lscpu"]),
        "memory": command_output(["free", "-h"]),
        "docker_version": command_output(["docker", "version"]),
        "compose_images": compose_images,
        "resolved_image_digests": image_inspect,
        "pip_freeze": command_output([sys.executable, "-m", "pip", "freeze"]),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()

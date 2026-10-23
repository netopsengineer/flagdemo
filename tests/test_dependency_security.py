"""Keep runtime security updates and their CI audit observable and fail closed."""

from importlib.metadata import version
from pathlib import Path

import yaml
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]


def locked_versions() -> dict[str, str]:
    return {
        line.split("==", 1)[0]: line.split("==", 1)[1]
        for line in (ROOT / "app/requirements.lock").read_text().splitlines()
        if "==" in line and not line.lstrip().startswith("#")
    }


def test_fixed_runtime_dependencies_are_locked_and_installed() -> None:
    pins = locked_versions()
    for package, minimum in {"certifi": "2026.10.19", "urllib3": "2.8.0"}.items():
        assert Version(pins[package]) >= Version(minimum)
        assert version(package) == pins[package]


def test_runtime_audit_fails_closed_before_integration() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/verify.yml").read_text())
    job = workflow["jobs"]["test"]
    assert not job.get("continue-on-error", False)
    steps = job["steps"]
    audit = next(
        s for s in steps if s.get("name") == "Audit locked runtime dependencies"
    )
    assert "if" not in audit
    assert not audit.get("continue-on-error", False)
    command = audit["run"]
    assert "pip-audit==2.10.1" in command
    assert (
        "-m pip_audit --strict --no-deps --disable-pip -r app/requirements.lock"
        in command
    )
    assert "--ignore-vuln" not in command
    assert "||" not in command
    assert steps.index(audit) < next(
        i for i, s in enumerate(steps) if s.get("name") == "Prepare pinned providers"
    )


def test_application_image_installs_the_audited_lock() -> None:
    dockerfile = (ROOT / "app/Dockerfile").read_text()
    assert "COPY app/requirements.lock /app/app/requirements.lock" in dockerfile
    assert "pip install --no-cache-dir -r /app/app/requirements.lock" in dockerfile

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]

from app.config import Backend, Settings
from app.evaluation_context import build_evaluation_context
from app.flag_catalog import load_catalog, select_flags, value_digest
from bench.bench import planned_request_count
from bench.capture_metadata import tree_sha256
from bench.contract import validate_checkout_payload
from bench.preflight import validate_info_expectations
from bench.validate_results import validate_configuration_identity
from seed import seed
from tools.generate_flags import build_flagd_document, build_manifest, render_json


def digest_for(values: list[tuple[str, bool]]) -> str:
    raw = "\n".join(f"{key}={int(value)}" for key, value in values)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class CatalogTests(unittest.TestCase):
    def test_generated_catalog_has_distinct_opposing_defaults(self) -> None:
        manifest = build_manifest(50)
        flags = manifest["flags"]
        self.assertEqual(len(flags), 50)
        self.assertEqual(len({item["key"] for item in flags}), 50)
        self.assertTrue(all(item["expected"] is True for item in flags))
        self.assertTrue(all(item["default"] is False for item in flags))

    def test_flagd_keys_match_manifest(self) -> None:
        manifest = build_manifest(50)
        flagd = build_flagd_document(manifest)
        self.assertEqual(
            list(flagd["flags"]), [item["key"] for item in manifest["flags"]]
        )
        self.assertTrue(
            all(item["defaultVariant"] == "on" for item in flagd["flags"].values())
        )

    def test_load_and_select_rejects_repetition(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "manifest.json"
            path.write_text(render_json(build_manifest(50)), encoding="utf-8")
            catalog = load_catalog(path)
            selected = select_flags(catalog, 50)
            self.assertEqual(len(selected), 50)
            self.assertEqual(len({flag.key for flag in selected}), 50)
            self.assertEqual(
                value_digest(selected),
                digest_for([(flag.key, True) for flag in selected]),
            )
            with self.assertRaises(ValueError):
                select_flags(catalog, 51)


class ConfigurationTests(unittest.TestCase):
    def test_shared_context_is_accepted_by_flipt_ofrep(self) -> None:
        context = build_evaluation_context("user-00005")
        self.assertEqual(context.targeting_key, "user-00005")
        self.assertEqual(context.attributes["plan"], "pro")
        self.assertTrue(
            all(isinstance(value, str) for value in context.attributes.values())
        )

    def test_flipt_environment_and_cache_are_explicit(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FLAG_BACKEND": "flipt",
                "FLAG_STRATEGY": "threaded_batch",
                "FLIPT_ENVIRONMENT": "production",
                "FLIPT_NAMESPACE": "checkout",
                "FLAGD_RPC_CACHE": "disabled",
            },
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertIs(settings.backend, Backend.FLIPT)
        self.assertEqual(settings.flipt_environment, "production")
        self.assertEqual(settings.flipt_namespace, "checkout")
        self.assertEqual(settings.flagd_rpc_cache, "disabled")

    def test_invalid_cache_mode_is_rejected(self) -> None:
        with (
            patch.dict(os.environ, {"FLAGD_RPC_CACHE": "unknown"}, clear=True),
            self.assertRaises(ValueError),
        ):
            Settings.from_env()


class ContractTests(unittest.TestCase):
    def test_valid_payload_passes(self) -> None:
        values = [
            ("checkout-flag-001", True),
            ("checkout-flag-002", True),
        ]
        digest = digest_for(values)
        payload = {
            "evaluated_flags": 2,
            "distinct_flags": 2,
            "true_count": 2,
            "evaluation_error_count": 0,
            "value_mismatch_count": 0,
            "semantic_ok": True,
            "value_digest": digest,
            "details": [
                {
                    "key": "checkout-flag-001",
                    "value": True,
                    "expected": True,
                    "default": False,
                    "error_code": None,
                },
                {
                    "key": "checkout-flag-002",
                    "value": True,
                    "expected": True,
                    "default": False,
                    "error_code": None,
                },
            ],
        }
        self.assertEqual(
            validate_checkout_payload(
                payload,
                expected_count=2,
                expected_keys=["checkout-flag-001", "checkout-flag-002"],
                expected_digest=digest,
                require_details=True,
            ),
            [],
        )

    def test_fallback_value_fails(self) -> None:
        payload = {
            "evaluated_flags": 1,
            "distinct_flags": 1,
            "true_count": 0,
            "evaluation_error_count": 1,
            "value_mismatch_count": 1,
            "semantic_ok": False,
            "value_digest": digest_for([("checkout-flag-001", False)]),
            "details": [
                {
                    "key": "checkout-flag-001",
                    "value": False,
                    "expected": True,
                    "default": False,
                    "error_code": "GENERAL",
                }
            ],
        }
        errors = validate_checkout_payload(
            payload,
            expected_count=1,
            expected_keys=["checkout-flag-001"],
            expected_digest=digest_for([("checkout-flag-001", True)]),
            require_details=True,
        )
        self.assertGreaterEqual(len(errors), 5)


class ConfigurationIdentityTests(unittest.TestCase):
    def test_result_label_must_match_backend_and_strategy(self) -> None:
        row = {
            "label": "flipt+direct",
            "backend": "flagd_inprocess",
            "strategy": "direct",
            "provider_path": "file-backed",
        }
        errors = validate_configuration_identity(row)
        self.assertTrue(
            any("expected 'flagd_inprocess+direct'" in error for error in errors)
        )

    def test_preflight_can_pin_the_expected_process(self) -> None:
        info = {
            "schema_version": 2,
            "label": "flagd_inprocess+direct",
            "flags_per_request": 50,
            "pid": 100,
        }
        self.assertEqual(
            validate_info_expectations(
                info,
                expected_label="flagd_inprocess+direct",
                expected_count=50,
                expected_pid=100,
            ),
            [],
        )
        errors = validate_info_expectations(info, expected_pid=101)
        self.assertEqual(errors, ["app pid=100, expected 101"])


class MetadataTests(unittest.TestCase):
    def test_source_hash_tolerates_absent_optional_root_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            digest = tree_sha256(Path(temp_dir))
        self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_source_hash_includes_agent_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            instructions = root / "AGENTS.md"
            instructions.write_text("first contract\n", encoding="utf-8")
            first_digest = tree_sha256(root)
            instructions.write_text("second contract\n", encoding="utf-8")
            self.assertNotEqual(first_digest, tree_sha256(root))


class DependencyManifestTests(unittest.TestCase):
    def test_dev_requirements_do_not_reference_a_nested_lock(self) -> None:
        dev_requirements = (REPO_ROOT / "requirements-dev.txt").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("-r", dev_requirements)

    def test_ci_installs_runtime_lock_and_dev_requirements_explicitly(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/verify.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("-r app/requirements.lock", workflow)
        self.assertIn("-r requirements-dev.txt", workflow)


class SchedulingTests(unittest.TestCase):
    def test_half_open_arrival_count(self) -> None:
        self.assertEqual(planned_request_count(40.0, 15.0), 600)
        self.assertEqual(planned_request_count(0.5, 15.0), 8)
        self.assertEqual(planned_request_count(1.0, 0.1), 1)
        with self.assertRaises(ValueError):
            planned_request_count(0.0, 15.0)
        with self.assertRaises(ValueError):
            planned_request_count(1.0, 0.0)


class SeederTests(unittest.TestCase):
    def test_existing_flipt_flag_is_updated_with_revision(self) -> None:
        calls: list[tuple[str, str, dict | None]] = []

        def fake_call(method: str, url: str, body: dict | None = None):
            calls.append((method, url, body.copy() if body else None))
            if method == "POST":
                return 409, "already exists"
            if method == "GET":
                return 200, json.dumps({"revision": "revision-123"})
            if method == "PUT":
                return 200, "{}"
            raise AssertionError(method)

        with (
            patch.object(seed, "call", side_effect=fake_call),
            patch("builtins.print"),
        ):
            seed.put_flag("checkout-flag-001", True)

        self.assertEqual([method for method, _, _ in calls], ["POST", "GET", "PUT"])
        self.assertEqual(calls[-1][2]["revision"], "revision-123")
        self.assertEqual(
            calls[-1][2]["payload"]["@type"],
            "flipt.core.Flag",
        )


class AppRouteRegistrationTest(unittest.TestCase):
    """Importing app.main must register every route.

    FastAPI validates response-model annotations at route-registration time,
    which happens on import. A bad annotation (for example a union of a dict
    and a Response) raises FastAPIError here, so the app never starts and no
    measurement can run. compileall does not catch this because it compiles
    without importing.
    """

    def test_app_main_imports_and_registers_routes(self) -> None:
        import os
        from unittest.mock import patch

        env = {
            "FLAG_BACKEND": "flagd_inprocess",
            "FLAG_STRATEGY": "direct",
            "FLAGS_PER_REQUEST": "3",
            "FLAG_MANIFEST_PATH": str(REPO_ROOT / "flags" / "manifest.json"),
            "FLAGD_FLAG_FILE": str(REPO_ROOT / "flagd" / "flags.json"),
        }
        with patch.dict(os.environ, env, clear=False):
            for name in [m for m in sys.modules if m.startswith("app.")]:
                del sys.modules[name]
            import app.main as main

            paths = {route.path for route in main.app.routes}

        for expected in ["/checkout", "/info", "/healthz", "/ping"]:
            self.assertIn(expected, paths)


class RunnerMatrixParsingTest(unittest.TestCase):
    """The runner must not scalar-assign to a variable it also uses as an array.

    In bash, `X="a b"` on an existing array writes index 0 only. If the script
    does `read -r -a X` and later `export X="a b"`, element 0 silently becomes
    the whole string, collapsing the first matrix entry. Arrays also cannot be
    exported, so the child environment loses the value entirely.
    """

    MATRIX_VARS = ("FLAG_COUNTS", "RATES", "BACKENDS", "STRATEGIES")

    def test_no_array_is_clobbered_by_scalar_export(self) -> None:
        script = (REPO_ROOT / "run_experiment.sh").read_text(encoding="utf-8")
        for name in self.MATRIX_VARS:
            read_as_array = re.search(
                rf"^read\s+-r\s+-a\s+{name}\b", script, re.MULTILINE
            )
            scalar_export = re.search(rf"^export\s+{name}=", script, re.MULTILINE)
            self.assertFalse(
                read_as_array and scalar_export,
                f"{name} is both read as an array and scalar-exported; "
                "element 0 will be clobbered",
            )

    def test_preflight_is_pinned_to_the_spawned_process(self) -> None:
        script = (REPO_ROOT / "run_experiment.sh").read_text(encoding="utf-8")
        self.assertIn('--expected-label "$backend+$strategy"', script)
        self.assertIn('--expected-count "$flag_count"', script)
        self.assertIn('--expected-pid "$APP_PID"', script)

    def test_runner_prefers_the_repository_virtual_environment(self) -> None:
        script = (REPO_ROOT / "run_experiment.sh").read_text(encoding="utf-8")
        self.assertIn('elif [[ -x "$ROOT/.venv/bin/python" ]]', script)
        self.assertIn('PYTHON_BIN="$ROOT/.venv/bin/python"', script)

    def test_prepare_prefers_the_repository_virtual_environment(self) -> None:
        script = (REPO_ROOT / "prepare.sh").read_text(encoding="utf-8")
        self.assertIn('if [[ -x "$ROOT/.venv/bin/python" ]]', script)
        self.assertIn('PYTHON_BIN="$ROOT/.venv/bin/python"', script)

    def test_prepare_uses_the_configured_flagd_host_endpoint(self) -> None:
        script = (REPO_ROOT / "prepare.sh").read_text(encoding="utf-8")
        self.assertIn('FLAGD_READY_HOST="${FLAGD_HOST:-localhost}"', script)
        self.assertIn('FLAGD_READY_PORT="${FLAGD_PORT:-8013}"', script)
        self.assertIn("socket.create_connection((host, port)", script)

    def test_default_rate_is_safe_for_the_slowest_smoke_path(self) -> None:
        script = (REPO_ROOT / "run_experiment.sh").read_text(encoding="utf-8")
        self.assertIn('RATES_RAW="${RATES:-5}"', script)


if __name__ == "__main__":
    unittest.main()

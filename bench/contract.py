"""Shared validation for benchmark endpoint responses."""

from __future__ import annotations

from typing import Any


def validate_checkout_payload(
    payload: Any,
    *,
    expected_count: int,
    expected_keys: list[str] | None = None,
    expected_digest: str | None = None,
    require_details: bool = False,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["response JSON is not an object"]

    if payload.get("evaluated_flags") != expected_count:
        errors.append(
            f"evaluated_flags={payload.get('evaluated_flags')!r}, expected {expected_count}"
        )
    if payload.get("distinct_flags") != expected_count:
        errors.append(
            f"distinct_flags={payload.get('distinct_flags')!r}, expected {expected_count}"
        )
    if payload.get("evaluation_error_count") != 0:
        errors.append(
            f"evaluation_error_count={payload.get('evaluation_error_count')!r}"
        )
    if payload.get("value_mismatch_count") != 0:
        errors.append(f"value_mismatch_count={payload.get('value_mismatch_count')!r}")
    if payload.get("semantic_ok") is not True:
        errors.append(f"semantic_ok={payload.get('semantic_ok')!r}")
    if payload.get("true_count") != expected_count:
        errors.append(
            f"true_count={payload.get('true_count')!r}, expected {expected_count}"
        )
    digest = payload.get("value_digest")
    if not isinstance(digest, str) or len(digest) != 64:
        errors.append("value_digest is missing or invalid")
    elif expected_digest is not None and digest != expected_digest:
        errors.append("value_digest does not match the selected manifest values")

    if require_details:
        details = payload.get("details")
        if not isinstance(details, list):
            errors.append("details are missing")
            return errors
        if len(details) != expected_count:
            errors.append(f"details count={len(details)}, expected {expected_count}")
            return errors

        keys = [item.get("key") for item in details if isinstance(item, dict)]
        if len(keys) != expected_count:
            errors.append("one or more details are not objects")
        elif len(set(keys)) != expected_count:
            errors.append("detail keys are not distinct")
        if expected_keys is not None and keys != expected_keys:
            errors.append("detail keys do not match the manifest prefix selection")

        for item in details:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            if item.get("expected") is not True:
                errors.append(f"{key}: expected field is not true")
            if item.get("default") is not False:
                errors.append(f"{key}: caller default is not false")
            if item.get("value") is not True:
                errors.append(f"{key}: returned value is not true")
            if item.get("error_code") is not None:
                errors.append(f"{key}: error_code={item.get('error_code')!r}")

    return errors

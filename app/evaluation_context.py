"""Build one provider-compatible evaluation context for every workload path."""

from __future__ import annotations

from openfeature.evaluation_context import EvaluationContext


def build_evaluation_context(user_id: str) -> EvaluationContext:
    """Return the shared context used by HTTP and microbenchmark evaluations.

    The released OFREP provider preserves native attribute types, while Flipt
    v2.11.0 currently accepts a ``map<string, string>`` at its OFREP endpoint.
    Keeping every custom attribute string-valued prevents valid OpenFeature
    attributes such as booleans from being rejected by that provider path.
    """
    return EvaluationContext(
        targeting_key=user_id,
        attributes={
            "plan": "pro" if user_id.endswith(("0", "5")) else "free",
            "benchmark": "true",
        },
    )

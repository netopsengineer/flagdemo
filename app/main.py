"""FastAPI benchmark application for published Python OpenFeature paths.

The application initializes one provider per worker, builds one evaluation
context per request, evaluates N distinct flags, and treats provider fallbacks
as failures. The benchmark endpoint returns a compact semantic summary instead
of all flag values so response-size growth does not dominate the measurement.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import os
import platform
import sys
import threading
import time
from typing import Annotated, Any

try:
    import resource
except ImportError:  # pragma: no cover - Windows fallback
    resource = None  # type: ignore[assignment]

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from openfeature.evaluation_context import EvaluationContext

from app.config import Backend, Settings, Strategy
from app.evaluation_context import build_evaluation_context
from app.flag_client import FlagGateway, init_openfeature, shutdown_openfeature
from app.loop_monitor import LoopMonitor

settings = Settings.from_env()
monitor = LoopMonitor()


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def strategy_description(strategy: Strategy) -> str:
    if strategy is Strategy.DIRECT:
        return "sequential synchronous evaluations on the event-loop thread"
    if strategy is Strategy.THREADED_BATCH:
        return "one sequential evaluation batch offloaded to one pool worker"
    return "one pool job per flag, gathered concurrently; optional fan-out design"


def provider_path_description(backend: Backend) -> str:
    if backend is Backend.FLIPT:
        return "released Flipt Python OpenFeature provider over synchronous OFREP HTTP"
    if backend is Backend.FLAGD_RPC:
        return "released flagd Python OpenFeature provider over gRPC RPC evaluation"
    return (
        "released flagd Python OpenFeature provider with local file-backed evaluation"
    )


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    client = init_openfeature(settings)
    gateway = FlagGateway(settings, client)
    app.state.gateway = gateway
    app.state.started_at = time.time()
    monitor.start()
    try:
        yield
    finally:
        await monitor.stop()
        gateway.shutdown()
        shutdown_openfeature()


app = FastAPI(title="openfeature-provider-path-benchmark", lifespan=lifespan)


def get_gateway(request: Request) -> FlagGateway:
    return request.app.state.gateway


def get_eval_context(
    x_user_id: Annotated[str | None, Header()] = None,
) -> EvaluationContext:
    user_id = x_user_id or "anonymous"
    return build_evaluation_context(user_id)


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/info")
async def info(
    gateway: Annotated[FlagGateway, Depends(get_gateway)],
) -> dict[str, Any]:
    from openfeature import api

    return {
        "schema_version": 2,
        "label": settings.label,
        "backend": settings.backend.value,
        "strategy": settings.strategy.value,
        "strategy_description": strategy_description(settings.strategy),
        "provider_path": provider_path_description(settings.backend),
        "provider": api.get_provider_metadata().name,
        "catalog_size": gateway.catalog_size,
        "flags_per_request": settings.flags_per_request,
        "selected_keys": gateway.selected_keys,
        "selection": "first N keys from the canonical manifest; all keys are distinct",
        "configured_value": True,
        "caller_default": False,
        "flipt_environment": (
            settings.flipt_environment if settings.backend is Backend.FLIPT else None
        ),
        "flipt_namespace": (
            settings.flipt_namespace if settings.backend is Backend.FLIPT else None
        ),
        "manifest_sha256": gateway.manifest_sha256,
        "expected_value_digest": gateway.expected_value_digest,
        "flag_pool_size": (
            settings.flag_pool_size
            if settings.strategy is not Strategy.DIRECT
            else None
        ),
        "deadline_ms": settings.deadline_ms,
        "flagd_rpc_cache": (
            settings.flagd_rpc_cache if settings.backend is Backend.FLAGD_RPC else None
        ),
        "pid": os.getpid(),
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "openfeature-sdk": package_version("openfeature-sdk"),
            "openfeature-provider-flipt": package_version("openfeature-provider-flipt"),
            "openfeature-provider-ofrep": package_version("openfeature-provider-ofrep"),
            "openfeature-provider-flagd": package_version("openfeature-provider-flagd"),
            "fastapi": package_version("fastapi"),
            "uvicorn": package_version("uvicorn"),
        },
    }


def process_memory() -> tuple[float, str]:
    statm = "/proc/self/statm"
    if os.path.exists(statm):
        with open(statm, encoding="utf-8") as handle:
            rss_pages = int(handle.read().split()[1])
        return (
            round(rss_pages * os.sysconf("SC_PAGE_SIZE") / 1048576, 2),
            "current_rss",
        )

    if resource is None:
        return 0.0, "unavailable"

    # ru_maxrss is KiB on Linux/BSD and bytes on macOS. This fallback records
    # peak RSS rather than current RSS, and the response labels that difference.
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1048576 if platform.system() == "Darwin" else 1024
    return round(raw / divisor, 2), "peak_rss_fallback"


@app.get("/metrics/proc")
async def metrics_proc() -> dict[str, float | int | str]:
    rss_mib, rss_kind = process_memory()
    task_dir = "/proc/self/task"
    if os.path.isdir(task_dir):
        threads = len(os.listdir(task_dir))
        thread_kind = "native_threads"
    else:
        threads = threading.active_count()
        thread_kind = "python_threads_fallback"
    return {
        "cpu_seconds": round(time.process_time(), 6),
        "rss_mib": rss_mib,
        "rss_kind": rss_kind,
        "threads": threads,
        "thread_kind": thread_kind,
    }


@app.get("/checkout", response_model=None)
async def checkout(
    gateway: Annotated[FlagGateway, Depends(get_gateway)],
    ctx: Annotated[EvaluationContext, Depends(get_eval_context)],
    include_details: Annotated[bool, Query()] = False,
) -> dict[str, Any] | JSONResponse:
    summary, eval_ms = await gateway.evaluate_checkout(
        ctx, include_details=include_details
    )

    payload: dict[str, Any] = {
        "user": ctx.targeting_key,
        "flag_eval_ms": round(eval_ms, 4),
        **summary,
    }
    if not summary["semantic_ok"]:
        return JSONResponse(status_code=502, content=payload)
    return payload


@app.get("/ping")
async def ping() -> dict[str, float | bool]:
    return {"pong": True, "t": time.time()}


@app.get("/metrics/loop")
async def metrics_loop() -> dict[str, Any]:
    return monitor.stats()


@app.post("/metrics/reset")
async def metrics_reset() -> dict[str, bool]:
    monitor.reset()
    return {"reset": True}

"""Provider wiring, call strategies, and semantic result validation.

The benchmark uses one canonical manifest shared by the app, Flipt seeding,
and flagd. Each selected flag key is distinct. Every configured value is True,
while the OpenFeature caller default is False. A missing flag, provider-not-
ready response, timeout, parse failure, or other fallback therefore becomes a
visible mismatch instead of being counted as a successful request.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from openfeature import api
from openfeature.client import OpenFeatureClient
from openfeature.evaluation_context import EvaluationContext
from openfeature.provider import AbstractProvider

from app.config import Backend, Settings, Strategy
from app.flag_catalog import (
    FlagSpec,
    file_sha256,
    load_catalog,
    select_flags,
    value_digest,
)


def _enum_text(value: Any) -> str | None:
    if value is None:
        return None
    raw = getattr(value, "value", None)
    return str(raw if raw is not None else value)


@dataclass(frozen=True)
class EvaluationRecord:
    key: str
    value: bool
    expected: bool
    default: bool
    error_code: str | None
    error_message: str | None
    reason: str | None
    variant: str | None

    @property
    def mismatch(self) -> bool:
        return self.value is not self.expected

    def public(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "expected": self.expected,
            "default": self.default,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "reason": self.reason,
            "variant": self.variant,
        }


def build_provider(settings: Settings) -> AbstractProvider:
    """Construct the provider for the configured published integration path."""
    if settings.backend is Backend.FLIPT:
        # openfeature-provider-flipt 0.1.3 subclasses the synchronous OFREP
        # provider. Each OpenFeature evaluation performs one HTTP POST.
        from openfeature.contrib.provider.flipt import FliptProvider

        return FliptProvider(
            base_url=settings.flipt_url,
            namespace=settings.flipt_namespace,
            headers_factory=lambda: {"X-Flipt-Environment": settings.flipt_environment},
            timeout=settings.deadline_ms / 1000.0,
        )

    if settings.backend is Backend.FLAGD_RPC:
        from openfeature.contrib.provider.flagd import FlagdProvider
        from openfeature.contrib.provider.flagd.config import CacheType, ResolverType

        cache_type = (
            CacheType.LRU if settings.flagd_rpc_cache == "lru" else CacheType.DISABLED
        )
        return FlagdProvider(
            host=settings.flagd_host,
            port=settings.flagd_port,
            resolver_type=ResolverType.RPC,
            deadline_ms=settings.deadline_ms,
            # The fair default is disabled because Flipt OFREP has no
            # equivalent value cache. Production-default cache behavior can
            # be measured separately with FLAGD_RPC_CACHE=lru.
            cache=cache_type,
            retry_grace_period=15,
        )

    if settings.backend is Backend.FLAGD_INPROCESS:
        from openfeature.contrib.provider.flagd import FlagdProvider
        from openfeature.contrib.provider.flagd.config import CacheType, ResolverType

        return FlagdProvider(
            resolver_type=ResolverType.FILE,
            offline_flag_source_path=settings.flagd_flag_file,
            cache=CacheType.DISABLED,
        )

    raise ValueError(f"unhandled backend: {settings.backend}")


class FlagGateway:
    def __init__(self, settings: Settings, client: OpenFeatureClient):
        self._settings = settings
        self._client = client
        self._catalog = load_catalog(settings.flag_manifest_path)
        self._plan = select_flags(self._catalog, settings.flags_per_request)
        self._executor: ThreadPoolExecutor | None = None

        if settings.strategy in (
            Strategy.THREADED_BATCH,
            Strategy.THREADED_PARALLEL,
        ):
            self._executor = ThreadPoolExecutor(
                max_workers=settings.flag_pool_size,
                thread_name_prefix="flageval",
            )

    @property
    def catalog_size(self) -> int:
        return len(self._catalog)

    @property
    def selected_keys(self) -> list[str]:
        return [flag.key for flag in self._plan]

    @property
    def manifest_sha256(self) -> str:
        return file_sha256(self._settings.flag_manifest_path)

    @property
    def expected_value_digest(self) -> str:
        return value_digest(self._plan)

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)

    def _resolve(self, flag: FlagSpec, ctx: EvaluationContext) -> EvaluationRecord:
        details = self._client.get_boolean_details(flag.key, flag.default, ctx)
        return EvaluationRecord(
            key=flag.key,
            value=details.value,
            expected=flag.expected,
            default=flag.default,
            error_code=_enum_text(details.error_code),
            error_message=details.error_message,
            reason=_enum_text(details.reason),
            variant=details.variant,
        )

    def _evaluate_sequential(self, ctx: EvaluationContext) -> list[EvaluationRecord]:
        return [self._resolve(flag, ctx) for flag in self._plan]

    async def _evaluate_threaded_batch(
        self, ctx: EvaluationContext
    ) -> list[EvaluationRecord]:
        """Move one unchanged sequential evaluation batch to one worker thread.

        This isolates the effect of protecting the event loop. It does not add
        per-request provider concurrency or change the order/number of calls.
        """
        loop = asyncio.get_running_loop()
        executor = self._executor
        if executor is None:
            raise RuntimeError("threaded_batch requires an executor")
        return await loop.run_in_executor(
            executor,
            functools.partial(self._evaluate_sequential, ctx),
        )

    async def _evaluate_threaded_parallel(
        self, ctx: EvaluationContext
    ) -> list[EvaluationRecord]:
        """Optional fan-out experiment, not part of the default fair matrix.

        This issues one executor job per flag. It may reduce request latency but
        also changes concurrency, increases pressure on the remote service, and
        concurrently uses the provider instance. Treat it as a separate design.
        """
        loop = asyncio.get_running_loop()
        executor = self._executor
        if executor is None:
            raise RuntimeError("threaded_parallel requires an executor")
        futures = [
            loop.run_in_executor(
                executor,
                functools.partial(self._resolve, flag, ctx),
            )
            for flag in self._plan
        ]
        return list(await asyncio.gather(*futures))

    @staticmethod
    def _summarize(
        records: list[EvaluationRecord], include_details: bool
    ) -> dict[str, Any]:
        distinct_count = len({record.key for record in records})
        error_count = sum(record.error_code is not None for record in records)
        mismatch_count = sum(record.mismatch for record in records)
        digest_input = "\n".join(
            f"{record.key}={int(record.value)}" for record in records
        )
        summary: dict[str, Any] = {
            "evaluated_flags": len(records),
            "distinct_flags": distinct_count,
            "true_count": sum(record.value for record in records),
            "evaluation_error_count": error_count,
            "value_mismatch_count": mismatch_count,
            "semantic_ok": (
                distinct_count == len(records)
                and error_count == 0
                and mismatch_count == 0
            ),
            "value_digest": hashlib.sha256(digest_input.encode("utf-8")).hexdigest(),
        }
        if include_details:
            summary["details"] = [record.public() for record in records]
        return summary

    async def evaluate_checkout(
        self, ctx: EvaluationContext, *, include_details: bool = False
    ) -> tuple[dict[str, Any], float]:
        """Evaluate the configured distinct flags and validate every result."""
        started = time.perf_counter()
        if self._settings.strategy is Strategy.DIRECT:
            records = self._evaluate_sequential(ctx)
        elif self._settings.strategy is Strategy.THREADED_BATCH:
            records = await self._evaluate_threaded_batch(ctx)
        else:
            records = await self._evaluate_threaded_parallel(ctx)
        elapsed_ms = (time.perf_counter() - started) * 1000
        return self._summarize(records, include_details), elapsed_ms


def init_openfeature(settings: Settings) -> OpenFeatureClient:
    """Register the provider and wait until it reports ready."""
    api.set_provider_and_wait(build_provider(settings))
    return api.get_client()


def shutdown_openfeature() -> None:
    api.shutdown()

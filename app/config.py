"""Configuration for the benchmark application.

Backend and application call strategy are independent axes:

    backend  = flipt | flagd_rpc | flagd_inprocess
    strategy = direct | threaded_batch | threaded_parallel

The primary comparison should use direct and threaded_batch.
threaded_parallel is an optional fan-out experiment. It changes per-request
provider concurrency in addition to moving synchronous work off the event loop.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum


class Backend(StrEnum):
    """Which published OpenFeature provider path performs evaluation."""

    FLIPT = "flipt"
    FLAGD_RPC = "flagd_rpc"
    FLAGD_INPROCESS = "flagd_inprocess"


class Strategy(StrEnum):
    """How the FastAPI coroutine invokes the synchronous provider."""

    DIRECT = "direct"
    THREADED_BATCH = "threaded_batch"
    THREADED_PARALLEL = "threaded_parallel"


@dataclass(frozen=True)
class Settings:
    backend: Backend
    strategy: Strategy

    flipt_url: str
    flipt_environment: str
    flipt_namespace: str

    flagd_host: str
    flagd_port: int
    flagd_flag_file: str
    flagd_rpc_cache: str

    flag_manifest_path: str
    flags_per_request: int

    deadline_ms: int
    flag_pool_size: int

    @staticmethod
    def from_env() -> Settings:
        settings = Settings(
            backend=Backend(os.getenv("FLAG_BACKEND", Backend.FLAGD_INPROCESS.value)),
            strategy=Strategy(os.getenv("FLAG_STRATEGY", Strategy.DIRECT.value)),
            flipt_url=os.getenv("FLIPT_URL", "http://localhost:8080"),
            flipt_environment=os.getenv("FLIPT_ENVIRONMENT", "default"),
            flipt_namespace=os.getenv("FLIPT_NAMESPACE", "default"),
            flagd_host=os.getenv("FLAGD_HOST", "localhost"),
            flagd_port=int(os.getenv("FLAGD_PORT", "8013")),
            flagd_flag_file=os.getenv("FLAGD_FLAG_FILE", "/app/flagd/flags.json"),
            flagd_rpc_cache=os.getenv("FLAGD_RPC_CACHE", "disabled").lower(),
            flag_manifest_path=os.getenv(
                "FLAG_MANIFEST_PATH", "/app/flags/manifest.json"
            ),
            flags_per_request=int(os.getenv("FLAGS_PER_REQUEST", "3")),
            deadline_ms=int(os.getenv("FLAG_DEADLINE_MS", "500")),
            flag_pool_size=int(os.getenv("FLAG_POOL_SIZE", "16")),
        )
        if settings.flagd_rpc_cache not in {"disabled", "lru"}:
            raise ValueError("FLAGD_RPC_CACHE must be disabled or lru")
        if settings.deadline_ms < 1:
            raise ValueError("FLAG_DEADLINE_MS must be at least 1")
        if settings.flag_pool_size < 1:
            raise ValueError("FLAG_POOL_SIZE must be at least 1")
        if settings.flags_per_request < 1:
            raise ValueError("FLAGS_PER_REQUEST must be at least 1")
        return settings

    @property
    def label(self) -> str:
        return f"{self.backend.value}+{self.strategy.value}"

    @property
    def is_remote(self) -> bool:
        return self.backend in (Backend.FLIPT, Backend.FLAGD_RPC)

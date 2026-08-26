from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

TimeoutProfile = Literal["fetch", "search"]


@dataclass(frozen=True)
class HttpTimeoutBudget:
    overall_s: float
    connect_s: float
    read_s: float
    write_s: float
    pool_s: float

    def as_httpx_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect_s,
            read=self.read_s,
            write=self.write_s,
            pool=self.pool_s,
        )


def build_http_timeout_budget(overall_s: float, *, profile: TimeoutProfile) -> HttpTimeoutBudget:
    total = float(overall_s)
    if total <= 0:
        raise ValueError("overall timeout must be > 0")

    if profile == "fetch":
        connect_cap = 5.0
        write_cap = 10.0
        pool_cap = 5.0
    else:
        connect_cap = 10.0
        write_cap = 15.0
        pool_cap = 10.0

    return HttpTimeoutBudget(
        overall_s=total,
        connect_s=min(total, connect_cap),
        read_s=total,
        write_s=min(total, write_cap),
        pool_s=min(total, pool_cap),
    )


def format_http_timeout_error(
    *,
    operation: str,
    budget: HttpTimeoutBudget,
    error: httpx.TimeoutException,
) -> str:
    if isinstance(error, httpx.ConnectTimeout):
        phase = "connection setup"
    elif isinstance(error, httpx.ReadTimeout):
        phase = "response read"
    elif isinstance(error, httpx.WriteTimeout):
        phase = "request write"
    elif isinstance(error, httpx.PoolTimeout):
        phase = "connection-pool wait"
    else:
        phase = "network I/O"

    budget_text = (
        f"connect={budget.connect_s:g}s read={budget.read_s:g}s "
        f"write={budget.write_s:g}s pool={budget.pool_s:g}s"
    )
    detail = str(error).strip()
    if detail:
        return (
            f"{operation} timed out during {phase} "
            f"({budget_text}; overall={budget.overall_s:g}s): {detail}"
        )
    return f"{operation} timed out during {phase} ({budget_text}; overall={budget.overall_s:g}s)."

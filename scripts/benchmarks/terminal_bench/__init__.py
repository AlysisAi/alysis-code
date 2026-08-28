"""Terminal-Bench integration for Alysis Code."""

from typing import Any

__all__ = ["AlysisHarborAgent", "AlysisSimpleAgent"]


def __getattr__(name: str) -> Any:
    if name == "AlysisSimpleAgent":
        from .alysis_agent import AlysisSimpleAgent

        return AlysisSimpleAgent
    if name == "AlysisHarborAgent":
        from .harbor_agent import AlysisHarborAgent

        return AlysisHarborAgent
    raise AttributeError(name)

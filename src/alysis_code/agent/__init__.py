from __future__ import annotations

import sys
from typing import TypeVar

_T = TypeVar("_T")


def _patchable(name: str, default: _T) -> _T:
    mod = sys.modules.get("alysis_code.agent_loop")
    return getattr(mod, name, default) if mod is not None else default

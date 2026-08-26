from __future__ import annotations

import sys
from typing import TypeVar

_T = TypeVar("_T")


def _patchable(name: str, default: _T) -> _T:
    module = sys.modules.get("alysis_code.cli")
    return getattr(module, name, default) if module is not None else default

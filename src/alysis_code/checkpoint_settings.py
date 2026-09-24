"""Already-validated checkpoint settings for the isolated filesystem worker.

Application configuration owns defaults and validation. The private worker IPC
copies all fields into this immutable value without importing the application's
configuration library inside the fixed checkpoint deadline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class CheckpointSettings:
    enabled: bool
    max_files: int
    max_total_bytes: int
    max_candidates: int
    objective_command: str
    objective_key: str
    objective_direction: Literal["minimize", "maximize"]

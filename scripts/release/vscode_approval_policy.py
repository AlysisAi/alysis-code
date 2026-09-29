"""Source-pinned single-maintainer approval authorized for the VS Code release."""

from __future__ import annotations

# The owner requested solo approval on 2026-09-22. Keep the immutable GitHub identity
# pinned so a similarly named account cannot inherit that exception. Actual approval
# receipts are still required for signing, evidence, and Marketplace promotion.
SINGLE_MAINTAINER_LOGIN = "Perdikis10"
SINGLE_MAINTAINER_ID = 190930654


def is_single_maintainer_login(login: str) -> bool:
    return login.casefold() == SINGLE_MAINTAINER_LOGIN.casefold()


def is_single_maintainer(login: str, user_id: int) -> bool:
    return is_single_maintainer_login(login) and user_id == SINGLE_MAINTAINER_ID

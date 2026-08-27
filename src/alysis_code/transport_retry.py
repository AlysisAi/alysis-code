"""Retry policy for a connection dropped mid-response.

The defect this exists for
--------------------------
A transport can close a response before the complete body arrives::

    Infrastructure error after retries: LLM request failed: peer closed
    connection without sending complete message body (incomplete chunked read)

That failure happens with no usable response body. Retrying costs a bounded
amount of time and does not repeat completed model work, while giving up
immediately can fail an otherwise recoverable request.

The general provider retry budget is one retry, sized for a throttled or
briefly unavailable endpoint. That is the right default for a call that might
have half-succeeded; it is far too thin for a transport that drops the body
before a single token arrives. This module gives that specific failure a
budget of its own -- 5 retries at 2/4/8/16/32s -- and leaves every other
retry decision exactly as it was.

Why a separate module
---------------------
Deliberately dependency-light: standard library only, and no imports from the
rest of the package. ``failure_category`` (which owns the general
classification) imports ``httpx``, and ``provider_limits`` imports
``failure_category``, so neither can be unit-tested in a bare interpreter.
The predicate here is the part that has to be provable against the exact
production string above, so it lives where a test can load it by file path
with nothing installed -- the same arrangement as ``budget_policy.py``.

Matching therefore works on exception *type names* and message text rather
than ``isinstance`` against httpx classes. That is not a workaround: the
failure arrives wrapped in ``LLMError`` with the original as ``__cause__``,
so the chain has to be walked as text anyway.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

# Machine-readable marker for "the transport kept dropping the connection and
# the retry budget for that is spent". A genuine error -- no work was
# possible, so it stays non-zero -- but a *named* one, so diagnostics can
# distinguish a dead route from a model that failed the task.
STOP_REASON_TRANSPORT_CONNECTION_FAILURE = "transport_connection_failure"

# Reason recorded on each retry of this class, distinct from the general
# "provider_unavailable" so the two budgets are separable in telemetry.
RETRY_REASON_TRANSPORT_CONNECTION_DROP = "transport_connection_drop"

# Retries granted *after* the initial attempt: 5 retries, so 6 attempts total,
# producing exactly the 2/4/8/16/32 schedule below. Worst case adds 62s of
# sleep to a call that had otherwise produced nothing at all.
DEFAULT_CONNECTION_DROP_MAX_RETRIES = 5

DEFAULT_CONNECTION_DROP_BASE_DELAY_SECONDS = 2.0
DEFAULT_CONNECTION_DROP_MAX_DELAY_SECONDS = 32.0

# Same jitter ratio the general provider retry uses, so the two schedules
# behave alike under load and only their magnitudes differ.
JITTER_RATIO = 0.25

# Substrings identifying a connection that closed while the response body was
# still being read. Deliberately narrow: a DNS failure, a refused connection,
# or a read timeout is a *different* failure with a different fix, and is
# already handled by the general classifier. Widening this set widens the
# extra budget, so it should stay restricted to mid-body drops.
CONNECTION_DROP_MESSAGE_MARKERS: tuple[str, ...] = (
    "peer closed connection",
    "incomplete chunked read",
    "server disconnected without sending complete message body",
    "server disconnected",
    "remote protocol error",
    "connection reset",
    "connection aborted",
    "response body ended early",
    # Compact class-name spellings. A wrapped message frequently renders the
    # originating class rather than prose ("RemoteProtocolError: ..."), and
    # the spaced markers above do not match that.
    "remoteprotocolerror",
    "connectionreseterror",
    "connectionabortederror",
    "incompleteread",
    "chunkedencodingerror",
)

# Exception *type* names that mean the same thing, matched by name so this
# module never has to import httpx. ConnectionResetError and
# ConnectionAbortedError are builtins and would match by isinstance too; the
# rest arrive from httpx/httpcore/urllib3 depending on the stack in play.
CONNECTION_DROP_TYPE_NAMES: frozenset[str] = frozenset(
    {
        "ChunkedEncodingError",
        "ConnectionAbortedError",
        "ConnectionResetError",
        "IncompleteRead",
        "ProtocolError",
        "RemoteProtocolError",
    }
)

# Attribute used to tag an exception whose connection-drop budget is spent.
TRANSPORT_FAILURE_ATTR = "_transport_connection_failure"


def _iter_exception_chain(error: BaseException | None) -> Iterator[BaseException]:
    """Yield ``error`` and everything it was raised from, without cycling."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        nxt = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        current = nxt if isinstance(nxt, BaseException) else None


def is_connection_drop_message(
    message: str | None,
    markers: Sequence[str] = CONNECTION_DROP_MESSAGE_MARKERS,
) -> bool:
    """True when ``message`` names a connection dropped mid-response.

    Case-insensitive substring matching, which is what the codebase already
    does for every other error-signature list -- provider messages are prose
    and there is no structured field to read instead.
    """
    text = str(message or "").casefold()
    if not text:
        return False
    return any(marker in text for marker in markers)


def is_connection_drop_error(error: BaseException | None) -> bool:
    """True when ``error``, or anything it was raised from, is a mid-body drop.

    The chain has to be walked: the transport raises
    ``httpx.RemoteProtocolError`` and the client re-raises it as
    ``LLMError("LLM request failed: ...")``, so neither the outermost type nor
    a bare ``str(error)`` is reliable on its own.
    """
    if error is None:
        return False
    for exc in _iter_exception_chain(error):
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
            return True
        if type(exc).__name__ in CONNECTION_DROP_TYPE_NAMES:
            return True
        if is_connection_drop_message(str(exc)):
            return True
    return False


def connection_drop_delay_seconds(
    retry_index: int,
    *,
    base_delay_seconds: float = DEFAULT_CONNECTION_DROP_BASE_DELAY_SECONDS,
    max_delay_seconds: float = DEFAULT_CONNECTION_DROP_MAX_DELAY_SECONDS,
    jitter_sample: float = 0.5,
) -> float:
    """Backoff for retry number ``retry_index`` (0-based): 2, 4, 8, 16, 32s.

    ``jitter_sample`` is a caller-supplied uniform draw in ``[0, 1]``; the
    default of ``0.5`` is the midpoint and yields the exact schedule, which
    keeps the arithmetic testable without stubbing a random source. Jitter is
    symmetric around the raw delay, matching the general provider retry, so
    concurrent runs do not resynchronise onto the same instant.
    """
    index = max(0, int(retry_index))
    base = max(0.0, float(base_delay_seconds))
    ceiling = max(base, float(max_delay_seconds))
    raw = min(ceiling, base * (2**index))
    sample = min(max(float(jitter_sample), 0.0), 1.0)
    offset = (sample - 0.5) * 2.0 * JITTER_RATIO * raw
    return max(0.0, min(ceiling, raw + offset))


def connection_drop_backoff_schedule(
    retries: int = DEFAULT_CONNECTION_DROP_MAX_RETRIES,
    *,
    base_delay_seconds: float = DEFAULT_CONNECTION_DROP_BASE_DELAY_SECONDS,
    max_delay_seconds: float = DEFAULT_CONNECTION_DROP_MAX_DELAY_SECONDS,
) -> list[float]:
    """The un-jittered schedule, for documentation and tests."""
    return [
        connection_drop_delay_seconds(
            index,
            base_delay_seconds=base_delay_seconds,
            max_delay_seconds=max_delay_seconds,
        )
        for index in range(max(0, int(retries)))
    ]


def connection_drop_retry_budget(
    general_max_retries: int,
    *,
    connection_drop_max_retries: int = DEFAULT_CONNECTION_DROP_MAX_RETRIES,
) -> int:
    """Retries allowed for a connection drop, given the general budget.

    Never *reduces* an already generous configuration: a campaign that raised
    the provider retry budget keeps it. This only guarantees a floor for the
    one failure class that needs it.
    """
    return max(int(general_max_retries), int(connection_drop_max_retries))


def connection_drop_budget_exhausted(retries_used: int, retries_allowed: int) -> bool:
    """Whether another connection-drop retry is still permitted."""
    return int(retries_used) >= int(retries_allowed)


def mark_transport_connection_failure(error: BaseException | None) -> None:
    """Tag ``error`` as "connection-drop budget spent".

    Read back by the diagnostics builder so the recorded failure names the
    transport rather than being one more anonymous infrastructure error.
    Never raises: some exception types forbid attribute assignment, and a
    missing label must not turn into a second failure.
    """
    if error is None:
        return
    try:
        setattr(error, TRANSPORT_FAILURE_ATTR, True)
    except Exception:  # noqa: BLE001 - tagging must never crash the caller
        pass


def transport_connection_failure_reason(error: BaseException | None) -> str | None:
    """Stop reason for ``error``, or ``None`` when it is not one of these.

    Returns the reason for an explicitly tagged error, and also for an
    untagged one whose chain is unambiguously a connection drop -- a failure
    that never reached the retry loop (or was vetoed before it) is still a
    transport failure and should be labelled as one.
    """
    if error is None:
        return None
    for exc in _iter_exception_chain(error):
        if getattr(exc, TRANSPORT_FAILURE_ATTR, False):
            return STOP_REASON_TRANSPORT_CONNECTION_FAILURE
    if is_connection_drop_error(error):
        return STOP_REASON_TRANSPORT_CONNECTION_FAILURE
    return None

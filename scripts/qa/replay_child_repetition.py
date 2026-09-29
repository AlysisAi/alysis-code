from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alysis_code.agent.turn.core import _child_tool_outcome_fingerprint


@dataclass(frozen=True)
class ReplayedOutcome:
    step: int
    tool_name: str
    tool_call_id: str
    fingerprint: str
    consecutive_count: int
    canonical_json: str


def _canonical_outcome(*, tool_name: str, arguments: dict[str, Any], result: Any) -> str:
    return json.dumps(
        {
            "tool": str(tool_name or "").strip().casefold(),
            "arguments": arguments,
            "result": result,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def replay_session(path: Path) -> list[ReplayedOutcome]:
    pending_calls: dict[str, dict[str, Any]] = {}
    outcomes: list[ReplayedOutcome] = []
    previous_fingerprint = ""
    consecutive_count = 0

    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at {path}:{line_number}: {exc}") from exc
            if not isinstance(event, dict):
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            event_type = str(event.get("type") or "")
            tool_call_id = str(payload.get("tool_call_id") or "")
            if not tool_call_id:
                continue
            if event_type == "tool_call":
                pending_calls[tool_call_id] = payload
                continue
            if event_type != "tool_result":
                continue
            call = pending_calls.pop(tool_call_id, None)
            if call is None:
                raise ValueError(
                    f"tool_result at {path}:{line_number} has no preceding tool_call: "
                    f"{tool_call_id}"
                )
            tool_name = str(call.get("name") or payload.get("name") or "")
            arguments = call.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            result = payload.get("result")
            fingerprint = _child_tool_outcome_fingerprint(
                tool_name=tool_name,
                redacted_arguments=arguments,
                redacted_result=result,
            )
            if fingerprint == previous_fingerprint:
                consecutive_count += 1
            else:
                previous_fingerprint = fingerprint
                consecutive_count = 1
            outcomes.append(
                ReplayedOutcome(
                    step=int(payload.get("step") or call.get("step") or 0),
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    fingerprint=fingerprint,
                    consecutive_count=consecutive_count,
                    canonical_json=_canonical_outcome(
                        tool_name=tool_name,
                        arguments=arguments,
                        result=result,
                    ),
                )
            )

    if pending_calls:
        unresolved = ", ".join(sorted(pending_calls))
        raise ValueError(f"tool_call event(s) have no persisted result: {unresolved}")
    return outcomes


def _records_in_range(
    records: list[ReplayedOutcome], *, start_step: int | None, end_step: int | None
) -> list[ReplayedOutcome]:
    return [
        record
        for record in records
        if (start_step is None or record.step >= start_step)
        and (end_step is None or record.step <= end_step)
    ]


def _print_byte_diff(before: ReplayedOutcome, after: ReplayedOutcome) -> None:
    before_bytes = before.canonical_json.encode("utf-8")
    after_bytes = after.canonical_json.encode("utf-8")
    shared_length = min(len(before_bytes), len(after_bytes))
    difference_at = next(
        (index for index in range(shared_length) if before_bytes[index] != after_bytes[index]),
        shared_length,
    )
    context_start = max(0, difference_at - 32)
    context_end = difference_at + 33
    print(
        f"byte_diff {before.tool_call_id} -> {after.tool_call_id} "
        f"offset={difference_at} lengths={len(before_bytes)}->{len(after_bytes)}"
    )
    print(
        "before_bytes="
        f"{before_bytes[context_start:context_end].hex()} "
        f"range={context_start}:{min(context_end, len(before_bytes))}"
    )
    print(
        "after_bytes="
        f"{after_bytes[context_start:context_end].hex()} "
        f"range={context_start}:{min(context_end, len(after_bytes))}"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay persisted child tool outcomes through Alysis Code's real repetition "
            "fingerprint function."
        )
    )
    parser.add_argument("session", type=Path, help="Child session JSONL path.")
    parser.add_argument("--step-start", type=int)
    parser.add_argument("--step-end", type=int)
    parser.add_argument("--tail-start", type=int)
    parser.add_argument("--tail-end", type=int)
    parser.add_argument("--fingerprint-prefix", type=int, default=12)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        records = replay_session(args.session)
    except (OSError, ValueError) as exc:
        print(f"replay failed: {exc}", file=sys.stderr)
        return 2

    selected = _records_in_range(
        records,
        start_step=args.step_start,
        end_step=args.step_end,
    )
    for record in selected:
        print(
            f"step={record.step} tool={record.tool_name} "
            f"fingerprint={record.fingerprint[: max(1, args.fingerprint_prefix)]} "
            f"consecutive={record.consecutive_count}"
        )

    maximum = max((record.consecutive_count for record in selected), default=0)
    print(f"selected_calls={len(selected)} max_consecutive={maximum}")

    if args.tail_start is not None or args.tail_end is not None:
        tail = _records_in_range(
            records,
            start_step=args.tail_start,
            end_step=args.tail_end,
        )
        tail_identical = bool(tail) and len({record.fingerprint for record in tail}) == 1
        print(
            f"tail_calls={len(tail)} tail_identical={'yes' if tail_identical else 'no'} "
            f"tail_range={args.tail_start}-{args.tail_end}"
        )
        if not tail_identical:
            for before, after in zip(tail, tail[1:], strict=False):
                if before.fingerprint != after.fingerprint:
                    _print_byte_diff(before, after)
                    break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

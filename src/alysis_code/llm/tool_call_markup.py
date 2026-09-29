"""Detect tool protocol leaked into prose without interpreting it as commands."""

from __future__ import annotations

import re

_MARKUP = re.compile(
    r"<\s*(?:[|｜]{1,2}\s*DSML\s*[|｜]{1,2}\s*(?:tool_calls|calls|invoke|parameter)\b"
    r"|(?:tool_calls?|function_calls)\s*>|invoke\s+name\s*=)",
    re.IGNORECASE,
)
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+)(?:.*?\1|.*$)")


class ToolCallMarkupFilter:
    """Stream prose while holding incomplete tags, including split DSML markers.

    Fenced examples, inline code, and Markdown quotations remain ordinary text.
    Once live protocol markup appears, suppress the remainder of that response.
    No parameter value is parsed and no command is executed here.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._pending = ""
        self._sent = 0
        self._fence = ""
        self.detected = False

    def _line(self, line: str, *, complete: bool) -> str:
        if self.detected:
            return ""
        fence = _FENCE.match(line)
        if self._fence:
            if (
                complete
                and fence
                and fence[1][0] == self._fence[0]
                and len(fence[1]) >= len(self._fence)
            ):
                self._fence = ""
            return line
        if fence:
            if complete:
                self._fence = fence[1]
            return line
        if line.lstrip().startswith(">"):
            return line
        masked = _INLINE_CODE.sub(lambda m: " " * len(m[0]), line)
        match = _MARKUP.search(masked)
        if match:
            self.detected = True
            return line[: match.start()]
        if not complete:
            # A tag can arrive one character at a time. Keep its opening '<'
            # until it is recognized or closed; ordinary prose streams now.
            start = masked.rfind("<")
            if start >= 0 and ">" not in masked[start:]:
                return line[:start]
        return line

    def feed(self, text: str) -> str:
        if self.detected:
            return ""
        parts = text.splitlines(keepends=True)
        output = []
        for part in parts:
            self._pending += part
            complete = part.endswith(("\n", "\r"))
            visible = self._line(self._pending, complete=complete)
            output.append(visible[self._sent :])
            self._sent = len(visible)
            if complete:
                self._pending = ""
                self._sent = 0
            if self.detected:
                break
        return "".join(output)

    def finish(self) -> str:
        visible = self._line(self._pending, complete=True)
        result = visible[self._sent :]
        self._pending = ""
        self._sent = 0
        return result


def contains_tool_call_markup(text: str) -> bool:
    detector = ToolCallMarkupFilter()
    detector.feed(text)
    detector.finish()
    return detector.detected

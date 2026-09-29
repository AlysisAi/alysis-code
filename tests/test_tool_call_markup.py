import pytest

from alysis_code.llm.tool_call_markup import ToolCallMarkupFilter, contains_tool_call_markup

MARKUP = '<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="shell_run">\n<｜｜DSML｜｜ parameter name="cmd" string="true">java -version</｜｜DSML｜｜ parameter>\n</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>'


def test_prose_streams_immediately_and_reset_discards_abandoned_partial_tag():
    guard = ToolCallMarkupFilter()
    assert guard.feed("Checking now. ") == "Checking now. "
    assert guard.feed("<｜｜DS") == ""
    guard.reset()
    assert guard.feed("A new reply.") == "A new reply."
    assert guard.finish() == ""


@pytest.mark.parametrize(
    "text", [MARKUP, MARKUP.replace("｜｜", "|"), '<invoke name="shell_run">x</invoke>']
)
def test_markup_is_suppressed_at_every_stream_split(text):
    for split in range(len(text) + 1):
        guard = ToolCallMarkupFilter()
        output = (
            guard.feed("Checking.\n" + text[:split]) + guard.feed(text[split:]) + guard.finish()
        )
        assert guard.detected
        assert output == "Checking.\n"


@pytest.mark.parametrize(
    "text",
    [
        f"Example:\n```xml\n{MARKUP}\n```\nThat is malformed.",
        f"Example:\n~~~xml\n{MARKUP}\n~~~\nThat is malformed.",
        'The tag `<invoke name="shell_run">` is not an API call.',
        "> <｜｜DSML｜｜ calls>\nThis is quoted.",
        "Use curl to download the file.\nThen check it.",
    ],
)
def test_quoted_examples_and_ordinary_text_are_preserved(text):
    guard = ToolCallMarkupFilter()
    output = "".join(guard.feed(char) for char in text) + guard.finish()
    assert not contains_tool_call_markup(text)
    assert output == text

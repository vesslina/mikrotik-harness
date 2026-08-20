from __future__ import annotations

import re

from rich.text import Text

_TOKEN = re.compile(r"(\*\*[^*\n]+\*\*|`[^`\n]+`|\*[^*\n]+\*)")


def markdown_to_text(value: str, *, style: str = "white") -> Text:
    """Render the small Markdown subset most LLM replies use in the TUI.

    RichLog already handles Text renderables efficiently. Keeping this parser
    deliberately small avoids a full Markdown document renderer while still
    making bold text, inline code, italics, headings, and list markers readable.
    """

    output = Text()
    lines = value.splitlines() or [""]
    for index, line in enumerate(lines):
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if heading:
            line = heading.group(1)
            _append_inline(output, line, style=f"bold {style}")
        else:
            _append_inline(output, line, style=style)
        if index < len(lines) - 1:
            output.append("\n", style=style)
    return output


def _append_inline(output: Text, line: str, *, style: str) -> None:
    cursor = 0
    for match in _TOKEN.finditer(line):
        if match.start() > cursor:
            output.append(line[cursor : match.start()], style=style)
        token = match.group(0)
        if token.startswith("**") and token.endswith("**"):
            output.append(token[2:-2], style=f"bold {style}")
        elif token.startswith("`") and token.endswith("`"):
            output.append(token[1:-1], style="#7dd3fc on #20252b")
        else:
            output.append(token[1:-1], style=f"italic {style}")
        cursor = match.end()
    if cursor < len(line):
        output.append(line[cursor:], style=style)

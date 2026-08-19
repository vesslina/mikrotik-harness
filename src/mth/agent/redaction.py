from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from mth.core.mcp_client.models import McpToolResult

REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = frozenset(
    {
        "apikey",
        "community",
        "credential",
        "credentials",
        "pass",
        "passphrase",
        "passwd",
        "password",
        "presharedkey",
        "privatekey",
        "psk",
        "secret",
        "token",
    }
)
_TEXT_SECRET = re.compile(
    r"(?i)(\b(?:password|passwd|passphrase|secret|api[-_ ]?key|"
    r"private[-_ ]?key|pre[-_ ]?shared[-_ ]?key|psk|community)\b"
    r"\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
)


def _normalized_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).casefold())


def redact_sensitive_value(value: Any) -> Any:
    """Recursively remove credential-shaped fields from untrusted tool output."""
    if isinstance(value, Mapping):
        return {
            str(key): (
                REDACTED
                if _normalized_key(key) in _SENSITIVE_KEYS
                else redact_sensitive_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_sensitive_value(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def redact_sensitive_text(value: str) -> str:
    return _TEXT_SECRET.sub(lambda match: f"{match.group(1)}{REDACTED}", value)


def redact_tool_result(result: McpToolResult) -> McpToolResult:
    structured = (
        redact_sensitive_value(result.structured_content)
        if result.structured_content is not None
        else None
    )
    return McpToolResult(
        content=tuple(redact_sensitive_text(item) for item in result.content),
        structured_content=structured,
        is_error=result.is_error,
    )

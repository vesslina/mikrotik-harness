from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from mth.core.mcp_client.models import McpTool


class ProviderErrorCode(StrEnum):
    CONNECTION_FAILED = "PROVIDER_CONNECTION_FAILED"
    AUTHENTICATION_FAILED = "PROVIDER_AUTHENTICATION_FAILED"
    MODEL_NOT_FOUND = "PROVIDER_MODEL_NOT_FOUND"
    INVALID_RESPONSE = "PROVIDER_INVALID_RESPONSE"


class ProviderError(RuntimeError):
    def __init__(self, code: ProviderErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ProviderToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ProviderReply:
    content: str
    tool_calls: tuple[ProviderToolCall, ...]
    reasoning: str = ""
    reasoning_tokens: int | None = None


class ChatProvider(Protocol):
    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[McpTool] = (),
    ) -> ProviderReply: ...


HttpTransport = Callable[[str, Mapping[str, str], bytes, float], bytes]


class OpenAICompatibleClient:
    """Minimal non-streaming transport for an OpenAI-compatible chat endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 60.0,
        transport: HttpTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport or self._send_http

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[McpTool] = (),
    ) -> ProviderReply:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [dict(message) for message in messages],
            "stream": False,
        }
        if tools:
            payload["tools"] = [self._tool_payload(tool) for tool in tools]
            payload["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        try:
            raw = await asyncio.to_thread(
                self._transport,
                f"{self.base_url}/chat/completions",
                headers,
                body,
                self._timeout,
            )
        except urllib.error.HTTPError as error:
            detail = self._http_error_detail(error)
            lowered = detail.lower()
            if error.code in {401, 403}:
                code = ProviderErrorCode.AUTHENTICATION_FAILED
            elif error.code == 404 or (
                error.code == 400
                and "model" in lowered
                and any(marker in lowered for marker in ("not found", "unknown", "invalid"))
            ):
                code = ProviderErrorCode.MODEL_NOT_FOUND
            else:
                code = ProviderErrorCode.CONNECTION_FAILED
            raise ProviderError(code, detail) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ProviderError(
                ProviderErrorCode.CONNECTION_FAILED,
                f"Could not reach the model provider: {error}",
            ) from error

        try:
            document = json.loads(raw.decode("utf-8"))
            return self._parse_reply(document)
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                f"Provider returned an invalid chat completion: {error}",
            ) from error

    @staticmethod
    def _send_http(
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> bytes:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return bytes(response.read())

    @staticmethod
    def _tool_payload(tool: McpTool) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.input_schema,
            },
        }

    @staticmethod
    def _parse_reply(document: Any) -> ProviderReply:
        if not isinstance(document, dict):
            raise TypeError("top-level response is not an object")
        choices = document["choices"]
        if not isinstance(choices, list) or not choices:
            raise ValueError("response has no choices")
        choice = choices[0]
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            raise TypeError("choice message is not an object")
        message = choice["message"]
        content = message.get("content") or ""
        if not isinstance(content, str):
            raise TypeError("message content is not text")
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        if not isinstance(reasoning, str):
            raise TypeError("message reasoning is not text")

        reasoning_tokens: int | None = None
        usage = document.get("usage")
        if isinstance(usage, dict):
            details = usage.get("completion_tokens_details")
            if isinstance(details, dict):
                raw_reasoning_tokens = details.get("reasoning_tokens")
                if isinstance(raw_reasoning_tokens, int) and not isinstance(
                    raw_reasoning_tokens, bool
                ):
                    reasoning_tokens = raw_reasoning_tokens

        calls: list[ProviderToolCall] = []
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise TypeError("tool_calls is not a list")
        for raw_call in raw_calls:
            if not isinstance(raw_call, dict) or not isinstance(raw_call.get("function"), dict):
                raise TypeError("tool call is malformed")
            function = raw_call["function"]
            arguments = json.loads(function.get("arguments", "{}"))
            if not isinstance(arguments, dict):
                raise TypeError("tool arguments are not an object")
            calls.append(
                ProviderToolCall(
                    call_id=str(raw_call.get("id") or f"call-{len(calls) + 1}"),
                    name=str(function["name"]),
                    arguments=arguments,
                )
            )
        return ProviderReply(
            content=content,
            tool_calls=tuple(calls),
            reasoning=reasoning,
            reasoning_tokens=reasoning_tokens,
        )

    @staticmethod
    def _http_error_detail(error: urllib.error.HTTPError) -> str:
        try:
            body = error.read().decode("utf-8", errors="replace")
        except OSError:
            body = ""
        suffix = f": {body[:400]}" if body else ""
        return f"Provider HTTP {error.code}{suffix}"

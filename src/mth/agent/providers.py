from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
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


@dataclass(frozen=True, slots=True)
class ProviderStreamChunk:
    """Incremental provider output; ``reply`` is populated once the stream ends."""

    content: str = ""
    reasoning: str = ""
    reply: ProviderReply | None = None


class ChatProvider(Protocol):
    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[McpTool] = (),
    ) -> ProviderReply: ...


HttpTransport = Callable[[str, Mapping[str, str], bytes, float], bytes]


class OpenAICompatibleClient:
    """Small stdlib OpenAI-compatible transport with optional SSE streaming."""

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
        payload = self._payload(messages, tools, stream=False)
        headers = self._headers()
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

    async def stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[McpTool] = (),
    ) -> AsyncIterator[ProviderStreamChunk]:
        """Read OpenAI SSE chunks without buffering the provider response."""

        payload = self._payload(messages, tools, stream=True)
        headers = self._headers()
        queue: asyncio.Queue[bytes | BaseException | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def publish(item: bytes | BaseException | None) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, item)

        def worker() -> None:
            try:
                request = urllib.request.Request(
                    f"{self.base_url}/chat/completions",
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    for line in response:
                        publish(bytes(line))
            except urllib.error.HTTPError as error:
                publish(self._http_provider_error(error))
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                publish(
                    ProviderError(
                        ProviderErrorCode.CONNECTION_FAILED,
                        f"Could not reach the model provider: {error}",
                    )
                )
            finally:
                publish(None)

        task = asyncio.create_task(asyncio.to_thread(worker))
        content: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        reasoning_tokens: int | None = None
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                line = item.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if not line.startswith("data:") and not line.startswith("{"):
                    continue
                data = line[5:].strip() if line.startswith("data:") else line
                if data == "[DONE]":
                    continue
                try:
                    document = json.loads(data)
                except json.JSONDecodeError as error:
                    raise ProviderError(
                        ProviderErrorCode.INVALID_RESPONSE,
                        f"Provider returned an invalid streaming event: {error}",
                    ) from error
                if not isinstance(document, dict):
                    continue
                usage = document.get("usage")
                if isinstance(usage, dict):
                    details = usage.get("completion_tokens_details")
                    if isinstance(details, dict) and isinstance(
                        details.get("reasoning_tokens"), int
                    ):
                        reasoning_tokens = details["reasoning_tokens"]
                choices = document.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                choice = choices[0]
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta") or choice.get("message") or {}
                if not isinstance(delta, dict):
                    continue
                content_delta = delta.get("content") or ""
                reasoning_delta = delta.get("reasoning_content") or delta.get("reasoning") or ""
                if isinstance(content_delta, str) and content_delta:
                    content.append(content_delta)
                    yield ProviderStreamChunk(content=content_delta)
                if isinstance(reasoning_delta, str) and reasoning_delta:
                    reasoning.append(reasoning_delta)
                    yield ProviderStreamChunk(reasoning=reasoning_delta)
                raw_calls = delta.get("tool_calls") or []
                if isinstance(raw_calls, list):
                    for raw_call in raw_calls:
                        if not isinstance(raw_call, dict):
                            continue
                        index = raw_call.get("index", len(calls))
                        if not isinstance(index, int):
                            continue
                        entry = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        if isinstance(raw_call.get("id"), str):
                            entry["id"] = raw_call["id"]
                        function = raw_call.get("function")
                        if not isinstance(function, dict):
                            continue
                        if isinstance(function.get("name"), str):
                            entry["name"] = function["name"]
                        if isinstance(function.get("arguments"), str):
                            entry["arguments"] += function["arguments"]
        finally:
            # ``to_thread`` cannot interrupt a blocking socket read.  Do not
            # keep a cancelled UI worker waiting for the provider timeout; the
            # daemon thread will finish and close its response independently.
            if not task.done():
                task.cancel()

        tool_calls: list[ProviderToolCall] = []
        for index in sorted(calls):
            entry = calls[index]
            try:
                arguments = json.loads(entry["arguments"] or "{}")
            except json.JSONDecodeError as error:
                raise ProviderError(
                    ProviderErrorCode.INVALID_RESPONSE,
                    f"Provider returned malformed streamed tool arguments: {error}",
                ) from error
            if not isinstance(arguments, dict):
                raise ProviderError(
                    ProviderErrorCode.INVALID_RESPONSE,
                    "Provider returned streamed tool arguments that are not an object",
                )
            tool_calls.append(
                ProviderToolCall(
                    call_id=entry["id"] or f"call-{index + 1}",
                    name=entry["name"],
                    arguments=arguments,
                )
            )
        yield ProviderStreamChunk(
            reply=ProviderReply(
                content="".join(content),
                tool_calls=tuple(tool_calls),
                reasoning="".join(reasoning),
                reasoning_tokens=reasoning_tokens,
            )
        )

    def _payload(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[McpTool],
        *,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [dict(message) for message in messages],
            "stream": stream,
        }
        if tools:
            payload["tools"] = [self._tool_payload(tool) for tool in tools]
            payload["tool_choice"] = "auto"
        return payload

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    @classmethod
    def _http_provider_error(cls, error: urllib.error.HTTPError) -> ProviderError:
        detail = cls._http_error_detail(error)
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
        return ProviderError(code, detail)

    @staticmethod
    def _http_error_detail(error: urllib.error.HTTPError) -> str:
        try:
            body = error.read().decode("utf-8", errors="replace")
        except OSError:
            body = ""
        suffix = f": {body[:400]}" if body else ""
        return f"Provider HTTP {error.code}{suffix}"

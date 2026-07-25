import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


MCP_PROTOCOL_VERSION = "2025-03-26"


class ModelRouterError(RuntimeError):
    pass


def _post(url: str, token: str, payload: dict[str, Any], timeout: float) -> tuple[str, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for _ in range(3):
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json, text/event-stream")
        request.add_header("MCP-Protocol-Version", MCP_PROTOCOL_VERSION)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8"), response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            if exc.code in (307, 308) and exc.headers.get("Location"):
                url = urllib.parse.urljoin(url, exc.headers["Location"])
                continue
            raise ModelRouterError(f"model router returned HTTP {exc.code}") from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            reason = getattr(exc, "reason", str(exc))
            raise ModelRouterError(f"model router is unavailable: {reason}") from exc
    raise ModelRouterError("model router returned too many redirects")


def _rpc_response(raw: str, content_type: str, request_id: int) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    if content_type.startswith("text/event-stream"):
        for line in raw.splitlines():
            if line.startswith("data:") and line[5:].strip():
                try:
                    messages.append(json.loads(line[5:].strip()))
                except json.JSONDecodeError as exc:
                    raise ModelRouterError("model router returned invalid event data") from exc
    elif raw.strip():
        try:
            messages.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise ModelRouterError("model router returned invalid JSON") from exc
    for message in messages:
        if message.get("id") == request_id and ("result" in message or "error" in message):
            return message
    raise ModelRouterError("model router response did not contain the requested result")


def call_chat(
    *,
    url: str,
    token: str,
    messages: list[dict[str, str]],
    external_user_id: str,
    provider: str = "",
    model_id: str = "",
    tier: str = "",
    params: dict[str, Any] | None = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "external_user_id": external_user_id,
        "messages": messages,
        "params": params or {},
    }
    if provider:
        arguments["provider"] = provider
    if model_id:
        arguments["model_id"] = model_id
    if tier:
        arguments["tier"] = tier
    request_id = 1
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": "chat", "arguments": arguments},
    }
    raw, content_type = _post(url, token, payload, timeout)
    response = _rpc_response(raw, content_type, request_id)
    if "error" in response:
        error = response["error"]
        raise ModelRouterError(f"model router error: {error.get('message', error)}")
    result = response["result"]
    if not isinstance(result, dict):
        raise ModelRouterError("model router returned an unexpected tool response")
    if result.get("isError"):
        texts = [part.get("text", "") for part in result.get("content", []) if part.get("type") == "text"]
        raise ModelRouterError("model call failed: " + (" ".join(texts) or str(result)))
    reply = result.get("structuredContent")
    if isinstance(reply, dict) and set(reply) == {"result"}:
        reply = reply["result"]
    if reply is None:
        texts = [part.get("text", "") for part in result.get("content", []) if part.get("type") == "text"]
        reply = {"text": "\n".join(texts)}
    if not isinstance(reply, dict) or not isinstance(reply.get("text"), str):
        raise ModelRouterError("model router returned an unexpected chat response")
    return reply

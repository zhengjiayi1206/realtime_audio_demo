import base64
import json
import logging
import time
from typing import Any, Callable

import httpx

from full_duplex_demo.config import (
    QWEN_API_BASE,
    QWEN_SPEAKER,
    REQUEST_TIMEOUT,
    TARGET_SAMPLE_RATE,
    resolved_provider,
)

logger = logging.getLogger("uvicorn.error")

_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=8, max_connections=20),
        )
    return _client


# ═══════════════════════════════════════════════════════════════
#  Payload builder — explicit full-duplex state
# ═══════════════════════════════════════════════════════════════


def _audio_item(wav_bytes: bytes) -> dict[str, Any]:
    provider = resolved_provider()
    audio_b64 = base64.b64encode(wav_bytes).decode("ascii")

    if provider == "vllm_omni":
        return {
            "type": "audio_url",
            "audio_url": {"url": f"data:audio/wav;base64,{audio_b64}"},
        }
    return {
        "type": "audio",
        "audio": f"data:audio/wav;base64,{audio_b64}",
    }


def _text_item(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


def build_omniflow_payload(
    *,
    model: str,
    wav_bytes: bytes,
    system_prompt: str,
    flow_history: list[dict[str, Any]],
    max_tokens: int,
    stream: bool = True,
) -> dict[str, Any]:
    """Build a chat/completions payload for one full-duplex audio tick."""
    provider = resolved_provider()

    current_user_message = {
        "role": "user",
        "content": [
            _audio_item(wav_bytes),
        ],
    }

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": [_text_item(system_prompt)]},
        *flow_history,
        current_user_message,
    ]

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "chat_template_kwargs": {"sampling_rate": TARGET_SAMPLE_RATE},
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": stream,
    }

    if provider == "vllm_omni":
        payload["modalities"] = ["text"]
        if QWEN_SPEAKER:
            payload["speaker"] = QWEN_SPEAKER

    return payload


def build_listen_speak_payload(
    *,
    model: str,
    wav_bytes: bytes,
    system_prompt: str,
    user_instruction: str = "请判断这段用户音频是否已经说完。",
) -> dict[str, Any]:
    """Build a non-streaming payload that returns only speak/listen."""
    provider = resolved_provider()
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": [_text_item(system_prompt)]},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_instruction},
                    _audio_item(wav_bytes),
                ],
            },
        ],
        "chat_template_kwargs": {"sampling_rate": TARGET_SAMPLE_RATE},
        "max_tokens": 2,
        "temperature": 0,
        "stream": False,
    }

    if provider == "vllm_omni":
        payload["modalities"] = ["text"]
        if QWEN_SPEAKER:
            payload["speaker"] = QWEN_SPEAKER

    return payload


# ═══════════════════════════════════════════════════════════════
#  Streaming HTTP
# ═══════════════════════════════════════════════════════════════


def stream_json_sync(
    url: str,
    payload: dict[str, Any],
    push: Callable[[dict[str, Any]], None],
) -> None:
    """Stream SSE from a POST request. Calls ``push(event)`` per line."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    client = _get_client()
    req_start = time.perf_counter()

    try:
        with client.stream(
            "POST", url, content=body,
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        ) as resp:
            if resp.status_code >= 400:
                push({
                    "type": "error",
                    "status_code": resp.status_code,
                    "message": resp.read().decode("utf-8", errors="replace")[:2000],
                })
                return

            first_token = True
            for raw_line in resp.iter_lines():
                line = raw_line.strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                if first_token:
                    first_token = False
                    ttft = int((time.perf_counter() - req_start) * 1000)
                    push({"type": "ttft", "ttft_ms": ttft})

                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    push({"type": "chunk", "data": json.loads(data)})
                except json.JSONDecodeError:
                    push({"type": "error", "message": data})
                    return
        push({"type": "done"})
    except httpx.HTTPStatusError as exc:
        push({"type": "error", "status_code": exc.response.status_code, "message": exc.response.text[:2000]})
    except Exception as exc:
        push({"type": "error", "message": str(exc)})


def complete_json_sync(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run a non-streaming JSON request."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    client = _get_client()
    resp = client.post(
        url,
        content=body,
        headers={"Content-Type": "application/json"},
    )
    if resp.status_code >= 400:
        raise RuntimeError(resp.text[:2000])
    return resp.json()

# ═══════════════════════════════════════════════════════════════
#  Response parsing
# ═══════════════════════════════════════════════════════════════


def extract_stream_delta(data: dict[str, Any]) -> str | None:
    """Extract text delta from one SSE chunk."""
    modality = data.get("modality")
    for choice in data.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if modality == "text" and isinstance(content, str):
            return content
        if isinstance(content, str) and len(content) < 200:
            return content
    return None


def extract_message_text(data: dict[str, Any]) -> str:
    for choice in data.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
    return ""


def extract_listen_speak_text(data: dict[str, Any]) -> str:
    return _parse_listen_speak_text(extract_message_text(data))


def _parse_listen_speak_text(text: str) -> str:
    cleaned = text.strip().lower()
    if "speak" in cleaned:
        return "speak"
    if "listen" in cleaned:
        return "listen"
    return "listen"

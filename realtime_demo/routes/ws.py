import asyncio
import base64
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import websockets

from realtime_demo.config import (
    QWEN_REALTIME_API_KEY,
    QWEN_REALTIME_MODEL,
    QWEN_REALTIME_SAMPLE_RATE,
    QWEN_REALTIME_SEND_RESPONSE_CREATE,
    QWEN_REALTIME_VOICE,
    realtime_ws_url,
)

router = APIRouter()
logger = logging.getLogger("uvicorn.error")
RESPONSE_TIMEOUT_S = 120


async def _connect_realtime(url: str, headers: dict[str, str]):
    try:
        return await websockets.connect(url, additional_headers=headers, max_size=64 * 1024 * 1024)
    except TypeError:
        return await websockets.connect(url, extra_headers=headers, max_size=64 * 1024 * 1024)


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


def _event_type(event: Any) -> str:
    if isinstance(event, dict):
        return str(event.get("type") or "")
    return ""


def _extract_text_delta(event: dict[str, Any]) -> str:
    for key in ("delta", "text", "transcript"):
        value = event.get(key)
        if isinstance(value, str):
            return value
    return ""


def _extract_any_text(value: Any) -> str:
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            if key in {"text", "transcript", "output_text"} and isinstance(item, str):
                parts.append(item)
            elif key == "content" and isinstance(item, str):
                parts.append(item)
            elif isinstance(item, (dict, list)):
                nested = _extract_any_text(item)
                if nested:
                    parts.append(nested)
        return "\n".join(part for part in parts if part)
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := _extract_any_text(item)))
    return ""


def _extract_audio_delta(event: dict[str, Any]) -> str:
    for key in ("delta", "audio"):
        value = event.get(key)
        if isinstance(value, str):
            return value
    audio = event.get("audio")
    if isinstance(audio, dict):
        for key in ("delta", "data"):
            value = audio.get(key)
            if isinstance(value, str):
                return value
    return ""


def _safe_event_preview(event: dict[str, Any]) -> dict[str, Any]:
    preview: dict[str, Any] = {}
    for key, value in event.items():
        if key in {"audio", "delta"} and isinstance(value, str) and len(value) > 240:
            preview[key] = f"<base64 {len(value)} chars>"
        elif isinstance(value, str) and len(value) > 1000:
            preview[key] = value[:1000] + "...<truncated>"
        elif isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False)
            preview[key] = json.loads(text[:1000]) if len(text) <= 1000 else text[:1000] + "...<truncated>"
        else:
            preview[key] = value
    return preview


@router.websocket("/ws/realtime")
async def realtime_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    sid = uuid.uuid4().hex[:12]

    upstream = None
    upstream_reader: asyncio.Task[None] | None = None
    current_response = ""
    committed_audio = False
    response_active = False
    sample_rate = QWEN_REALTIME_SAMPLE_RATE
    response_timeout: asyncio.Task[None] | None = None
    system_prompt = ""
    upstream_url = realtime_ws_url()
    upstream_headers: dict[str, str] = {}

    async def send_client(etype: str, **payload: Any) -> None:
        await websocket.send_text(_json({"session_id": sid, "type": etype, **payload}))

    async def send_upstream(payload: dict[str, Any]) -> None:
        if upstream is None:
            raise RuntimeError("upstream realtime websocket is not connected")
        await upstream.send(_json(payload))

    async def start_response_timeout() -> None:
        nonlocal response_active
        try:
            await asyncio.sleep(RESPONSE_TIMEOUT_S)
            if response_active:
                response_active = False
                await send_client(
                    "error",
                    message=f"upstream response timeout after {RESPONSE_TIMEOUT_S}s; check vLLM-Omni server logs",
                )
        except asyncio.CancelledError:
            raise

    async def close_upstream() -> None:
        nonlocal upstream, upstream_reader
        current = upstream
        upstream = None
        if current is not None:
            try:
                await current.close()
            except Exception:
                pass
        upstream_reader = None

    async def open_upstream_for_turn() -> None:
        nonlocal upstream, upstream_reader
        if upstream is not None:
            return

        upstream = await _connect_realtime(upstream_url, upstream_headers)
        upstream_reader = asyncio.create_task(read_upstream(upstream))

        update_event: dict[str, Any] = {
            "type": "session.update",
            "model": QWEN_REALTIME_MODEL,
        }
        if system_prompt:
            update_event["instructions"] = system_prompt
        if QWEN_REALTIME_VOICE:
            update_event["voice"] = QWEN_REALTIME_VOICE

        await send_upstream(update_event)
        await send_upstream({"type": "input_audio_buffer.commit", "final": False})

    async def read_upstream(conn) -> None:
        nonlocal current_response, response_active, upstream, upstream_reader, response_timeout
        try:
            async for raw in conn:
                try:
                    event = json.loads(raw)
                except Exception:
                    await send_client("upstream_raw", data=str(raw)[:1000])
                    continue

                etype = _event_type(event)

                if etype in {"response.text.delta", "response.output_text.delta", "response.audio_transcript.delta"}:
                    delta = _extract_text_delta(event)
                    current_response += delta
                    await send_client("text_delta", text=delta, upstream_type=etype)
                elif etype in {"response.audio.delta", "response.output_audio.delta"}:
                    delta = _extract_audio_delta(event)
                    if delta:
                        await send_client("audio_delta", audio=delta, sample_rate_hz=event.get("sample_rate_hz"))
                elif etype in {"transcription.delta"}:
                    delta = _extract_text_delta(event)
                    current_response += delta
                    await send_client("text_delta", text=delta, upstream_type=etype)
                elif etype in {"transcription.done"}:
                    final_text = event.get("text") or current_response or _extract_any_text(event)
                    current_response = str(final_text or "")
                    await send_client("transcription_done", text=current_response)
                elif etype in {"response.done", "response.completed"}:
                    final_text = current_response or _extract_any_text(event)
                    current_response = final_text
                    response_active = False
                    await send_client("response_done", text=final_text)
                elif etype == "response.audio.done":
                    response_active = False
                    await send_client("response_done", text=current_response, has_audio=event.get("has_audio"))
                    if response_timeout is not None:
                        response_timeout.cancel()
                        response_timeout = None
                    if upstream is conn:
                        await close_upstream()
                    break
                elif etype == "error":
                    response_active = False
                    await send_client("error", message=json.dumps(event, ensure_ascii=False)[:1500])
                else:
                    text = _extract_any_text(event)
                    if text and text not in current_response:
                        current_response += text
                        await send_client("text_delta", text=text, upstream_type=etype)
                    audio_delta = _extract_audio_delta(event)
                    if audio_delta and etype:
                        await send_client("audio_delta", audio=audio_delta, sample_rate_hz=event.get("sample_rate_hz"))
                    await send_client("upstream_event", upstream_type=etype, data=_safe_event_preview(event))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            response_active = False
            logger.exception("realtime upstream reader failed")
            try:
                await send_client("error", message=f"upstream read failed: {exc}")
            except Exception:
                pass
        finally:
            if upstream is conn:
                upstream = None
                upstream_reader = None

    try:
        while True:
            msg = await websocket.receive()

            if msg.get("text") is not None:
                data = json.loads(msg["text"])
                etype = data.get("type")

                if etype == "start":
                    system_prompt = str(data.get("system_prompt") or "").strip()
                    sample_rate = int(data.get("sample_rate") or QWEN_REALTIME_SAMPLE_RATE)
                    upstream_url = str(data.get("url") or realtime_ws_url())
                    if "model=" not in upstream_url:
                        joiner = "&" if "?" in upstream_url else "?"
                        upstream_url = f"{upstream_url}{joiner}model={QWEN_REALTIME_MODEL}"

                    upstream_headers = {}
                    if QWEN_REALTIME_API_KEY:
                        upstream_headers["Authorization"] = f"Bearer {QWEN_REALTIME_API_KEY}"

                    await open_upstream_for_turn()
                    await send_client(
                        "ready",
                        model=QWEN_REALTIME_MODEL,
                        upstream_url=upstream_url,
                        sample_rate=sample_rate,
                    )

                elif etype == "user_end":
                    if upstream is None:
                        await send_client("error", message="not connected")
                        continue
                    if response_active:
                        await send_client("error", message="response is still active")
                        continue
                    if not committed_audio:
                        await send_client("error", message="no audio captured for this turn")
                        continue

                    current_response = ""
                    committed_audio = False
                    response_active = True
                    await send_upstream({"type": "input_audio_buffer.commit", "final": True})
                    if QWEN_REALTIME_SEND_RESPONSE_CREATE:
                        await send_upstream({"type": "response.create"})
                    await send_client("response_started")
                    if response_timeout is not None:
                        response_timeout.cancel()
                    response_timeout = asyncio.create_task(start_response_timeout())

                elif etype == "stop":
                    break

                elif etype == "ping":
                    await send_client("pong")

            elif msg.get("bytes") is not None:
                if upstream is None or response_active:
                    if response_active:
                        continue
                    await open_upstream_for_turn()
                chunk = msg["bytes"]
                if not chunk:
                    continue
                committed_audio = True
                await send_upstream({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode("ascii"),
                })

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.exception("realtime websocket failed")
        try:
            await send_client("error", message=str(exc))
        except Exception:
            pass
    finally:
        if upstream_reader is not None:
            upstream_reader.cancel()
            try:
                await upstream_reader
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if upstream is not None:
            try:
                await upstream.close()
            except Exception:
                pass
        if response_timeout is not None:
            response_timeout.cancel()

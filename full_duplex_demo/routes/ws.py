import asyncio
import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from full_duplex_demo.config import (
    CHUNK_DURATION_S,
    LISTEN_SPEAK_PROMPT,
    MAX_RESPONSE_CHARS,
    QWEN_API_BASE,
    QWEN_MODEL,
    ROLLING_AUDIO_CONTEXT_S,
    SYSTEM_PROMPT,
    TEXT_HISTORY_TURNS,
)
from full_duplex_demo.services.audio_utils import wav_bytes_from_float32_chunks
from full_duplex_demo.services.qwen_client import (
    build_omniflow_payload,
    build_listen_speak_payload,
    complete_json_sync,
    extract_message_text,
    extract_stream_delta,
    stream_json_sync,
)

router = APIRouter()
logger = logging.getLogger("uvicorn.error")


def _clean_fragment(text: str, max_chars: int) -> str:
    """Normalize one short visible assistant fragment."""
    cleaned = text.replace("\r", "\n").split("\n", 1)[0].strip()
    for prefix in ("assistant:", "Assistant:", "助手：", "助手:", "AI：", "AI:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    cleaned = cleaned.strip(" `\"'“”‘’")
    cleaned = cleaned[:max_chars].strip()
    return cleaned.rstrip("，,。.!！？?；;：:")


def _payload_preview(payload: dict[str, Any]) -> str:
    """Render the user-visible model input without dumping audio base64."""
    lines: list[str] = []

    for idx, message in enumerate(payload.get("messages") or []):
        if not isinstance(message, dict):
            continue

        role = message.get("role", "?")
        if role == "system":
            continue

        content = message.get("content")
        lines.append(f"{role}:")

        if isinstance(content, str):
            lines.append(content)
            continue

        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue

                ptype = part.get("type", "?")
                if ptype == "text":
                    lines.append(str(part.get("text", "")))
                elif ptype == "audio_url":
                    lines.append("[用户输入音频]")
                elif ptype == "audio":
                    lines.append("[用户输入音频]")
            continue

    return "\n".join(lines)


def _parse_listen_speak(text: str) -> str:
    cleaned = text.strip().lower()
    if "speak" in cleaned:
        return "speak"
    if "listen" in cleaned:
        return "listen"
    return "listen"


# ═══════════════════════════════════════════════════════════════
#  WebSocket handler — all logic on the server side
# ═══════════════════════════════════════════════════════════════


@router.websocket("/ws/full_duplex")
async def full_duplex_ws(websocket: WebSocket) -> None:
    await websocket.accept()

    sid = uuid.uuid4().hex[:12]
    source_sr = 48000

    # --- mutable state (closure for inner functions) ---
    audio_buf: list[bytes] = []       # PCM for current window
    judge_audio_buf: list[bytes] = [] # PCM for the current unfinished user utterance
    window_ms: float = 0.0
    chunk_idx: int = 0
    flow_history: list[dict[str, Any]] = []  # prior chat messages, kept unchanged
    inferring: bool = False           # guards against overlapping requests

    # --- helpers ---

    async def send_event(etype: str, **kw: Any) -> None:
        try:
            await websocket.send_text(json.dumps(
                {"session_id": sid, "type": etype, **kw},
                ensure_ascii=False,
            ))
        except Exception:
            pass

    def send_fire_and_forget(etype: str, **kw: Any) -> None:
        """Send from sync context inside async task (best-effort)."""
        try:
            asyncio.create_task(websocket.send_text(json.dumps(
                {"session_id": sid, "type": etype, **kw},
                ensure_ascii=False,
            )))
        except Exception:
            pass

    # --- per-chunk inference (closure over inferring, history, send_*) ---

    async def run_chunk_inference(
        chunk_idx_local: int,
        audio_chunks: list[bytes],
    ) -> None:
        nonlocal inferring

        t0 = time.perf_counter()

        current_audio_ms = sum((len(chunk) / 4) / source_sr * 1000 for chunk in audio_chunks)

        # 1. PCM → WAV. ``wav`` is the current window for reply history;
        # ``judge_wav`` is the accumulated unfinished utterance for turn-taking.
        try:
            wav = await asyncio.to_thread(
                wav_bytes_from_float32_chunks,
                audio_chunks,
                source_sr,
                16000,
            )
            judge_wav = await asyncio.to_thread(
                wav_bytes_from_float32_chunks,
                [*judge_audio_buf, *audio_chunks],
                source_sr,
                16000,
            )
        except Exception as exc:
            await send_event("error", message=f"audio encode failed: {exc}")
            inferring = False
            return

        # 2. Single multimodal inference. Prompt must make this a reply task,
        # not an ASR task.
        payload = build_omniflow_payload(
            model=QWEN_MODEL,
            wav_bytes=wav,
            system_prompt=SYSTEM_PROMPT,
            flow_history=flow_history,
            max_tokens=MAX_RESPONSE_CHARS,
            stream=True,
        )
        preview = _payload_preview(payload)
        logger.info("chunk=%d model_input:\n%s", chunk_idx_local, preview)
        await send_event("model_input",
                         chunk_index=chunk_idx_local,
                         text=preview)

        try:
            judge_payload = build_listen_speak_payload(
                model=QWEN_MODEL,
                wav_bytes=judge_wav,
                system_prompt=LISTEN_SPEAK_PROMPT,
            )
            judge_resp = await asyncio.to_thread(
                complete_json_sync,
                f"{QWEN_API_BASE}/chat/completions",
                judge_payload,
            )
            listen_speak = _parse_listen_speak(extract_message_text(judge_resp))
        except Exception as exc:
            await send_event("error", message=f"listen/speak judge failed: {exc}")
            inferring = False
            return

        await send_event("listen_speak",
                         chunk_index=chunk_idx_local,
                         state=listen_speak)

        current_user_message = payload["messages"][-1]

        if listen_speak == "listen":
            flow_history.append(current_user_message)
            judge_audio_buf.extend(audio_chunks)
            while len(flow_history) > TEXT_HISTORY_TURNS * 2:
                flow_history.pop(0)

            latency_ms = int((time.perf_counter() - t0) * 1000)
            await send_event("chunk_done",
                             chunk_index=chunk_idx_local,
                             text="",
                             latency_ms=latency_ms,
                             ttft_ms=None,
                             listen_speak=listen_speak)
            inferring = False
            return

        # 3. Stream from Qwen
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def push(item: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, item)

        stream_task = asyncio.create_task(
            asyncio.to_thread(
                stream_json_sync,
                f"{QWEN_API_BASE}/chat/completions",
                payload,
                push,
            )
        )

        text_parts: list[str] = []
        visible_chars = 0
        ttft_ms: int | None = None

        try:
            while True:
                item = await queue.get()
                itype = item.get("type")

                if itype == "done":
                    break
                if itype == "ttft":
                    ttft_ms = item.get("ttft_ms")
                    continue
                if itype == "error":
                    await send_event("error", message=item.get("message", "stream error")[:500])
                    inferring = False
                    return

                chunk_data = item.get("data")
                if not isinstance(chunk_data, dict):
                    continue

                delta = extract_stream_delta(chunk_data)
                if delta and visible_chars < MAX_RESPONSE_CHARS:
                    remaining = MAX_RESPONSE_CHARS - visible_chars
                    visible_delta = delta[:remaining]
                    visible_chars += len(visible_delta)
                    text_parts.append(visible_delta)
                    send_fire_and_forget("text_delta",
                                         chunk_index=chunk_idx_local,
                                         text=visible_delta)

        except asyncio.CancelledError:
            inferring = False
            return
        finally:
            await stream_task

        # 4. Finalize
        full_text = _clean_fragment("".join(text_parts), MAX_RESPONSE_CHARS)
        latency_ms = int((time.perf_counter() - t0) * 1000)

        logger.info(
            "chunk=%d text=%r latency=%dms ttft=%sms",
            chunk_idx_local, full_text or "", latency_ms, ttft_ms,
        )

        flow_history.append(current_user_message)
        flow_history.append({
            "role": "assistant",
            "content": full_text or "嗯",
        })
        judge_audio_buf.clear()
        while len(flow_history) > TEXT_HISTORY_TURNS * 2:
            flow_history.pop(0)

        await send_event("chunk_done",
                         chunk_index=chunk_idx_local,
                         text=full_text or "",
                         latency_ms=latency_ms,
                         ttft_ms=ttft_ms,
                         listen_speak=listen_speak)

        inferring = False

    # ═══════════════════════════════════════════════════════
    #  Main message loop
    # ═══════════════════════════════════════════════════════

    await send_event("ready")

    try:
        while True:
            try:
                msg = await websocket.receive()
            except RuntimeError as exc:
                if "disconnect" in str(exc):
                    break
                raise

            if msg.get("text") is not None:
                data = json.loads(msg["text"])
                etype = data.get("type", "")

                if etype == "hello":
                    source_sr = int(data.get("sampleRate", 48000))
                    await send_event("hello_ok",
                                     sample_rate=source_sr,
                                     chunk_duration_s=CHUNK_DURATION_S,
                                     rolling_audio_context_s=ROLLING_AUDIO_CONTEXT_S,
                                     max_response_chars=MAX_RESPONSE_CHARS,
                                     model=QWEN_MODEL)

                elif etype == "ping":
                    await send_event("pong")

            elif msg.get("bytes") is not None:
                chunk = msg["bytes"]
                if len(chunk) < 4:
                    continue

                audio_buf.append(chunk)
                chunk_ms = (len(chunk) / 4) / source_sr * 1000
                window_ms += chunk_ms

                # ── 1-second window ready ──
                if window_ms >= CHUNK_DURATION_S * 1000:
                    if inferring:
                        # Previous inference still running — keep audio,
                        # extend window without losing data
                        window_ms -= 200
                        continue

                    chunk_idx += 1
                    window_audio = list(audio_buf)
                    audio_buf.clear()
                    window_ms = 0.0
                    inferring = True

                    await send_event("chunk_processing",
                                     chunk_index=chunk_idx)

                    asyncio.create_task(
                        run_chunk_inference(chunk_idx, window_audio)
                    )

    except WebSocketDisconnect:
        pass
    finally:
        inferring = False

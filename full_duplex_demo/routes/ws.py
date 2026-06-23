import asyncio
import json
import logging
import struct
import time
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from full_duplex_demo.config import (
    CHUNK_DURATION_S,
    LISTEN_SPEAK_PROMPT,
    LISTEN_SPEAK_PROMPT_2,
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
    extract_listen_speak_text,
    extract_stream_delta,
    stream_json_sync,
)

router = APIRouter()
logger = logging.getLogger("uvicorn.error")
INTERRUPT_RMS_THRESHOLD = 0.003


def _clean_fragment(text: str, max_chars: int) -> str:
    """Normalize one short visible assistant fragment."""
    cleaned = _visible_answer_text(text, final=True).replace("\r", "\n").split("\n", 1)[0].strip()
    for prefix in ("assistant:", "Assistant:", "助手：", "助手:", "AI：", "AI:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    cleaned = cleaned.strip(" `\"'“”‘’")
    cleaned = cleaned[:max_chars].strip()
    return cleaned


def _strip_answer_tags(text: str) -> str:
    return text.replace("<AnswerStart>", "").replace("<AnswerEnd>", "")


def _visible_answer_text(text: str, *, final: bool = False) -> str:
    visible = _strip_answer_tags(text)
    if final:
        return visible

    # Streaming deltas can split tags, e.g. "<Answer" then "Start>" later.
    # Do not leak an unfinished tag into the visible conversation.
    for tag in ("<AnswerStart>", "<AnswerEnd>"):
        max_prefix_len = min(len(tag) - 1, len(visible))
        for prefix_len in range(max_prefix_len, 0, -1):
            if visible.endswith(tag[:prefix_len]):
                return visible[:-prefix_len]
    return visible


def _has_answer_end(text: str) -> bool:
    return "<AnswerEnd>" in text


def _float32_rms(chunks: list[bytes]) -> float:
    total = 0.0
    count = 0
    for chunk in chunks:
        usable = len(chunk) - (len(chunk) % 4)
        for idx in range(0, usable, 4):
            value = struct.unpack("<f", chunk[idx:idx + 4])[0]
            total += value * value
            count += 1
    if count == 0:
        return 0.0
    return (total / count) ** 0.5


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


def _assistant_message(text: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": [
            {"type": "text", "text": text},
        ],
    }



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
    assistant_has_floor: bool = False # true after AI starts/continues speaking

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
        nonlocal assistant_has_floor, inferring

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
            judge_source_chunks = audio_chunks if assistant_has_floor else [*judge_audio_buf, *audio_chunks]
            judge_wav = await asyncio.to_thread(
                wav_bytes_from_float32_chunks,
                judge_source_chunks,
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
            max_tokens=MAX_RESPONSE_CHARS + 32,
            stream=True,
        )
        preview = _payload_preview(payload)
        logger.info("chunk=%d model_input:\n%s", chunk_idx_local, preview)
        await send_event("model_input",
                         chunk_index=chunk_idx_local,
                         text=preview)

        # 3. Run turn-taking judgement and reply generation in parallel.
        # Reply deltas stay buffered until the judgement returns "speak".
        judging_assistant_interrupt = assistant_has_floor
        judge_prompt = LISTEN_SPEAK_PROMPT_2 if judging_assistant_interrupt else LISTEN_SPEAK_PROMPT
        interrupt_rms = _float32_rms(audio_chunks)
        skip_judge_as_silence = judging_assistant_interrupt and interrupt_rms < INTERRUPT_RMS_THRESHOLD
        judge_payload = build_listen_speak_payload(
            model=QWEN_MODEL,
            wav_bytes=judge_wav,
            system_prompt=judge_prompt,
            user_instruction=(
                "当前 AI 正在输出。请只根据这段用户音频判断：用户是否正在说话并打断 AI。"
                if judging_assistant_interrupt
                else "请判断这段用户音频是否已经说完整，是否轮到 AI 说话。"
            ),
        )

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def push(item: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, item)

        judge_task: asyncio.Task[dict[str, Any]] | None = None
        if not skip_judge_as_silence:
            judge_task = asyncio.create_task(
                asyncio.to_thread(
                    complete_json_sync,
                    f"{QWEN_API_BASE}/chat/completions",
                    judge_payload,
                )
            )
        stream_task = asyncio.create_task(
            asyncio.to_thread(
                stream_json_sync,
                f"{QWEN_API_BASE}/chat/completions",
                payload,
                push,
            )
        )

        buffered_deltas: list[tuple[str, str]] = []
        raw_text_parts: list[str] = []
        emitted_visible_text = ""
        ttft_ms: int | None = None
        listen_speak: str | None = "speak" if skip_judge_as_silence else None
        reply_done = False

        def collect_visible_delta(delta: str) -> str:
            nonlocal emitted_visible_text
            raw_text_parts.append(delta)
            visible_text = "".join(raw_text_parts)
            if len(visible_text) <= len(emitted_visible_text):
                return ""
            visible_delta = visible_text[len(emitted_visible_text):]
            emitted_visible_text = visible_text
            return visible_delta

        try:
            while listen_speak is None:
                get_task = asyncio.create_task(queue.get())
                done, pending = await asyncio.wait(
                    {judge_task, get_task} if judge_task is not None else {get_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if judge_task is not None and judge_task in done:
                    judge_resp = judge_task.result()
                    listen_speak = extract_listen_speak_text(judge_resp)

                if get_task in done:
                    item = get_task.result()
                    itype = item.get("type")

                    if itype == "done":
                        reply_done = True
                    elif itype == "ttft":
                        ttft_ms = item.get("ttft_ms")
                    elif itype == "error":
                        await send_event("error", message=item.get("message", "stream error")[:500])
                        stream_task.cancel()
                        inferring = False
                        return
                    else:
                        chunk_data = item.get("data")
                        if isinstance(chunk_data, dict):
                            delta = extract_stream_delta(chunk_data)
                            if delta:
                                visible_delta = collect_visible_delta(delta)
                                if visible_delta or delta:
                                    buffered_deltas.append((visible_delta, delta))

                if get_task in pending:
                    get_task.cancel()
        except Exception as exc:
            stream_task.cancel()
            await send_event("error", message=f"parallel inference failed: {exc}")
            inferring = False
            return

        await send_event("listen_speak",
                         chunk_index=chunk_idx_local,
                         state=listen_speak,
                         judge_mode="interrupt" if judging_assistant_interrupt else "turn")

        current_user_message = payload["messages"][-1]

        if listen_speak == "listen":
            # If prompt2 detects a user interruption while AI is speaking, keep this
            # audio as the start of the user's unfinished turn. The next chunk will
            # be judged with prompt1 until that user turn becomes complete.
            assistant_has_floor = False
            stream_task.cancel()
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

        judge_audio_buf.clear()
        assistant_has_floor = True

        for visible_delta, raw_delta in buffered_deltas:
            send_fire_and_forget("text_delta",
                                 chunk_index=chunk_idx_local,
                                 text=visible_delta,
                                 raw_text=raw_delta)

        try:
            while not reply_done:
                item = await queue.get()
                itype = item.get("type")

                if itype == "done":
                    reply_done = True
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
                if delta:
                    visible_delta = collect_visible_delta(delta)
                    if visible_delta:
                        send_fire_and_forget("text_delta",
                                             chunk_index=chunk_idx_local,
                                             text=visible_delta,
                                             raw_text=delta)

        except asyncio.CancelledError:
            inferring = False
            return
        finally:
            await stream_task

        # 4. Finalize
        raw_full_text = "".join(raw_text_parts)
        full_text = raw_full_text.strip()
        answer_done = _has_answer_end(raw_full_text)
        latency_ms = int((time.perf_counter() - t0) * 1000)

        logger.info(
            "chunk=%d text=%r latency=%dms ttft=%sms",
            chunk_idx_local, full_text or "", latency_ms, ttft_ms,
        )

        flow_history.append(current_user_message)
        raw_history_text = raw_full_text.strip()
        if raw_history_text:
            flow_history.append(_assistant_message(raw_history_text))
            assistant_has_floor = not answer_done
        elif answer_done:
            assistant_has_floor = False
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

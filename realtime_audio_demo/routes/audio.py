import asyncio
import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from realtime_audio_demo.config import (
    CAPTURE_DIR,
    DEFAULT_FINAL_PROMPT,
    DEFAULT_PREFILL_MS,
    FINAL_MAX_TOKENS,
    PREFILL_MODE,
    QWEN_API_BASE,
    QWEN_MODEL,
    STREAM_FINAL_OUTPUT,
    normalize_model_name,
)
from realtime_audio_demo.events import send_event
from realtime_audio_demo.services.qwen import (
    build_chat_payload,
    extract_model_output,
    extract_stream_delta,
    normalize_history,
    post_json,
    stream_json_sync,
)
from realtime_audio_demo.services.skill_loader import compose_realtime_prompt
from realtime_audio_demo.services.text_chat import request_text_completion
from realtime_audio_demo.sessions import AudioSession
from realtime_audio_demo.utils.audio import float32_sample_count, wav_bytes_from_float32_chunks


router = APIRouter()


@router.post("/api/realtime/text")
@router.post("/api/chatbox/text")
async def chatbox_text(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        return JSONResponse({"message": f"bad json: {exc}"}, status_code=400)

    if not isinstance(payload, dict):
        return JSONResponse({"message": "json body must be an object"}, status_code=400)

    user_text = str(payload.get("text") or "").strip()
    if not user_text:
        return JSONResponse({"message": "text is required"}, status_code=400)

    model = normalize_model_name(payload.get("model") or QWEN_MODEL)
    skill_names = normalize_skill_names(payload.get("skillNames") or [])
    prompt, used_skills, missing_skills = compose_realtime_prompt(
        payload.get("prompt") or DEFAULT_FINAL_PROMPT,
        skill_names,
    )
    history = normalize_history(payload.get("history"))
    output_audio = bool(payload.get("outputAudio"))
    result, status_code = await request_text_completion(
        model=model,
        text=user_text,
        prompt=prompt,
        history=history,
        max_tokens=FINAL_MAX_TOKENS,
        output_audio=output_audio,
    )
    if status_code >= 400:
        return JSONResponse(result, status_code=status_code)

    result.update({"skills": used_skills, "missing_skills": missing_skills, "output_audio": output_audio})
    return JSONResponse(result)


@router.websocket("/ws/audio")
async def websocket_audio(websocket: WebSocket) -> None:
    await websocket.accept()
    session = AudioSession(websocket=websocket)
    session.prefill_task = asyncio.create_task(prefill_worker(session))
    await send_event(session, "ready", {"session_id": session.session_id})

    try:
        while True:
            try:
                message = await websocket.receive()
            except RuntimeError as exc:
                if "disconnect message has been received" in str(exc):
                    break
                raise
            if message.get("text") is not None:
                await handle_control_message(session, message["text"])
            elif message.get("bytes") is not None:
                await handle_audio_chunk(session, message["bytes"])
    except WebSocketDisconnect:
        session.stopped = True
    finally:
        if session.prefill_task:
            session.prefill_task.cancel()
            try:
                await session.prefill_task
            except asyncio.CancelledError:
                pass


async def handle_control_message(session: AudioSession, text: str) -> None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        await send_event(session, "error", {"message": f"bad json: {exc}"})
        return

    event_type = payload.get("type")
    if event_type == "start":
        session.source_sample_rate = int(payload.get("sampleRate") or session.source_sample_rate)
        session.prefill_ms = int(payload.get("prefillMs") or DEFAULT_PREFILL_MS)
        session.model = normalize_model_name(payload.get("model") or QWEN_MODEL)
        session.output_audio = bool(payload.get("outputAudio"))
        skill_names = normalize_skill_names(payload.get("skillNames") or [])
        session.prompt, used_skills, missing_skills = compose_realtime_prompt(
            payload.get("prompt") or session.prompt,
            skill_names,
        )
        session.history = normalize_history(payload.get("history"))
        await send_event(
            session,
            "started",
            {
                "session_id": session.session_id,
                "source_sample_rate": session.source_sample_rate,
                "target_sample_rate": session.target_sample_rate,
                "prefill_ms": session.prefill_ms,
                "model": session.model,
                "qwen_api_base": QWEN_API_BASE,
                "history_messages": len(session.history),
                "skills": used_skills,
                "missing_skills": missing_skills,
                "output_audio": session.output_audio,
            },
        )
    elif event_type == "stop":
        session.stopped = True
        await send_event(session, "finalizing", {"chunks": len(session.chunks)})
        await finalize_session(session)
    elif event_type == "ping":
        await send_event(session, "pong", {"ts": time.time()})
    else:
        await send_event(session, "error", {"message": f"unknown control event: {event_type}"})


def normalize_skill_names(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def output_modalities(output_audio: bool) -> list[str]:
    return ["text", "audio"] if output_audio else ["text"]


async def handle_audio_chunk(session: AudioSession, pcm_float32_bytes: bytes) -> None:
    if session.stopped:
        return
    if len(pcm_float32_bytes) < 4:
        return

    session.chunks.append(pcm_float32_bytes)
    chunk_index = len(session.chunks)
    duration_ms = int(float32_sample_count(pcm_float32_bytes) * 1000 / session.source_sample_rate)

    await send_event(
        session,
        "chunk_received",
        {
            "chunk_index": chunk_index,
            "duration_ms": duration_ms,
            "queued_prefill": PREFILL_MODE != "off",
        },
    )

    if PREFILL_MODE != "off":
        await session.prefill_queue.put((chunk_index, pcm_float32_bytes))


async def prefill_worker(session: AudioSession) -> None:
    while True:
        chunk_index, chunk = await session.prefill_queue.get()
        while not session.prefill_queue.empty():
            session.prefill_queue.task_done()
            chunk_index, chunk = await session.prefill_queue.get()
        try:
            await run_prefill_probe(session, chunk_index, chunk)
        except Exception as exc:
            await send_event(
                session,
                "prefill_error",
                {
                    "chunk_index": chunk_index,
                    "message": str(exc),
                },
            )
        finally:
            session.prefill_queue.task_done()


async def run_prefill_probe(session: AudioSession, chunk_index: int, chunk: bytes) -> None:
    start = time.perf_counter()
    if PREFILL_MODE == "cumulative_probe":
        probe_chunks = session.chunks[:chunk_index]
        prompt = (
            "这是实时语音交互中截至当前时刻的音频前缀。"
            "请只完成音频理解的预热/预填充探测，不要回答用户问题。"
            "只输出 OK。"
        )
    else:
        probe_chunks = [chunk]
        prompt = (
            "这是实时语音交互中的一个 600ms 音频 chunk。"
            "请只完成音频理解的预热/预填充探测，不要回答用户问题。"
            "只输出 OK。"
        )

    wav = wav_bytes_from_float32_chunks(probe_chunks, session.source_sample_rate, session.target_sample_rate)
    payload = build_chat_payload(
        session.model,
        wav,
        prompt,
        history=session.history,
        max_tokens=1,
        modalities=["text"],
    )

    response = await post_json(f"{QWEN_API_BASE}/chat/completions", payload)
    latency_ms = int((time.perf_counter() - start) * 1000)

    if response.status_code >= 400:
        await send_event(
            session,
            "prefill_error",
            {
                "chunk_index": chunk_index,
                "status_code": response.status_code,
                "message": response.text[:1000],
                "latency_ms": latency_ms,
            },
        )
        return

    await send_event(
        session,
        "prefill_ok",
        {
            "chunk_index": chunk_index,
            "latency_ms": latency_ms,
            "mode": PREFILL_MODE,
            "probe_chunks": len(probe_chunks),
            "note": "OpenAI-compatible prefill probe; native KV cache reuse requires server-side support.",
        },
    )


async def finalize_session(session: AudioSession) -> None:
    if not session.chunks:
        await send_event(session, "error", {"message": "no audio chunks received"})
        return

    wav = wav_bytes_from_float32_chunks(session.chunks, session.source_sample_rate, session.target_sample_rate)
    input_path = CAPTURE_DIR / f"{session.session_id}_input.wav"
    input_path.write_bytes(wav)

    payload = build_chat_payload(
        session.model,
        wav,
        session.prompt,
        history=session.history,
        max_tokens=FINAL_MAX_TOKENS,
        modalities=output_modalities(session.output_audio),
    )
    start = time.perf_counter()

    if STREAM_FINAL_OUTPUT:
        await stream_final_session(session, payload, input_path, start)
        return

    try:
        response = await post_json(f"{QWEN_API_BASE}/chat/completions", payload)
    except Exception as exc:
        await send_event(
            session,
            "final_error",
            {
                "message": str(exc),
                "saved_input_wav": str(input_path),
            },
        )
        return

    latency_ms = int((time.perf_counter() - start) * 1000)
    if response.status_code >= 400:
        await send_event(
            session,
            "final_error",
            {
                "status_code": response.status_code,
                "message": response.text[:2000],
                "saved_input_wav": str(input_path),
                "latency_ms": latency_ms,
            },
        )
        return

    data = response.json()
    parsed = extract_model_output(data)
    await send_event(
        session,
        "final_result",
        {
            "text": parsed["text"],
            "audio_data_url": parsed["audio_data_url"],
            "raw_response": data,
            "saved_input_wav": str(input_path),
            "latency_ms": latency_ms,
        },
    )


async def stream_final_session(
    session: AudioSession,
    payload: dict[str, Any],
    input_path: Path,
    start: float,
) -> None:
    payload = {**payload, "stream": True}
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def push(item: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, item)

    task = asyncio.create_task(
        asyncio.to_thread(stream_json_sync, f"{QWEN_API_BASE}/chat/completions", payload, push)
    )

    text_parts: list[str] = []
    audio_count = 0
    try:
        while True:
            item = await queue.get()
            if item.get("type") == "done":
                break
            if item.get("type") == "error":
                await send_event(
                    session,
                    "final_error",
                    {
                        "status_code": item.get("status_code"),
                        "message": item.get("message", "stream request failed")[:2000],
                        "saved_input_wav": str(input_path),
                        "latency_ms": int((time.perf_counter() - start) * 1000),
                    },
                )
                return

            chunk = item.get("data")
            if not isinstance(chunk, dict):
                continue
            delta = extract_stream_delta(chunk)
            if delta["text"]:
                text_parts.append(delta["text"])
                await send_event(session, "final_text_delta", {"text": delta["text"]})
            if delta["audio_data_url"]:
                audio_count += 1
                await send_event(
                    session,
                    "final_audio_delta",
                    {
                        "audio_data_url": delta["audio_data_url"],
                        "audio_index": audio_count,
                    },
                )
    finally:
        await task

    text = "".join(text_parts).strip() or None
    await send_event(
        session,
        "final_result",
        {
            "text": text,
            "audio_data_url": None,
            "audio_chunks": audio_count,
            "raw_response": None,
            "saved_input_wav": str(input_path),
            "latency_ms": int((time.perf_counter() - start) * 1000),
        },
    )

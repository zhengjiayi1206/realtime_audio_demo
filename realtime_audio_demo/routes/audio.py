import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from realtime_audio_demo.config import (
    CAPTURE_DIR,
    DEFAULT_PREFILL_MS,
    FINAL_MAX_TOKENS,
    PREFILL_MODE,
    QWEN_API_BASE,
    QWEN_MODEL,
    SILERO_VAD_ENABLED,
    STREAM_FINAL_OUTPUT,
    normalize_model_name,
)
from realtime_audio_demo.events import send_event
from realtime_audio_demo.services.component_tools import (
    call_component_tool,
    extract_component_call,
    format_component_result,
)
from realtime_audio_demo.services.intent_skill_router import (
    complete_intent_target,
    extract_json_object,
    repair_intent_json,
    select_skill_for_intent,
)
from realtime_audio_demo.services.output_filter import normalize_assistant_output
from realtime_audio_demo.services.qwen import (
    build_chat_payload,
    extract_model_output,
    extract_stream_delta,
    normalize_history,
    post_json,
    stream_json_sync,
)
from realtime_audio_demo.services.skill_loader import compose_realtime_prompt
from realtime_audio_demo.services.silero_vad import SileroVadConfig, SileroVadSession, SileroVadUnavailable
from realtime_audio_demo.services.text_chat import request_text_completion
from realtime_audio_demo.sessions import AudioSession
from realtime_audio_demo.utils.audio import float32_sample_count, wav_bytes_from_float32_chunks


router = APIRouter()
logger = logging.getLogger("uvicorn.error")


SPEECH_PROMPT = (
    "请把用户输入作为语音播报文本。"
    "只按原文朗读，不要解释、不要改写、不要补充任何内容。"
)
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
    prompt, used_skills, missing_skills = compose_realtime_prompt("", skill_names)
    history = normalize_history(payload.get("history"))
    output_audio = bool(payload.get("outputAudio"))
    result, status_code = await request_text_completion(
        model=model,
        text=user_text,
        prompt=prompt,
        history=history,
        max_tokens=FINAL_MAX_TOKENS,
        output_audio=False,
    )
    if status_code >= 400:
        return JSONResponse(result, status_code=status_code)

    intent_json = extract_json_object(result.get("text"))
    json_repair: dict[str, Any] | None = None
    if intent_json is None:
        intent_json, json_repair = await repair_intent_json(model, result.get("text"))
        if intent_json is not None:
            result["text"] = json.dumps(intent_json, ensure_ascii=False, indent=2)
    intent_target = complete_intent_target(intent_json)
    logger.info(
        "chatbox intent route text intent=%s target=%s repaired=%s",
        json.dumps(intent_json, ensure_ascii=False) if intent_json is not None else None,
        json.dumps(intent_target, ensure_ascii=False) if intent_target is not None else None,
        bool(json_repair),
    )
    selected_skill: str | None = None
    skill_selection: dict[str, Any] | None = None
    new_session = False
    if intent_json and intent_target:
        selected_skill, skill_selection = await select_skill_for_intent(model, intent_json)
        if selected_skill:
            routed_skill_names = [selected_skill]
            prompt, used_skills, missing_skills = compose_realtime_prompt("", routed_skill_names)
            routed_user_text = (
                f"用户原始输入：{user_text}\n\n"
                f"意图识别结果：{json.dumps(intent_json, ensure_ascii=False)}"
            )
            routed_result, routed_status_code = await request_text_completion(
                model=model,
                text=routed_user_text,
                prompt=prompt,
                history=[],
                max_tokens=FINAL_MAX_TOKENS,
                output_audio=False,
            )
            if routed_status_code < 400:
                result = routed_result
                skill_names = routed_skill_names
                new_session = True
            else:
                result["skill_routing_error"] = routed_result

    normalized = normalize_assistant_output(result.get("text"))
    result["history_text"] = normalized.history_text
    result["speech_text"] = normalized.speech_text
    result["output_is_json"] = normalized.is_json
    result.update(
        {
            "skills": used_skills,
            "missing_skills": missing_skills,
            "output_audio": output_audio,
            "intent_target": intent_target,
            "json_repair": json_repair,
            "selected_skill": selected_skill,
            "selected_skills": [selected_skill] if selected_skill else [],
            "skill_selection": skill_selection,
            "new_session": new_session,
        }
    )
    return JSONResponse(result)


@router.post("/api/chatbox/speech")
async def chatbox_speech(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        return JSONResponse({"message": f"bad json: {exc}"}, status_code=400)

    if not isinstance(payload, dict):
        return JSONResponse({"message": "json body must be an object"}, status_code=400)

    text = str(payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"audio_data_url": None})

    model = normalize_model_name(payload.get("model") or QWEN_MODEL)
    audio_data_url = await synthesize_speech_audio(model, text)
    return JSONResponse({"audio_data_url": audio_data_url})


async def maybe_route_chatbox_voice_intent(
    *,
    model: str,
    assistant_text: str | None,
    skill_names: list[str],
) -> tuple[str | None, str, dict[str, Any]]:
    meta: dict[str, Any] = {
        "intent_target": None,
        "json_repair": None,
        "selected_skill": None,
        "selected_skills": [],
        "skill_selection": None,
        "new_session": False,
        "skills": [],
        "missing_skills": [],
    }
    user_history_text = "[用户通过语音输入了一条消息]"
    if not assistant_text:
        return assistant_text, user_history_text, meta

    intent_json = extract_json_object(assistant_text)
    if intent_json is None:
        intent_json, meta["json_repair"] = await repair_intent_json(model, assistant_text)
        if intent_json is not None:
            assistant_text = json.dumps(intent_json, ensure_ascii=False, indent=2)

    if intent_json:
        user_history_text = format_voice_user_history(intent_json)

    intent_target = complete_intent_target(intent_json)
    meta["intent_target"] = intent_target
    logger.info(
        "chatbox intent route voice intent=%s target=%s repaired=%s",
        json.dumps(intent_json, ensure_ascii=False) if intent_json is not None else None,
        json.dumps(intent_target, ensure_ascii=False) if intent_target is not None else None,
        bool(meta["json_repair"]),
    )
    if not intent_json or not intent_target:
        return assistant_text, user_history_text, meta

    selected_skill, skill_selection = await select_skill_for_intent(model, intent_json)
    meta["selected_skill"] = selected_skill
    meta["selected_skills"] = [selected_skill] if selected_skill else []
    meta["skill_selection"] = skill_selection
    if not selected_skill:
        return assistant_text, user_history_text, meta

    routed_skill_names = [selected_skill]
    prompt, used_skills, missing_skills = compose_realtime_prompt("", routed_skill_names)
    routed_user_text = (
        f"用户语音输入的结构化意图：{user_history_text}\n\n"
        f"意图识别结果：{json.dumps(intent_json, ensure_ascii=False)}"
    )
    routed_result, routed_status_code = await request_text_completion(
        model=model,
        text=routed_user_text,
        prompt=prompt,
        history=[],
        max_tokens=FINAL_MAX_TOKENS,
        output_audio=False,
    )
    if routed_status_code < 400:
        meta["new_session"] = True
        meta["skills"] = used_skills
        meta["missing_skills"] = missing_skills
        return routed_result.get("text"), user_history_text, meta

    meta["skill_routing_error"] = routed_result
    return assistant_text, user_history_text, meta


def format_voice_user_history(intent_json: dict[str, Any]) -> str:
    content = str(intent_json.get("content") or "").strip()
    intention = str(intent_json.get("intention") or "").strip()
    if not intention:
        target = intent_json.get("target")
        if isinstance(target, dict):
            intention = (
                f"target.name={target.get('name') or ''}#"
                f"target.params1={target.get('params1') or ''}#"
                f"target.params2={target.get('params2') or ''}"
            )
    if content and intention:
        return f"语音输入意图：{intention}；模型追问/确认：{content}"
    if intention:
        return f"语音输入意图：{intention}"
    if content:
        return f"语音输入内容：{content}"
    return "[用户通过语音输入了一条消息]"


@router.post("/api/chatbox/components/call")
async def chatbox_component_call(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        return JSONResponse({"message": f"bad json: {exc}"}, status_code=400)

    if not isinstance(payload, dict):
        return JSONResponse({"message": "json body must be an object"}, status_code=400)

    component = str(payload.get("components") or "").strip()
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        return JSONResponse({"message": "params must be an object"}, status_code=400)

    result, status_code = await call_component_tool(component, params)
    return JSONResponse(result, status_code=status_code)


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


@router.websocket("/ws/vad")
async def websocket_vad(websocket: WebSocket) -> None:
    await websocket.accept()
    session = AudioSession(websocket=websocket)
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
                await handle_vad_control_message(session, message["text"])
            elif message.get("bytes") is not None:
                await handle_vad_monitor_chunk(session, message["bytes"])
    except WebSocketDisconnect:
        session.stopped = True


async def handle_vad_control_message(session: AudioSession, text: str) -> None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        await send_event(session, "error", {"message": f"bad json: {exc}"})
        return

    event_type = payload.get("type")
    if event_type == "start":
        session.source_sample_rate = int(payload.get("sampleRate") or session.source_sample_rate)
        await configure_vad(session, payload.get("vad") or payload)
        await send_event(
            session,
            "vad_monitor_started",
            {
                "session_id": session.session_id,
                "source_sample_rate": session.source_sample_rate,
                "vad": "silero" if session.vad else "none",
            },
        )
    elif event_type == "stop":
        session.stopped = True
        await send_event(session, "vad_monitor_stopped", {"session_id": session.session_id})
    elif event_type == "ping":
        await send_event(session, "pong", {"ts": time.time()})
    else:
        await send_event(session, "error", {"message": f"unknown control event: {event_type}"})


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
        session.route_context = str(payload.get("routeContext") or "").strip()
        session.output_audio = bool(payload.get("outputAudio"))
        session.stream_speech_audio = bool(payload.get("streamSpeechAudio"))
        await configure_vad(session, payload.get("vad"))
        skill_names = normalize_skill_names(payload.get("skillNames") or [])
        session.skill_names = skill_names
        base_prompt = "" if session.route_context == "chatbox" else payload.get("prompt") or session.prompt
        session.prompt, used_skills, missing_skills = compose_realtime_prompt(
            base_prompt,
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
                "stream_speech_audio": session.stream_speech_audio,
                "vad": "silero" if session.vad else "none",
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


async def synthesize_speech_audio(model: str, text: str | None) -> str | None:
    speech_text = (text or "").strip()
    if not speech_text:
        return None

    result, status_code = await request_text_completion(
        model=model,
        text=speech_text,
        prompt=SPEECH_PROMPT,
        history=[],
        max_tokens=FINAL_MAX_TOKENS,
        output_audio=True,
    )
    if status_code >= 400:
        return None
    audio_data_url = result.get("audio_data_url")
    return str(audio_data_url) if audio_data_url else None


def should_buffer_component_candidate(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped:
        return True
    return stripped.startswith("{") or stripped.startswith("```")


class SpeechTextChunker:
    sentence_end_chars = "。！？!?；;\n"
    soft_break_chars = "，,、 "

    def __init__(self, *, min_chars: int = 12, max_chars: int = 80) -> None:
        self.min_chars = min_chars
        self.max_chars = max_chars
        self.buffer = ""

    def add(self, text: str) -> list[str]:
        self.buffer += text
        return self._drain(force=False)

    def flush(self) -> list[str]:
        return self._drain(force=True)

    def _drain(self, *, force: bool) -> list[str]:
        chunks: list[str] = []
        while True:
            sentence_end = self._first_sentence_end()
            if sentence_end >= 0 and (sentence_end + 1 >= self.min_chars or len(self.buffer) >= self.max_chars):
                chunks.append(self._take(sentence_end + 1))
                continue

            if len(self.buffer) >= self.max_chars:
                chunks.append(self._take(self._soft_split_index()))
                continue

            break

        if force and self.buffer.strip():
            chunks.append(self._take(len(self.buffer)))
        return [item for item in chunks if item]

    def _first_sentence_end(self) -> int:
        indexes = [self.buffer.find(char) for char in self.sentence_end_chars if char in self.buffer]
        return min(indexes) if indexes else -1

    def _soft_split_index(self) -> int:
        head = self.buffer[: self.max_chars]
        indexes = [head.rfind(char) for char in self.soft_break_chars]
        index = max(indexes)
        if index >= self.min_chars:
            return index + 1
        return self.max_chars

    def _take(self, size: int) -> str:
        chunk = self.buffer[:size].strip()
        self.buffer = self.buffer[size:].lstrip()
        return chunk


async def resolve_component_output(
    session: AudioSession,
    text: str | None,
) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]:
    component_call = extract_component_call(text)
    if component_call is None:
        return text, None, None

    await send_event(session, "component_call_started", component_call)
    result, status_code = await call_component_tool(component_call["components"], component_call["params"])
    if status_code >= 400:
        message = result.get("message") or f"component call failed: {status_code}"
        return f"开户网点查询失败：{message}", component_call, result

    return format_component_result(component_call["components"], result), component_call, result


async def configure_vad(session: AudioSession, value: Any) -> None:
    session.vad = None
    if not isinstance(value, dict):
        return

    engine = str(value.get("engine") or "").strip().lower()
    if engine not in {"silero", "server_silero"}:
        return
    if not SILERO_VAD_ENABLED:
        await send_event(session, "vad_error", {"message": "Silero VAD is disabled by SILERO_VAD_ENABLED=0"})
        return

    defaults = SileroVadConfig()
    config = SileroVadConfig(
        threshold=clamp_float(value.get("threshold"), default=defaults.threshold, low=0.05, high=0.95),
        min_speech_ms=clamp_int(value.get("minSpeechMs"), default=defaults.min_speech_ms, low=32, high=3000),
        min_silence_ms=clamp_int(
            value.get("minSilenceMs"),
            default=defaults.min_silence_ms,
            low=100,
            high=5000,
        ),
        max_speech_ms=clamp_int(
            value.get("maxSpeechMs"),
            default=defaults.max_speech_ms,
            low=1000,
            high=120000,
        ),
        speech_pad_ms=clamp_int(value.get("speechPadMs"), default=defaults.speech_pad_ms, low=0, high=500),
        use_onnx=parse_bool(value.get("onnx"), default=defaults.use_onnx),
    )
    try:
        session.vad = await asyncio.to_thread(SileroVadSession, config)
    except SileroVadUnavailable as exc:
        await send_event(session, "vad_error", {"message": str(exc)})
        return
    except Exception as exc:
        await send_event(session, "vad_error", {"message": f"Silero VAD load failed: {exc}"})
        return

    await send_event(
        session,
        "vad_ready",
        {
            "engine": "silero",
            "sample_rate": 16000,
            "threshold": config.threshold,
            "min_speech_ms": config.min_speech_ms,
            "min_silence_ms": config.min_silence_ms,
            "max_speech_ms": config.max_speech_ms,
        },
    )


def clamp_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def clamp_float(value: Any, *, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def parse_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "on", "yes"}


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

    if session.vad:
        await run_server_vad(session, pcm_float32_bytes)


async def handle_vad_monitor_chunk(session: AudioSession, pcm_float32_bytes: bytes) -> None:
    if session.stopped or not session.vad:
        return
    if len(pcm_float32_bytes) < 4:
        return
    await run_server_vad(session, pcm_float32_bytes)


async def run_server_vad(session: AudioSession, pcm_float32_bytes: bytes) -> None:
    try:
        vad_events = await asyncio.to_thread(
            session.vad.process_chunk,
            pcm_float32_bytes,
            session.source_sample_rate,
        )
    except Exception as exc:
        session.vad = None
        await send_event(session, "vad_error", {"message": f"Silero VAD failed: {exc}"})
        return

    for item in vad_events:
        event = item.get("event")
        if event == "speech_start":
            await send_event(session, "vad_speech_start", item)
        elif event in {"speech_end", "max_speech"}:
            await send_event(session, "vad_speech_end", item)


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
        modalities=["text"],
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
    text = parsed["text"]
    user_history_text = "[用户通过语音输入了一条消息]"
    routing_meta: dict[str, Any] = {}
    if session.route_context == "chatbox":
        text, user_history_text, routing_meta = await maybe_route_chatbox_voice_intent(
            model=session.model,
            assistant_text=text,
            skill_names=session.skill_names,
        )
    text, component_call, component_result = await resolve_component_output(session, text)
    normalized = normalize_assistant_output(text)
    audio_data_url = await synthesize_speech_audio(session.model, normalized.speech_text) if session.output_audio else None
    await send_event(
        session,
        "final_result",
        {
            "text": text,
            "audio_data_url": audio_data_url,
            "history_text": normalized.history_text,
            "speech_text": normalized.speech_text,
            "user_history_text": user_history_text,
            "output_is_json": normalized.is_json,
            "component_call": component_call,
            "component_result": component_result,
            **routing_meta,
            "raw_response": data,
            "saved_input_wav": str(input_path),
            "latency_ms": latency_ms,
        },
    )


async def stream_speech_worker(session: AudioSession, speech_queue: asyncio.Queue[str | None]) -> int:
    audio_count = 0
    spoken_parts: list[str] = []

    while True:
        speech_text = await speech_queue.get()
        try:
            if speech_text is None:
                return audio_count

            audio_data_url = await synthesize_speech_audio(session.model, speech_text)
            if not audio_data_url:
                continue

            audio_count += 1
            spoken_parts.append(speech_text)
            await send_event(
                session,
                "final_audio_delta",
                {
                    "audio_data_url": audio_data_url,
                    "audio_index": audio_count,
                    "speech_text": speech_text,
                    "history_text": "".join(spoken_parts),
                },
            )
        finally:
            speech_queue.task_done()


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
    buffered_for_component = True
    audio_count = 0
    speech_chunker = SpeechTextChunker()
    speech_queue: asyncio.Queue[str | None] = asyncio.Queue()
    stream_speech = session.output_audio and session.stream_speech_audio
    speech_task = asyncio.create_task(stream_speech_worker(session, speech_queue)) if stream_speech else None
    speech_finished = False

    async def queue_speech_text(text: str) -> None:
        if not stream_speech:
            return
        for speech_text in speech_chunker.add(text):
            await speech_queue.put(speech_text)

    async def finish_speech_worker() -> int:
        nonlocal speech_finished
        if not speech_task or speech_finished:
            return 0
        speech_finished = True
        await speech_queue.put(None)
        return await speech_task

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
                await finish_speech_worker()
                return

            chunk = item.get("data")
            if not isinstance(chunk, dict):
                continue
            delta = extract_stream_delta(chunk)
            if delta["text"]:
                text_parts.append(delta["text"])
                current_text = "".join(text_parts)
                if buffered_for_component and not should_buffer_component_candidate(current_text):
                    buffered_for_component = False
                    await send_event(session, "final_text_delta", {"text": current_text})
                    await queue_speech_text(current_text)
                elif not buffered_for_component:
                    await send_event(session, "final_text_delta", {"text": delta["text"]})
                    await queue_speech_text(delta["text"])
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
    user_history_text = "[用户通过语音输入了一条消息]"
    routing_meta: dict[str, Any] = {}
    if session.route_context == "chatbox":
        text, user_history_text, routing_meta = await maybe_route_chatbox_voice_intent(
            model=session.model,
            assistant_text=text,
            skill_names=session.skill_names,
        )
    text, component_call, component_result = await resolve_component_output(session, text)
    normalized = normalize_assistant_output(text)

    if stream_speech:
        if not component_call:
            for speech_text in speech_chunker.flush():
                await speech_queue.put(speech_text)
        audio_count += await finish_speech_worker()

    audio_data_url = None
    if session.output_audio and audio_count == 0:
        audio_data_url = await synthesize_speech_audio(session.model, normalized.speech_text)
    await send_event(
        session,
        "final_result",
        {
            "text": text,
            "audio_data_url": audio_data_url,
            "audio_chunks": audio_count,
            "history_text": normalized.history_text,
            "speech_text": normalized.speech_text,
            "user_history_text": user_history_text,
            "output_is_json": normalized.is_json,
            "component_call": component_call,
            "component_result": component_result,
            **routing_meta,
            "raw_response": None,
            "saved_input_wav": str(input_path),
            "latency_ms": int((time.perf_counter() - start) * 1000),
        },
    )

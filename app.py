import asyncio
import base64
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid
import wave
from array import array
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
CAPTURE_DIR = APP_DIR / "captures"
CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

QWEN_API_BASE = os.getenv("QWEN_API_BASE", "http://127.0.0.1:5440/v1").rstrip("/")
QWEN_MODEL = os.getenv("QWEN_MODEL", "Qwen3-Omni-30B-A3B-Instruct")
QWEN_PROVIDER = os.getenv("QWEN_PROVIDER", "auto")
QWEN_MODALITIES = [
    item.strip() for item in os.getenv("QWEN_MODALITIES", "text,audio").split(",") if item.strip()
]
QWEN_SPEAKER = os.getenv("QWEN_SPEAKER", "")
TARGET_SAMPLE_RATE = int(os.getenv("TARGET_SAMPLE_RATE", "16000"))
DEFAULT_PREFILL_MS = int(os.getenv("PREFILL_INTERVAL_MS", "600"))
PREFILL_MODE = os.getenv("PREFILL_MODE", "cumulative_probe")
FINAL_MAX_TOKENS = int(os.getenv("FINAL_MAX_TOKENS", "512"))
REQUEST_TIMEOUT = float(os.getenv("QWEN_REQUEST_TIMEOUT", "180"))
MAX_HISTORY_TURNS = int(os.getenv("MAX_HISTORY_TURNS", "10"))
STREAM_FINAL_OUTPUT = os.getenv("STREAM_FINAL_OUTPUT", "1").lower() not in {"0", "false", "off", "no"}
DEFAULT_FINAL_PROMPT = (
    "你正在进行实时语音对话。请不要转写、复述或翻译用户的语音内容。"
    "请先理解用户语音里的意图，然后像聊天助手一样直接回答用户的问题。"
    "请根据问题复杂度给出完整、有帮助的回答；只有在用户只是打招呼时才简短回应。"
)

app = FastAPI(title="Qwen3-Omni realtime audio demo")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@dataclass
class AudioSession:
    websocket: WebSocket
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    source_sample_rate: int = 48000
    target_sample_rate: int = TARGET_SAMPLE_RATE
    model: str = QWEN_MODEL
    prompt: str = DEFAULT_FINAL_PROMPT
    history: list[dict[str, str]] = field(default_factory=list)
    prefill_ms: int = DEFAULT_PREFILL_MS
    chunks: list[bytes] = field(default_factory=list)
    prefill_queue: asyncio.Queue[tuple[int, bytes]] = field(default_factory=asyncio.Queue)
    prefill_task: Optional[asyncio.Task] = None
    started_at: float = field(default_factory=time.time)
    stopped: bool = False


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/demo")
async def demo() -> FileResponse:
    return FileResponse(STATIC_DIR / "demo.html")


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "qwen_api_base": QWEN_API_BASE,
            "model": QWEN_MODEL,
            "prefill_mode": PREFILL_MODE,
            "provider": resolved_provider(),
            "modalities": QWEN_MODALITIES,
            "speaker": QWEN_SPEAKER or None,
            "target_sample_rate": TARGET_SAMPLE_RATE,
            "max_history_turns": MAX_HISTORY_TURNS,
            "stream_final_output": STREAM_FINAL_OUTPUT,
        }
    )


@app.websocket("/ws/audio")
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
        session.prompt = payload.get("prompt") or session.prompt
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


@dataclass
class JsonResponseData:
    status_code: int
    text: str

    def json(self) -> Any:
        return json.loads(self.text)


async def post_json(url: str, payload: dict[str, Any]) -> JsonResponseData:
    return await asyncio.to_thread(post_json_sync, url, payload)


def post_json_sync(url: str, payload: dict[str, Any]) -> JsonResponseData:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            text = response.read().decode("utf-8", errors="replace")
            return JsonResponseData(status_code=response.status, text=text)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return JsonResponseData(status_code=exc.code, text=text)


def stream_json_sync(url: str, payload: dict[str, Any], push: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    push({"type": "chunk", "data": json.loads(data)})
                except json.JSONDecodeError:
                    push({"type": "error", "message": data})
                    return
        push({"type": "done"})
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        push({"type": "error", "status_code": exc.code, "message": text})
    except Exception as exc:
        push({"type": "error", "message": str(exc)})


def build_chat_payload(
    model: str,
    wav_bytes: bytes,
    prompt: str,
    *,
    history: Optional[list[dict[str, str]]] = None,
    max_tokens: int,
) -> dict[str, Any]:
    audio_b64 = base64.b64encode(wav_bytes).decode("ascii")
    provider = resolved_provider()
    if provider == "vllm_omni":
        audio_item = {
            "type": "audio_url",
            "audio_url": {
                "url": f"data:audio/wav;base64,{audio_b64}",
            },
        }
    else:
        audio_item = {
            "type": "audio",
            "audio": f"data:audio/wav;base64,{audio_b64}",
        }

    messages: list[dict[str, Any]] = []
    for item in history or []:
        role = item.get("role")
        content = item.get("content")
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})

    messages.append(
        {
            "role": "user",
            "content": [
                audio_item,
                {
                    "type": "text",
                    "text": prompt,
                },
            ],
        }
    )

    payload = {
        "model": model,
        "messages": messages,
        "chat_template_kwargs": {"sampling_rate": TARGET_SAMPLE_RATE},
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": False,
    }
    if provider == "vllm_omni":
        payload["modalities"] = QWEN_MODALITIES
        if QWEN_SPEAKER:
            payload["speaker"] = QWEN_SPEAKER
    return payload


def normalize_history(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []

    items: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        content = content.strip()
        if content:
            items.append({"role": role, "content": content[:4000]})

    max_messages = max(0, MAX_HISTORY_TURNS * 2)
    if max_messages:
        return items[-max_messages:]
    return []


def resolved_provider() -> str:
    if QWEN_PROVIDER != "auto":
        return QWEN_PROVIDER
    if ":5440" in QWEN_API_BASE or ":8091" in QWEN_API_BASE or "vllm-omni" in QWEN_API_BASE:
        return "vllm_omni"
    return "ms_swift"


def normalize_model_name(model: str) -> str:
    if resolved_provider() == "vllm_omni" and "/" not in model:
        return QWEN_MODEL
    return model


def extract_model_output(data: dict[str, Any]) -> dict[str, Optional[str]]:
    text_parts: list[str] = []
    audio_data_url: Optional[str] = None

    for choice in data.get("choices") or []:
        message = choice.get("message") or {}
        content = message.get("content")

        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and item.get("text"):
                    text_parts.append(str(item["text"]))
                if audio_data_url is None:
                    audio_data_url = find_audio_data_url(item)

        openai_audio = message.get("audio")
        if isinstance(openai_audio, dict):
            transcript = openai_audio.get("transcript")
            if transcript:
                text_parts.append(str(transcript))
            if audio_data_url is None and openai_audio.get("data"):
                audio_data_url = as_audio_data_url(str(openai_audio["data"]))

        if audio_data_url is None:
            audio_data_url = find_audio_data_url(message)

    if audio_data_url is None:
        audio_data_url = find_audio_data_url(data)

    return {
        "text": "\n".join(part for part in text_parts if part).strip() or None,
        "audio_data_url": audio_data_url,
    }


def extract_stream_delta(data: dict[str, Any]) -> dict[str, Optional[str]]:
    text: Optional[str] = None
    audio_data_url: Optional[str] = None
    modality = data.get("modality")

    for choice in data.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if modality == "text" and isinstance(content, str):
            text = (text or "") + content
        elif modality == "audio" and isinstance(content, str):
            audio_data_url = as_audio_data_url(content)
        elif isinstance(content, str):
            if content.startswith("data:audio/") or len(content) > 200:
                audio_data_url = as_audio_data_url(content)
            else:
                text = (text or "") + content
        if audio_data_url is None:
            audio_data_url = find_audio_data_url(delta)

    if audio_data_url is None:
        audio_data_url = find_audio_data_url(data)
    return {"text": text, "audio_data_url": audio_data_url}


def as_audio_data_url(value: str) -> str:
    if value.startswith("data:audio/"):
        return value
    return "data:audio/wav;base64," + value


def find_audio_data_url(obj: Any) -> Optional[str]:
    if isinstance(obj, str):
        if obj.startswith("data:audio/"):
            return obj
        return None
    if isinstance(obj, list):
        for item in obj:
            found = find_audio_data_url(item)
            if found:
                return found
        return None
    if not isinstance(obj, dict):
        return None

    for key in ("audio", "audio_url", "output_audio", "audio_data"):
        value = obj.get(key)
        if isinstance(value, str):
            if value.startswith("data:audio/"):
                return value
            if len(value) > 200:
                return as_audio_data_url(value)
        if isinstance(value, dict):
            audio_value = value.get("url") or value.get("data") or value.get("base64")
            if isinstance(audio_value, str):
                return as_audio_data_url(audio_value)

    for value in obj.values():
        found = find_audio_data_url(value)
        if found:
            return found
    return None


async def send_event(session: AudioSession, event_type: str, payload: dict[str, Any]) -> None:
    await session.websocket.send_text(json.dumps({"type": event_type, **payload}, ensure_ascii=False))


def float32_sample_count(data: bytes) -> int:
    return len(data) // 4


def wav_bytes_from_float32_chunks(chunks: list[bytes], source_rate: int, target_rate: int) -> bytes:
    samples: list[float] = []
    for chunk in chunks:
        samples.extend(float32_bytes_to_list(chunk))
    if source_rate != target_rate:
        samples = resample_linear(samples, source_rate, target_rate)
    return pcm_float_to_wav_bytes(samples, target_rate)


def float32_bytes_to_list(data: bytes) -> list[float]:
    arr = array("f")
    arr.frombytes(data[: len(data) - (len(data) % 4)])
    if sys.byteorder != "little":
        arr.byteswap()
    return arr.tolist()


def resample_linear(samples: list[float], source_rate: int, target_rate: int) -> list[float]:
    if not samples or source_rate == target_rate:
        return samples
    output_length = max(1, int(len(samples) * target_rate / source_rate))
    ratio = source_rate / target_rate
    out: list[float] = []
    last_index = len(samples) - 1
    for i in range(output_length):
        pos = i * ratio
        left = int(pos)
        right = min(left + 1, last_index)
        frac = pos - left
        out.append(samples[left] * (1.0 - frac) + samples[right] * frac)
    return out


def pcm_float_to_wav_bytes(samples: list[float], sample_rate: int) -> bytes:
    frames = bytearray()
    for sample in samples:
        clipped = max(-1.0, min(1.0, sample))
        frames.extend(struct.pack("<h", int(clipped * 32767.0)))

    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))
    return buf.getvalue()

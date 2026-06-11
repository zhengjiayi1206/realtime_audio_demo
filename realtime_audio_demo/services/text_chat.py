import json
import time
from typing import Any

from realtime_audio_demo.config import QWEN_API_BASE
from realtime_audio_demo.services.qwen import build_text_payload, extract_model_output, post_json


async def request_text_completion(
    *,
    model: str,
    text: str,
    prompt: str,
    history: list[dict[str, str]],
    max_tokens: int,
    output_audio: bool = False,
) -> tuple[dict[str, Any], int]:
    payload = build_text_payload(
        model,
        text,
        prompt,
        history=history,
        max_tokens=max_tokens,
        modalities=["text", "audio"] if output_audio else ["text"],
    )

    start = time.perf_counter()
    try:
        response = await post_json(f"{QWEN_API_BASE}/chat/completions", payload)
    except Exception as exc:
        return {"message": str(exc)}, 502

    latency_ms = int((time.perf_counter() - start) * 1000)
    if response.status_code >= 400:
        return (
            {
                "message": response.text[:2000],
                "status_code": response.status_code,
                "latency_ms": latency_ms,
            },
            response.status_code,
        )

    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        return {"message": f"bad upstream json: {exc}", "latency_ms": latency_ms}, 502

    parsed = extract_model_output(data)
    return (
        {
            "text": parsed["text"],
            "audio_data_url": parsed["audio_data_url"],
            "latency_ms": latency_ms,
        },
        200,
    )

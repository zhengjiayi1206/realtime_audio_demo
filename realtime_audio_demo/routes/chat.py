from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from realtime_audio_demo.config import DEFAULT_CHAT_PROMPT, FINAL_MAX_TOKENS, QWEN_MODEL, normalize_model_name
from realtime_audio_demo.routes.request_utils import read_json_object
from realtime_audio_demo.services.qwen import normalize_history
from realtime_audio_demo.services.text_chat import request_text_completion


router = APIRouter()


@router.post("/api/chat/text")
async def chat_text(request: Request) -> JSONResponse:
    payload, error_response = await read_json_object(request)
    if error_response is not None:
        return error_response

    user_text = str(payload.get("text") or "").strip()
    if not user_text:
        return JSONResponse({"message": "text is required"}, status_code=400)

    result, status_code = await request_text_completion(
        model=normalize_model_name(payload.get("model") or QWEN_MODEL),
        text=user_text,
        prompt=payload.get("prompt") or DEFAULT_CHAT_PROMPT,
        history=normalize_history(payload.get("history")),
        max_tokens=FINAL_MAX_TOKENS,
        output_audio=False,
    )
    return JSONResponse(result, status_code=status_code)

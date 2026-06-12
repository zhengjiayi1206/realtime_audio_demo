import json
import logging
import re
from typing import Any

from realtime_audio_demo.config import FINAL_MAX_TOKENS
from realtime_audio_demo.services.qwen import apply_provider_options, post_json
from realtime_audio_demo.services.skill_loader import list_runtime_skills, normalize_skill_name


JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
logger = logging.getLogger("uvicorn.error")


def extract_json_object(text: Any) -> dict[str, Any] | None:
    if not isinstance(text, str):
        return None

    raw = strip_code_fence(text.strip())
    candidates = [raw]
    match = JSON_OBJECT_RE.search(raw)
    if match:
        candidates.append(match.group(0))

    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def strip_code_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return text


def complete_intent_target(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None

    target = normalize_intent_target(value)
    if target is None:
        return None

    fields = {
        "name": str(target.get("name") or "").strip(),
        "params1": str(target.get("params1") or "").strip(),
        "params2": str(target.get("params2") or "").strip(),
    }
    if all(field and field.lower() != "default" for field in fields.values()):
        return fields
    return None


def normalize_intent_target(value: dict[str, Any]) -> dict[str, Any] | None:
    target = value.get("target")
    if isinstance(target, dict):
        return target

    intention = value.get("intention")
    if not isinstance(intention, str):
        return None

    parts: dict[str, str] = {}
    for item in intention.split("#"):
        if "=" not in item:
            continue
        key, raw_value = item.split("=", 1)
        parts[key.strip()] = raw_value.strip()

    return {
        "name": parts.get("target.name", ""),
        "params1": parts.get("target.params1", ""),
        "params2": parts.get("target.params2", ""),
        "action_name": parts.get("action.name", ""),
    }


def build_skill_selection_prompt(intent_json: dict[str, Any], skills: list[dict[str, Any]]) -> str:
    catalog = "\n".join(
        f"- name: {skill.get('name')}\n  description: {skill.get('description') or ''}"
        for skill in skills
    )
    return (
        "你是 runtime skill 选择器。根据意图 JSON，从可用 skills 中选择最适合继续处理当前业务的一个 skill。\n"
        "只允许选择列表里的 skill name。如果没有合适的 skill，返回 default。\n"
        "只输出 JSON，不要解释。\n\n"
        f"意图 JSON:\n{json.dumps(intent_json, ensure_ascii=False)}\n\n"
        f"可用 skills:\n{catalog}\n\n"
        '输出格式：{"skill":"skill-name-or-default"}'
    )


def build_json_repair_prompt(raw_text: str) -> str:
    return (
        "你是 JSON 修复器。请把下面的模型输出修复为一个合法 JSON 对象。\n"
        "只能输出 JSON，不要解释，不要 Markdown，不要代码块。\n"
        "必须保留原始语义，不要新增业务判断。\n\n"
        "目标格式：\n"
        "{\n"
        '  "content": "需要继续询问用户的问题或确认话术",\n'
        '  "intention": "action.name=动作#target.name=对象#target.params1=参数1#target.params2=参数2"\n'
        "}\n\n"
        "如果原文无法判断意图，intention 必须为：\n"
        '"action.name=default#target.name=default#target.params1=default#target.params2=default"\n\n'
        f"原始输出：\n{raw_text}"
    )


async def repair_intent_json(model: str, raw_text: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(raw_text, str) or not raw_text.strip():
        return None, None

    prompt = build_json_repair_prompt(raw_text[:4000])
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": min(FINAL_MAX_TOKENS, 256),
        "temperature": 0,
        "stream": False,
    }
    payload = apply_provider_options(payload, modalities=["text"])
    response = await post_json_response(payload)
    parsed = extract_json_object(response.get("text"))
    logger.info(
        "chatbox json repair model=%s raw_input=%r repair_prompt=%s raw_output=%r parsed=%s",
        model,
        raw_text[:1000] if isinstance(raw_text, str) else raw_text,
        prompt,
        response.get("text"),
        json.dumps(parsed, ensure_ascii=False) if parsed is not None else None,
    )
    return parsed, {"raw": response.get("text"), "parsed": parsed}


async def select_skill_for_intent(model: str, intent_json: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    skills = list_runtime_skills()
    if not skills:
        return None, {"reason": "no runtime skills"}

    prompt = build_skill_selection_prompt(intent_json, skills)
    logger.info(
        "chatbox skill selection input intent=%s skills=%s prompt=%s",
        json.dumps(intent_json, ensure_ascii=False),
        json.dumps(
            [
                {
                    "name": skill.get("name"),
                    "description": skill.get("description") or "",
                }
                for skill in skills
            ],
            ensure_ascii=False,
        ),
        prompt,
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": min(FINAL_MAX_TOKENS, 256),
        "temperature": 0,
        "stream": False,
    }
    payload = apply_provider_options(payload, modalities=["text"])
    response = await post_json_response(payload)
    parsed = extract_json_object(response.get("text"))
    selected = normalize_skill_name(str((parsed or {}).get("skill") or ""))
    valid_names = {normalize_skill_name(str(skill.get("name") or "")): str(skill.get("name") or "") for skill in skills}
    if not selected or selected == "default" or selected not in valid_names:
        logger.info(
            "chatbox skill selection result selected=%r valid=false raw_output=%r parsed=%s valid_names=%s",
            selected,
            response.get("text"),
            json.dumps(parsed, ensure_ascii=False) if parsed is not None else None,
            sorted(valid_names.values()),
        )
        return None, {"raw": response.get("text"), "parsed": parsed}
    logger.info(
        "chatbox skill selection result selected=%s raw_output=%r parsed=%s",
        valid_names[selected],
        response.get("text"),
        json.dumps(parsed, ensure_ascii=False) if parsed is not None else None,
    )
    return valid_names[selected], {"raw": response.get("text"), "parsed": parsed}


async def post_json_response(payload: dict[str, Any]) -> dict[str, Any]:
    from realtime_audio_demo.config import QWEN_API_BASE
    from realtime_audio_demo.services.qwen import extract_model_output

    response = await post_json(f"{QWEN_API_BASE}/chat/completions", payload)
    if response.status_code >= 400:
        return {"text": None, "status_code": response.status_code, "message": response.text[:1000]}
    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        return {"text": None, "message": f"bad upstream json: {exc}"}
    return extract_model_output(data)

import json
from dataclasses import dataclass


@dataclass
class AssistantOutput:
    raw_text: str
    history_text: str
    is_json: bool


def normalize_assistant_output(text: str | None) -> AssistantOutput:
    raw_text = (text or "").strip()
    if not raw_text:
        return AssistantOutput(raw_text="", history_text="", is_json=False)

    try:
        value = json.loads(raw_text)
    except json.JSONDecodeError:
        return AssistantOutput(raw_text=raw_text, history_text=raw_text, is_json=False)

    if not isinstance(value, dict):
        return AssistantOutput(raw_text=raw_text, history_text="", is_json=True)

    for key in ("content", "soundsName"):
        field = value.get(key)
        if isinstance(field, str) and field.strip():
            return AssistantOutput(raw_text=raw_text, history_text=field.strip(), is_json=True)

    return AssistantOutput(raw_text=raw_text, history_text="", is_json=True)

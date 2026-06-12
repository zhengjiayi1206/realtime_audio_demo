import asyncio
import json
from typing import Any


BRANCH_LOOKUP_COMPONENT = "call_10901558"
BRANCH_LOOKUP_RESULT = {
    "dm_vnoName": "平安银行金华浦江支行",
    "dm_vniAddr": "江西省赣州市章贡区新柳公寓",
    "dm_vniNode": "",
    "dm_lastFour": "",
    "dm_vendFlag": "",
    "dm_vniTel": "",
    "dm_vniNotes": "",
    "WSRESULT": "0",
}


def extract_component_call(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    if BRANCH_LOOKUP_COMPONENT not in text:
        return None

    params: dict[str, Any] = {}
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError:
        data = None

    if isinstance(data, dict) and isinstance(data.get("params"), dict):
        params = data["params"]
    return {
        "components": BRANCH_LOOKUP_COMPONENT,
        "params": params,
    }


async def call_component_tool(component: str, params: dict[str, Any]) -> tuple[dict[str, Any], int]:
    component = component.strip()
    if component != BRANCH_LOOKUP_COMPONENT:
        return {"message": f"unsupported components: {component}"}, 404

    await asyncio.sleep(1)
    return dict(BRANCH_LOOKUP_RESULT), 200


def format_component_result(component: str, result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)

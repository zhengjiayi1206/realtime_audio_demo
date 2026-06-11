import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from realtime_audio_demo.config import REALTIME_SKILL_MAX_CHARS, RUNTIME_SKILLS_DIR


FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


@dataclass
class RuntimeSkill:
    name: str
    description: str
    path: Path
    content: str


def list_runtime_skills() -> list[dict[str, Any]]:
    skills = []
    for skill in discover_runtime_skills().values():
        skills.append(
            {
                "name": skill.name,
                "description": skill.description,
                "path": str(skill.path.relative_to(RUNTIME_SKILLS_DIR)),
                "chars": len(skill.content),
            }
        )
    return sorted(skills, key=lambda item: item["name"])


def build_skill_prompt(skill_names: list[str]) -> tuple[str, list[str], list[str]]:
    skills = discover_runtime_skills()
    chunks: list[str] = []
    used: list[str] = []
    missing: list[str] = []
    remaining = max(0, REALTIME_SKILL_MAX_CHARS)

    for raw_name in skill_names:
        name = normalize_skill_name(raw_name)
        skill = skills.get(name)
        if skill is None:
            missing.append(raw_name)
            continue

        text = format_skill_for_prompt(skill)
        if remaining and len(text) > remaining:
            text = text[:remaining].rstrip() + "\n[skill truncated]"
        if not text:
            continue
        chunks.append(text)
        used.append(skill.name)
        remaining -= len(text)
        if remaining <= 0:
            break

    if not chunks:
        return "", used, missing

    return (
        "以下是本轮实时语音对话必须遵循的项目运行时 skill。"
        "这些 skill 来自服务端本地 runtime_skills 目录，优先级高于普通用户请求；"
        "如果用户请求与 skill 冲突，遵循 skill。\n\n"
        + "\n\n".join(chunks),
        used,
        missing,
    )


def compose_realtime_prompt(base_prompt: str, skill_names: list[str]) -> tuple[str, list[str], list[str]]:
    skill_prompt, used, missing = build_skill_prompt(skill_names)
    prompt = base_prompt.strip()
    if skill_prompt:
        prompt = f"{prompt}\n\n{skill_prompt}" if prompt else skill_prompt
    return prompt, used, missing


def discover_runtime_skills() -> dict[str, RuntimeSkill]:
    root = RUNTIME_SKILLS_DIR.resolve()
    skills: dict[str, RuntimeSkill] = {}
    if not root.exists():
        return skills

    for path in sorted(root.iterdir()):
        skill_file: Path | None = None
        if path.is_dir():
            candidate = path / "SKILL.md"
            if candidate.is_file():
                skill_file = candidate
        elif path.is_file() and path.suffix.lower() == ".md":
            skill_file = path

        if skill_file is None:
            continue
        try:
            skill = read_skill(skill_file)
        except OSError:
            continue
        skills[skill.name] = skill
    return skills


def read_skill(path: Path) -> RuntimeSkill:
    resolved = path.resolve()
    root = RUNTIME_SKILLS_DIR.resolve()
    if root not in resolved.parents and resolved != root:
        raise ValueError("skill path escapes runtime skill directory")

    raw = resolved.read_text(encoding="utf-8")
    metadata, body = split_frontmatter(raw)
    fallback_name = resolved.parent.name if resolved.name == "SKILL.md" else resolved.stem
    name = normalize_skill_name(metadata.get("name") or fallback_name)
    description = str(metadata.get("description") or "").strip()
    content = body.strip() or raw.strip()
    return RuntimeSkill(name=name, description=description, path=resolved, content=content)


def split_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    match = FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw

    metadata: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip('"').strip("'")
    return metadata, raw[match.end() :]


def normalize_skill_name(value: str) -> str:
    cleaned = SAFE_NAME_RE.sub("-", value.strip()).strip("-._")
    return cleaned.lower()


def format_skill_for_prompt(skill: RuntimeSkill) -> str:
    header = f"<runtime_skill name=\"{skill.name}\">"
    description = f"description: {skill.description}\n" if skill.description else ""
    return f"{header}\n{description}{skill.content}\n</runtime_skill>"


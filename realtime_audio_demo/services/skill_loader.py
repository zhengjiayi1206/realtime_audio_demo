import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from realtime_audio_demo.config import REALTIME_SKILL_MAX_CHARS, RUNTIME_SKILLS_DIR


FRONTMATTER_RE = re.compile(r"\A[\ufeff\s+]*---[ \t]*\r?\n(.*?)\r?\n[ \t]*---[ \t]*\r?\n?", re.DOTALL)
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

        text = skill.content
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

    return "\n\n".join(chunks), used, missing


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

    metadata = parse_frontmatter_metadata(match.group(1))
    return metadata, raw[match.end() :]


def parse_frontmatter_metadata(text: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    name_match = re.search(r"(?im)^\s*name\s*:\s*(.+?)\s*$", text)
    if name_match:
        metadata["name"] = clean_frontmatter_value(name_match.group(1))

    description_match = re.search(r"(?ims)^\s*description\s*:\s*([>|])\s*\r?\n(.*?)(?=^\s*[a-zA-Z0-9_.-]+\s*:|\Z)", text)
    if description_match:
        metadata["description"] = fold_block_scalar(
            [line.strip() for line in description_match.group(2).splitlines()],
            literal=description_match.group(1) == "|",
        )
    else:
        description_match = re.search(r"(?im)^\s*description\s*:\s*(.+?)\s*$", text)
        if description_match:
            metadata["description"] = clean_frontmatter_value(description_match.group(1))
    return metadata


def clean_frontmatter_value(value: str) -> str:
    return value.strip().strip('"').strip("'").strip()


def fold_block_scalar(lines: list[str], *, literal: bool) -> str:
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    if literal:
        return "\n".join(lines).strip()
    return " ".join(line for line in lines if line).strip()


def normalize_skill_name(value: str) -> str:
    cleaned = SAFE_NAME_RE.sub("-", value.strip()).strip("-._")
    return cleaned.lower()

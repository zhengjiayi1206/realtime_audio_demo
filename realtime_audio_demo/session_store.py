import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from realtime_audio_demo.config import MAX_HISTORY_TURNS, SESSION_TTL

logger = logging.getLogger("uvicorn.error")


@dataclass
class ChatSession:
    session_id: str
    history: list[dict[str, str]] = field(default_factory=list)
    locked_skill: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    last_access: float = field(default_factory=time.time)


_sessions: dict[str, ChatSession] = {}
_lock = asyncio.Lock()


async def get_session(session_id: str) -> ChatSession:
    async with _lock:
        session = _sessions.get(session_id)
        if session is None:
            session = ChatSession(session_id=session_id)
            _sessions[session_id] = session
            logger.info("session %s created", session_id)
        session.last_access = time.time()
        return session


async def lock_session_skill(session_id: str, skill_name: str) -> ChatSession:
    async with _lock:
        session = _sessions.get(session_id)
        if session is None:
            session = ChatSession(session_id=session_id)
            _sessions[session_id] = session
        session.history = []
        session.locked_skill = skill_name
        session.last_access = time.time()
        logger.info("session %s skill locked to %s, history cleared", session_id, skill_name)
        return session


async def append_history(session_id: str, role: str, content: str) -> None:
    if not content or not content.strip():
        return
    async with _lock:
        session = _sessions.get(session_id)
        if session is None:
            return
        session.history.append({"role": role, "content": content[:4000]})
        max_messages = max(0, MAX_HISTORY_TURNS * 2)
        if max_messages and len(session.history) > max_messages:
            session.history = session.history[-max_messages:]
        session.last_access = time.time()


async def get_session_history(session_id: str) -> list[dict[str, str]]:
    async with _lock:
        session = _sessions.get(session_id)
        if session is None:
            return []
        session.last_access = time.time()
        return list(session.history)


async def get_session_locked_skill(session_id: str) -> Optional[str]:
    async with _lock:
        session = _sessions.get(session_id)
        if session is None:
            return None
        session.last_access = time.time()
        return session.locked_skill


async def delete_session(session_id: str) -> None:
    async with _lock:
        if session_id in _sessions:
            del _sessions[session_id]
            logger.info("session %s deleted", session_id)


async def cleanup_expired_sessions() -> int:
    now = time.time()
    async with _lock:
        expired = [
            sid
            for sid, s in _sessions.items()
            if now - s.last_access > SESSION_TTL
        ]
        for sid in expired:
            del _sessions[sid]
    if expired:
        logger.info("cleaned up %d expired sessions", len(expired))
    return len(expired)

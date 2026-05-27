"""歷史 Session 儲存：讀寫 Supabase chat_sessions 表。"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from .supabase_logger import get_client


def upsert_session(
    session_id: str,
    employee_id: str,
    title: str,
    turns: list,
) -> None:
    """新建或更新 session（upsert by id）。turns 可以是 dataclass list 或 dict list。"""
    client = get_client()
    if not client:
        return
    try:
        turns_dicts = [
            dataclasses.asdict(t) if dataclasses.is_dataclass(t) else t
            for t in turns
        ]
        client.table("chat_sessions").upsert({
            "id": session_id,
            "employee_id": employee_id,
            "title": title,
            "turns": turns_dicts,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        print(f"[session_store] upsert 失敗：{e}")


def load_sessions_list(employee_id: str, limit: int = 60) -> list[dict]:
    """載入使用者的 session 清單（不含 turns），最新在前。"""
    client = get_client()
    if not client:
        return []
    try:
        result = (
            client.table("chat_sessions")
            .select("id, title, updated_at")
            .eq("employee_id", employee_id)
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []
    except Exception:
        return []


def load_session_turns(session_id: str) -> list[dict]:
    """載入指定 session 的完整 turns（dict 格式）。"""
    client = get_client()
    if not client:
        return []
    try:
        result = (
            client.table("chat_sessions")
            .select("turns")
            .eq("id", session_id)
            .execute()
        )
        if not result.data:
            return []
        return result.data[0].get("turns", [])
    except Exception:
        return []

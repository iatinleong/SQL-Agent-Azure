"""使用者認證：員工編號 + 密碼，bcrypt hash 存 Supabase users 表。
Session token 存 Supabase sessions 表，cookie 保持跨 refresh 登入狀態。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from .supabase_logger import get_client


def _hash(password: str) -> str:
    import bcrypt
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify(password: str, hashed: str) -> bool:
    import bcrypt
    return bcrypt.checkpw(password.encode(), hashed.encode())


def register_user(
    employee_id: str,
    password: str,
    display_name: str = "",
) -> tuple[bool, str]:
    """註冊新使用者。回傳 (success, message)。"""
    client = get_client()
    if client is None:
        return False, "資料庫連線失敗"
    existing = (
        client.table("users")
        .select("employee_id")
        .eq("employee_id", employee_id)
        .execute()
    )
    if existing.data:
        return False, "此員工編號已被註冊"
    client.table("users").insert({
        "employee_id": employee_id,
        "password_hash": _hash(password),
        "display_name": display_name.strip() or employee_id,
    }).execute()
    return True, "註冊成功"


def login_user(
    employee_id: str,
    password: str,
) -> tuple[bool, dict | str]:
    """驗證登入。回傳 (success, user_dict) 或 (False, error_message)。"""
    client = get_client()
    if client is None:
        return False, "資料庫連線失敗"
    result = (
        client.table("users")
        .select("employee_id, password_hash, display_name")
        .eq("employee_id", employee_id)
        .execute()
    )
    if not result.data:
        return False, "員工編號不存在"
    user = result.data[0]
    if not _verify(password, user["password_hash"]):
        return False, "密碼錯誤"
    return True, {
        "employee_id": user["employee_id"],
        "display_name": user.get("display_name") or employee_id,
    }


# ── Session token（跨 refresh 保持登入）──────────────────────────

_SESSION_DAYS = 30


def create_session(employee_id: str, display_name: str) -> str:
    """產生 session token，寫入 Supabase sessions 表，回傳 token。"""
    token = str(uuid.uuid4())
    expires = datetime.now(timezone.utc) + timedelta(days=_SESSION_DAYS)
    client = get_client()
    if client:
        client.table("sessions").insert({
            "token": token,
            "employee_id": employee_id,
            "display_name": display_name,
            "expires_at": expires.isoformat(),
        }).execute()
    return token


def verify_session(token: str) -> dict | None:
    """驗證 token 是否有效，回傳 user dict 或 None。"""
    if not token:
        return None
    client = get_client()
    if client is None:
        return None
    result = (
        client.table("sessions")
        .select("employee_id, display_name, expires_at")
        .eq("token", token)
        .execute()
    )
    if not result.data:
        return None
    row = result.data[0]
    expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) > expires:
        return None
    return {
        "employee_id": row["employee_id"],
        "display_name": row.get("display_name") or row["employee_id"],
    }


def delete_session(token: str) -> None:
    """刪除 session token（登出用）。"""
    if not token:
        return
    client = get_client()
    if client:
        client.table("sessions").delete().eq("token", token).execute()

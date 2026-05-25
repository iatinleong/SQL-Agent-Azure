"""Supabase 連線工具：供所有需要寫入 Cloud 的模組共用。"""

from __future__ import annotations

import os

_client = None


def get_client():
    """取得（或建立）Supabase client。找不到 credentials 時回傳 None。"""
    global _client
    if _client is not None:
        return _client

    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_KEY", "")
    if not url or not key:
        return None

    try:
        from supabase import create_client
        _client = create_client(url, key)
        return _client
    except Exception as e:
        print(f"  [Supabase] 初始化失敗：{e}")
        return None


def insert(table: str, data: dict) -> tuple[bool, str]:
    """寫入一筆記錄。回傳 (成功, 錯誤訊息)。"""
    client = get_client()
    if client is None:
        msg = f"找不到 SUPABASE_URL / SUPABASE_KEY（URL={os.getenv('SUPABASE_URL','(空')}）"
        print(f"  [Supabase] {msg}")
        return False, msg
    try:
        client.table(table).insert(data).execute()
        return True, ""
    except Exception as e:
        msg = str(e)
        print(f"  [Supabase] 寫入 {table} 失敗：{msg}")
        return False, msg

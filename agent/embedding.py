"""共用 BGE-M3 embedding model。

Thread-safety 設計：
  - _init_lock：double-checked locking，確保模型只初始化一次（修 race condition）
  - _encode_lock：序列化所有 encode() 呼叫，避免多 session 同時搶 CPU
    → 這就是 queue 效果：第二個 session 的 encode 會排隊等第一個完成
  - _waiting：記錄正在等待 _encode_lock 的 thread 數，供 UI 顯示排隊人數
"""
from __future__ import annotations

import threading

import numpy as np

from .config import BGE_MODEL_PATH

_model = None
_init_lock = threading.Lock()
_encode_lock = threading.Lock()
_waiting = 0  # 等待取得 _encode_lock 的 thread 數（不含正在執行的）


def _get_model():
    global _model
    if _model is None:
        with _init_lock:
            if _model is None:  # double-checked locking
                from sentence_transformers import SentenceTransformer
                _model = SentenceTransformer(BGE_MODEL_PATH, device="cpu")
    return _model


def get_queue_info() -> tuple[bool, int]:
    """回傳 (is_encoding, n_waiting)。
    is_encoding：目前是否有 session 正在執行 encode。
    n_waiting  ：正在排隊等待的 session 數（不含正在執行的）。
    """
    return _encode_lock.locked(), max(0, _waiting)


def encode(texts: list[str], **kwargs) -> np.ndarray:
    """Thread-safe encode，同一時間只允許一個 encode 任務執行。"""
    global _waiting
    _waiting += 1
    _acquired = False
    try:
        with _encode_lock:
            _waiting -= 1
            _acquired = True
            return _get_model().encode(texts, **kwargs)
    finally:
        if not _acquired:
            _waiting -= 1

"""SQLite + Fernet 加密的 Token 存储（按 Telegram 用户）。

设计为不可变风格：读返回新 dict，不就地修改持久层之外的状态。
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from core.config import get_settings


@dataclass(frozen=True)
class TokenRecord:
    tg_user_id: int
    token: str
    token_date: Optional[str]  # 瑞幸返回的 luckyMcpTokenDate（到期信息）
    updated_at: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_tokens (
    tg_user_id  INTEGER PRIMARY KEY,
    enc_token   BLOB    NOT NULL,
    token_date  TEXT,
    updated_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS spend_log (
    tg_user_id  INTEGER NOT NULL,
    day         TEXT    NOT NULL,   -- YYYY-MM-DD
    amount      REAL    NOT NULL,
    order_id    TEXT,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    tg_user_id    INTEGER NOT NULL,
    order_id      TEXT    NOT NULL,
    summary       TEXT,
    created_at    INTEGER NOT NULL,
    cancelled_at  INTEGER,
    PRIMARY KEY (tg_user_id, order_id)
);
CREATE TABLE IF NOT EXISTS user_location (
    tg_user_id  INTEGER PRIMARY KEY,
    lng         REAL    NOT NULL,
    lat         REAL    NOT NULL,
    label       TEXT,
    updated_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS login_nonce (
    nonce       TEXT    PRIMARY KEY,
    user_key    INTEGER NOT NULL,
    created_at  INTEGER NOT NULL
);
"""


def _fernet() -> Fernet:
    key = get_settings().fernet_key
    if not key:
        raise RuntimeError("FERNET_KEY 未配置；生成: "
                           'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"')
    return Fernet(key.encode() if isinstance(key, str) else key)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_settings().db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        # 轻量迁移：旧库的 orders 表补 cancelled_at 列
        cols = [r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
        if "cancelled_at" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN cancelled_at INTEGER")


def set_token(tg_user_id: int, token: str, token_date: Optional[str] = None) -> None:
    enc = _fernet().encrypt(token.encode())
    with _connect() as conn:
        conn.execute(
            "INSERT INTO user_tokens (tg_user_id, enc_token, token_date, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(tg_user_id) DO UPDATE SET "
            "enc_token=excluded.enc_token, token_date=excluded.token_date, updated_at=excluded.updated_at",
            (tg_user_id, enc, token_date, int(time.time())),
        )


def get_token(tg_user_id: int) -> Optional[TokenRecord]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT tg_user_id, enc_token, token_date, updated_at FROM user_tokens WHERE tg_user_id=?",
            (tg_user_id,),
        ).fetchone()
    if not row:
        return None
    try:
        token = _fernet().decrypt(row["enc_token"]).decode()
    except InvalidToken:
        return None  # 密钥变更 / 数据损坏 → 视为未登录
    return TokenRecord(row["tg_user_id"], token, row["token_date"], row["updated_at"])


def delete_token(tg_user_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM user_tokens WHERE tg_user_id=?", (tg_user_id,))


def record_spend(tg_user_id: int, day: str, amount: float, order_id: Optional[str]) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO spend_log (tg_user_id, day, amount, order_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (tg_user_id, day, amount, order_id, int(time.time())),
        )


def record_order(tg_user_id: int, order_id: str, summary: Optional[str] = None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO orders (tg_user_id, order_id, summary, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(tg_user_id, order_id) DO UPDATE SET "
            "summary=COALESCE(excluded.summary, orders.summary)",  # 后来的 NULL 摘要不抹掉已有标签
            (tg_user_id, order_id, summary, int(time.time())),
        )


def mark_order_cancelled(tg_user_id: int, order_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE orders SET cancelled_at=? WHERE tg_user_id=? AND order_id=?",
            (int(time.time()), tg_user_id, order_id),
        )


def list_orders(tg_user_id: int, limit: int = 5) -> list[dict]:
    """最近未取消的订单（最新在前）。仅含经本 bot 创建的单。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT order_id, summary, created_at FROM orders "
            "WHERE tg_user_id=? AND cancelled_at IS NULL "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (tg_user_id, limit),
        ).fetchall()
    return [{"order_id": r["order_id"], "summary": r["summary"], "created_at": r["created_at"]} for r in rows]


def create_login_nonce(nonce: str, user_key: int) -> None:
    """登录页用：把一次性登录链接绑定到 bot 用户(已折算成 db key)。"""
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO login_nonce (nonce, user_key, created_at) VALUES (?, ?, ?)",
            (nonce, user_key, int(time.time())),
        )


def consume_login_nonce(nonce: str, max_age: int = 900) -> Optional[int]:
    """取出并删除 nonce，返回绑定的 user_key（单次、15 分钟内有效）。"""
    with _connect() as conn:
        row = conn.execute("SELECT user_key, created_at FROM login_nonce WHERE nonce=?", (nonce,)).fetchone()
        if row:
            conn.execute("DELETE FROM login_nonce WHERE nonce=?", (nonce,))
    if not row or int(time.time()) - row["created_at"] > max_age:
        return None
    return row["user_key"]


def set_location(tg_user_id: int, lng: float, lat: float, label: Optional[str] = None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO user_location (tg_user_id, lng, lat, label, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(tg_user_id) DO UPDATE SET "
            "lng=excluded.lng, lat=excluded.lat, label=excluded.label, updated_at=excluded.updated_at",
            (tg_user_id, lng, lat, label, int(time.time())),
        )


def get_location(tg_user_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT lng, lat, label FROM user_location WHERE tg_user_id=?", (tg_user_id,)
        ).fetchone()
    return {"lng": row["lng"], "lat": row["lat"], "label": row["label"]} if row else None


def spend_today(tg_user_id: int, day: str) -> float:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM spend_log WHERE tg_user_id=? AND day=?",
            (tg_user_id, day),
        ).fetchone()
    return float(row["s"] or 0.0)

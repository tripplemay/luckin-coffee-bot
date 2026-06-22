"""SQLite + Fernet 加密的 Token 存储（按 Telegram 用户）。

设计为不可变风格：读返回新 dict，不就地修改持久层之外的状态。
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    channel     TEXT,                 -- 'tg' | 'wx'：登录成功后往哪个渠道回推
    push_target TEXT,                 -- 原生推送目标（tg=chat_id，wx=原始 user_key 串）
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS consumer_session (
    user_key    INTEGER PRIMARY KEY,  -- 消费版 H5 (m.lkcoffee.com) 登录态，用于优惠券领取
    enc_session BLOB    NOT NULL,      -- Fernet 加密的 ConsumerSession JSON
    updated_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS coupon_claim_log (
    user_key    INTEGER NOT NULL,     -- 领券限频用：每用户每日次数 + 最近一次时间
    day         TEXT    NOT NULL,
    claimed     INTEGER NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    user_key   INTEGER PRIMARY KEY,   -- 多用户：建档 + 活跃 + 封禁（/admin、告警用）
    channel    TEXT,                  -- 'tg' | 'wx'
    label      TEXT,                  -- 显示名（若拿得到）
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    blocked    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage_log (
    user_key  INTEGER NOT NULL,       -- 每用户每日消息计数（限频，护 API 预算）
    day       TEXT    NOT NULL,
    msg_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_key, day)
);
CREATE TABLE IF NOT EXISTS user_limits (
    user_key    INTEGER PRIMARY KEY,  -- 每用户限额覆盖（NULL=用全局默认）
    daily_spend REAL,
    daily_msgs  INTEGER
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
        # 轻量迁移：旧库的 login_nonce 表补 channel / push_target 列（登录成功回推用）
        ncols = [r[1] for r in conn.execute("PRAGMA table_info(login_nonce)").fetchall()]
        if "channel" not in ncols:
            conn.execute("ALTER TABLE login_nonce ADD COLUMN channel TEXT")
        if "push_target" not in ncols:
            conn.execute("ALTER TABLE login_nonce ADD COLUMN push_target TEXT")


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


def set_consumer_session(user_key: int, session_json: str) -> None:
    """加密保存消费版 H5 登录态（优惠券领取用）。"""
    enc = _fernet().encrypt(session_json.encode())
    with _connect() as conn:
        conn.execute(
            "INSERT INTO consumer_session (user_key, enc_session, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_key) DO UPDATE SET enc_session=excluded.enc_session, updated_at=excluded.updated_at",
            (user_key, enc, int(time.time())),
        )


def get_consumer_session(user_key: int) -> Optional[str]:
    """取回消费版登录态 JSON；无/损坏返回 None。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT enc_session FROM consumer_session WHERE user_key=?", (user_key,)
        ).fetchone()
    if not row:
        return None
    try:
        return _fernet().decrypt(row["enc_session"]).decode()
    except InvalidToken:
        return None


def delete_consumer_session(user_key: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM consumer_session WHERE user_key=?", (user_key,))


def record_coupon_claim(user_key: int, day: str, claimed: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO coupon_claim_log (user_key, day, claimed, created_at) VALUES (?, ?, ?, ?)",
            (user_key, day, claimed, int(time.time())),
        )


def coupon_claims_today(user_key: int, day: str) -> int:
    """今日已发起的领取次数（限频用，含领到 0 张的尝试）。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM coupon_claim_log WHERE user_key=? AND day=?", (user_key, day)
        ).fetchone()
    return int(row["n"] or 0)


def last_coupon_claim_at(user_key: int) -> Optional[int]:
    """最近一次领取尝试的时间戳（最小间隔限频用）；从未则 None。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT MAX(created_at) AS t FROM coupon_claim_log WHERE user_key=?", (user_key,)
        ).fetchone()
    return int(row["t"]) if row and row["t"] is not None else None


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


@dataclass(frozen=True)
class NonceRecord:
    user_key: int
    channel: Optional[str]       # 'tg' | 'wx'：回推到哪个渠道
    push_target: Optional[str]   # 原生推送目标（tg=chat_id，wx=原始 user_key 串）


def create_login_nonce(nonce: str, user_key: int, channel: Optional[str] = None,
                       push_target: Optional[str] = None) -> None:
    """登录页用：把一次性登录链接绑定到 bot 用户(已折算成 db key)。

    channel/push_target 用于登录成功后把"✅ 已登录"回推到来源渠道（见 core/push.py）。
    """
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO login_nonce (nonce, user_key, channel, push_target, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (nonce, user_key, channel, push_target, int(time.time())),
        )


def consume_login_nonce(nonce: str, max_age: int = 900) -> Optional[NonceRecord]:
    """取出并删除 nonce，返回绑定信息（单次、默认 15 分钟内有效）。过期/不存在返回 None。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT user_key, channel, push_target, created_at FROM login_nonce WHERE nonce=?", (nonce,)
        ).fetchone()
        if row:
            conn.execute("DELETE FROM login_nonce WHERE nonce=?", (nonce,))
    if not row or int(time.time()) - row["created_at"] > max_age:
        return None
    return NonceRecord(row["user_key"], row["channel"], row["push_target"])


def peek_login_nonce(nonce: str, max_age: int = 900) -> bool:
    """只校验 nonce 是否存在且未过期（不删除）。用于发短信前的轻量准入，防 SMS 滥用。"""
    if not nonce:
        return False
    with _connect() as conn:
        row = conn.execute("SELECT created_at FROM login_nonce WHERE nonce=?", (nonce,)).fetchone()
    return bool(row) and int(time.time()) - row["created_at"] <= max_age


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


# ---- 多用户：建档/活跃/封禁 · 限频 · 每用户限额 ----

_CST = timezone(timedelta(hours=8))


def today_cst() -> str:
    """固定 +08:00 的"今天"（避免服务器若为 UTC 导致按天计数在 08:00 重置）。"""
    return datetime.now(_CST).strftime("%Y-%m-%d")


def touch_user(user_key: int, channel: Optional[str] = None, label: Optional[str] = None) -> bool:
    """更新 last_seen（首次出现则建档）。返回 True 表示这是新用户（供告警）。"""
    now = int(time.time())
    with _connect() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE user_key=?", (user_key,)).fetchone()
        if exists:
            conn.execute(
                "UPDATE users SET last_seen=?, channel=COALESCE(?, channel), label=COALESCE(?, label) WHERE user_key=?",
                (now, channel, label, user_key))
            return False
        conn.execute(
            "INSERT INTO users (user_key, channel, label, first_seen, last_seen) VALUES (?, ?, ?, ?, ?)",
            (user_key, channel, label, now, now))
        return True


def is_blocked(user_key: int) -> bool:
    with _connect() as conn:
        row = conn.execute("SELECT blocked FROM users WHERE user_key=?", (user_key,)).fetchone()
    return bool(row and row["blocked"])


def set_blocked(user_key: int, blocked: bool) -> None:
    with _connect() as conn:
        conn.execute("UPDATE users SET blocked=? WHERE user_key=?", (1 if blocked else 0, user_key))


def usage_today(user_key: int, day: str) -> int:
    with _connect() as conn:
        row = conn.execute("SELECT msg_count FROM usage_log WHERE user_key=? AND day=?", (user_key, day)).fetchone()
    return int(row["msg_count"]) if row else 0


def incr_usage(user_key: int, day: str) -> int:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO usage_log (user_key, day, msg_count) VALUES (?, ?, 1) "
            "ON CONFLICT(user_key, day) DO UPDATE SET msg_count = msg_count + 1",
            (user_key, day))
        row = conn.execute("SELECT msg_count FROM usage_log WHERE user_key=? AND day=?", (user_key, day)).fetchone()
    return int(row["msg_count"])


def set_user_limit(user_key: int, daily_spend: Optional[float] = None, daily_msgs: Optional[int] = None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO user_limits (user_key, daily_spend, daily_msgs) VALUES (?, ?, ?) "
            "ON CONFLICT(user_key) DO UPDATE SET daily_spend=excluded.daily_spend, daily_msgs=excluded.daily_msgs",
            (user_key, daily_spend, daily_msgs))


def get_user_limits(user_key: int) -> tuple[Optional[float], Optional[int]]:
    """(daily_spend, daily_msgs) 覆盖值；未设为 (None, None)。"""
    with _connect() as conn:
        row = conn.execute("SELECT daily_spend, daily_msgs FROM user_limits WHERE user_key=?", (user_key,)).fetchone()
    return (row["daily_spend"], row["daily_msgs"]) if row else (None, None)


def effective_spend_limit(user_key: int) -> float:
    override, _ = get_user_limits(user_key)
    return float(override) if override is not None else get_settings().daily_spend_limit


def effective_msg_limit(user_key: int) -> int:
    _, override = get_user_limits(user_key)
    return int(override) if override is not None else get_settings().daily_msg_limit


def gate_message(user_key: int, day: str) -> Optional[str]:
    """每条入站消息的准入闸：封禁 / 超每日次数 → 返回拒绝语；放行则**原子**计数并返回 None。

    用 INSERT ... ON CONFLICT ... RETURNING 一步自增并拿回计数，避免 check-then-act 竞态
    （并发投递时各请求拿到唯一计数）；超限的请求回补一次，使计数不漂移到上限以上。
    """
    if is_blocked(user_key):
        return "你已被管理员停用本服务。"
    limit = effective_msg_limit(user_key)
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO usage_log (user_key, day, msg_count) VALUES (?, ?, 1) "
            "ON CONFLICT(user_key, day) DO UPDATE SET msg_count = msg_count + 1 RETURNING msg_count",
            (user_key, day)).fetchone()
        if int(row["msg_count"]) > limit:
            conn.execute("UPDATE usage_log SET msg_count = msg_count - 1 WHERE user_key=? AND day=?",
                         (user_key, day))
            return f"今日使用次数已达上限（{limit} 次/天），明天再来～"
    return None


def _order_count(user_key: int) -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM orders WHERE tg_user_id=?", (user_key,)).fetchone()
    return int(row["n"] or 0)


def list_users(day: str, limit: int = 50) -> list[dict]:
    """用户列表（最近活跃在前）+ 今日消息/消费 + 总订单数，供 /admin。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT user_key, channel, label, last_seen, blocked FROM users ORDER BY last_seen DESC LIMIT ?",
            (limit,)).fetchall()
    out = []
    for r in rows:
        uk = r["user_key"]
        out.append({
            "user_key": uk, "channel": r["channel"], "label": r["label"],
            "last_seen": r["last_seen"], "blocked": bool(r["blocked"]),
            "msgs_today": usage_today(uk, day), "spend_today": spend_today(uk, day),
            "orders": _order_count(uk),
        })
    return out

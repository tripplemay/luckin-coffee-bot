"""owner 专用 /admin（渠道无关，返回文本）+ owner 判定。

owner 由 .env 的 owner_tg_id / owner_wx_key 配置；未配置则相应渠道的 /admin 关闭。
user_key 是 db 内部整数键（Telegram=tg_id，微信=_uid 哈希），/admin users 会列出来供引用。
"""
from __future__ import annotations

from datetime import datetime

from core import db
from core.config import get_settings


def is_owner_tg(tg_id: int) -> bool:
    oid = get_settings().owner_tg_id
    return bool(oid) and tg_id == oid


def is_owner_wx(wx_key: str) -> bool:
    okey = get_settings().owner_wx_key
    return bool(okey) and wx_key == okey


def _fmt_ts(ts: int) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
    except Exception:
        return "-"


def _summary() -> str:
    day = db.today_cst()
    users = db.list_users(day, limit=1000)
    active = sum(1 for u in users if u["msgs_today"] > 0)
    msgs = sum(u["msgs_today"] for u in users)
    spend = sum(u["spend_today"] for u in users)
    blocked = sum(1 for u in users if u["blocked"])
    return (f"📊 概览（今日）\n用户 {len(users)}（今日活跃 {active}，封禁 {blocked}）\n"
            f"今日消息 {msgs} 条 · 今日消费 ¥{spend:.2f}\n明细：/admin users")


def _users() -> str:
    day = db.today_cst()
    users = db.list_users(day, limit=30)
    if not users:
        return "还没有用户。"
    lines = ["👥 用户（最近活跃在前）："]
    for u in users:
        flag = "🚫" if u["blocked"] else ""
        lines.append(
            f"{flag}{u['channel'] or '?'} `{u['user_key']}` {u['label'] or '-'} · "
            f"今{u['msgs_today']}条/¥{u['spend_today']:.0f} · 单{u['orders']} · {_fmt_ts(u['last_seen'])}")
    lines.append("\n限额：/admin limit <key> <消费上限> <消息上限>　封禁：/admin block <key>")
    return "\n".join(lines)


def admin_command(args: str) -> str:
    """处理 /admin 子命令，返回文本。调用方须已确认是 owner。"""
    parts = (args or "").split()
    if not parts:
        return _summary()
    sub = parts[0]
    if sub == "users":
        return _users()
    if sub == "limit" and len(parts) >= 4:
        try:
            uk, spend, msgs = int(parts[1]), float(parts[2]), int(parts[3])
        except ValueError:
            return "参数错误。用法：/admin limit <key> <消费上限> <消息上限>"
        db.set_user_limit(uk, spend, msgs)
        return f"✅ 已设用户 {uk}：消费上限 ¥{spend:.0f}/天，消息 {msgs} 条/天"
    if sub in ("block", "unblock") and len(parts) >= 2:
        try:
            uk = int(parts[1])
        except ValueError:
            return "key 须为数字"
        db.set_blocked(uk, sub == "block")
        return f"✅ 已{'封禁' if sub == 'block' else '解封'}用户 {uk}"
    return ("用法：\n/admin —— 今日概览\n/admin users —— 用户列表\n"
            "/admin limit <key> <消费上限> <消息上限>\n/admin block <key>\n/admin unblock <key>")

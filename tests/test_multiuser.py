"""多用户：建档/封禁/限频/每用户限额 + 对话历史裁剪。"""
from __future__ import annotations

from bot.agent import OrderingAgent
from core import db
from core.config import get_settings

DAY = "2026-06-23"


def test_touch_user_new_vs_existing():
    assert db.touch_user(7777, "tg", "Alice") is True
    assert db.touch_user(7777, "tg", "Alice") is False
    assert db.touch_user(7777) is False  # 不带 channel/label 也不报错


def test_block_and_usage():
    db.touch_user(7001, "tg")
    assert db.is_blocked(7001) is False
    db.set_blocked(7001, True)
    assert db.is_blocked(7001) is True
    db.set_blocked(7001, False)
    assert db.usage_today(7001, DAY) == 0
    assert db.incr_usage(7001, DAY) == 1
    assert db.incr_usage(7001, DAY) == 2
    assert db.usage_today(7001, DAY) == 2
    assert db.usage_today(7001, "2026-06-24") == 0  # 跨天独立


def test_user_limits_override_and_effective():
    s = get_settings()
    assert db.effective_spend_limit(8888) == s.daily_spend_limit  # 未设 → 全局
    assert db.effective_msg_limit(8888) == s.daily_msg_limit
    db.set_user_limit(8888, daily_spend=20.0, daily_msgs=5)
    assert db.effective_spend_limit(8888) == 20.0
    assert db.effective_msg_limit(8888) == 5


def test_gate_message_limit_and_block():
    u = 9090
    db.set_user_limit(u, daily_msgs=2)
    assert db.gate_message(u, DAY) is None      # 1
    assert db.gate_message(u, DAY) is None      # 2
    assert "上限" in db.gate_message(u, DAY)     # 3 → 拦
    db.touch_user(u, "tg")
    db.set_blocked(u, True)
    assert "停用" in db.gate_message(u, DAY)     # 封禁优先


def test_list_users_aggregates():
    db.touch_user(1212, "wx", "Bob")
    db.incr_usage(1212, DAY)
    db.record_spend(1212, DAY, 9.9, "o-1212")
    db.record_order(1212, "o-1212", "美式")
    me = [x for x in db.list_users(DAY, limit=100) if x["user_key"] == 1212]
    assert me, "用户应被列出"
    u = me[0]
    assert u["label"] == "Bob" and u["msgs_today"] == 1
    assert u["spend_today"] == 9.9 and u["orders"] == 1


def test_is_owner(monkeypatch):
    from core import admin
    monkeypatch.setattr(admin, "get_settings",
                        lambda: type("S", (), {"owner_tg_id": 123, "owner_wx_key": "wxkey"})())
    assert admin.is_owner_tg(123) is True
    assert admin.is_owner_tg(999) is False
    assert admin.is_owner_wx("wxkey") is True
    assert admin.is_owner_wx("other") is False
    monkeypatch.setattr(admin, "get_settings",
                        lambda: type("S", (), {"owner_tg_id": 0, "owner_wx_key": ""})())
    assert admin.is_owner_tg(123) is False  # 未配置 → 关闭
    assert admin.is_owner_wx("wxkey") is False


def test_admin_command():
    from core import admin
    db.touch_user(5555, "tg", "Carol")
    db.incr_usage(5555, db.today_cst())
    assert "概览" in admin.admin_command("")
    assert "用户" in admin.admin_command("users")
    assert "已设" in admin.admin_command("limit 5555 30 10")
    assert db.get_user_limits(5555) == (30.0, 10)
    assert "封禁" in admin.admin_command("block 5555")
    assert db.is_blocked(5555) is True
    assert "解封" in admin.admin_command("unblock 5555")
    assert db.is_blocked(5555) is False
    assert "用法" in admin.admin_command("nonsense")
    assert "参数错误" in admin.admin_command("limit 5555 abc 10")


def test_trim_history_keeps_system_and_user_boundary():
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(60):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    trimmed = OrderingAgent._trim_history(msgs)
    cap = get_settings().history_max_msgs
    assert trimmed[0]["role"] == "system"
    assert trimmed[1]["role"] == "user"          # 从 user 起头，不孤立 tool 配对
    assert len(trimmed) <= cap + 1
    # 短历史原样返回
    short = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    assert OrderingAgent._trim_history(short) is short

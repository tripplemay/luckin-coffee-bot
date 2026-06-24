"""Telegram driver（最小 PTB 桩）：/prefs + 老样子复购的安全接线。

仅用轻量 fake Update/Context 验证 TG 特有的接线，核心判定逻辑已在 core.prefs /
agent.build_reorder / ChannelCore 处充分单测。重点守护：复购走确认门、不自动下单。
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot.main as main
from core import db


class FakeMCP:
    def __init__(self):
        self.calls = []
        self.calls_full = []

    async def call_tool(self, token, name, arguments):
        self.calls.append(name)
        self.calls_full.append((name, arguments))
        if name == "previewOrder":
            return {"success": True, "data": {"discountPrice": 12.45, "couponCodeList": ["FRESH"],
                                              "productInfoList": [{"name": "美式", "amount": 1, "estimatePrice": 12.45}]}}
        if name == "createOrder":
            return {"success": True, "data": {"orderIdStr": "TG-R1", "payOrderUrl": "weixin://x",
                                              "needPay": True, "discountPrice": 12.45}}
        return {"success": True, "data": {}}


class FakeApp:
    def create_task(self, coro):
        try:
            coro.close()  # 别让后台协程(告警/轮询)悬挂
        except Exception:
            pass


def _make_update(uid, text=None, cb_data=None):
    user = SimpleNamespace(id=uid, full_name="U")
    chat = SimpleNamespace(id=555)
    message = SimpleNamespace(text=text, location=None, reply_text=AsyncMock(), chat_id=555)
    cq = None
    if cb_data is not None:
        cq = SimpleNamespace(data=cb_data, from_user=user, answer=AsyncMock(),
                             edit_message_text=AsyncMock(), message=message)
    return SimpleNamespace(effective_user=user, effective_chat=chat, message=message, callback_query=cq)


def _make_context():
    bot = SimpleNamespace(send_chat_action=AsyncMock(), send_photo=AsyncMock())
    return SimpleNamespace(user_data={}, bot=bot, application=FakeApp())


@pytest.fixture
def mcp(monkeypatch):
    m = FakeMCP()
    monkeypatch.setattr(main, "MCP", m)
    monkeypatch.setattr(main.AGENT, "_mcp", m)
    monkeypatch.setattr(db, "touch_user", lambda *a, **k: False)
    return m


def _last_reply(update):
    return update.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_tg_prefs_set_and_view(mcp):
    uid = 90011
    ctx = _make_context()
    await main.cmd_prefs(_make_update(uid, "/prefs set 温度 热"), ctx)
    assert db.get_prefs(uid)["temperature"] == "热"
    up = _make_update(uid, "/prefs")
    await main.cmd_prefs(up, ctx)
    assert "热" in _last_reply(up)


@pytest.mark.asyncio
async def test_tg_prefs_set_blocked_during_pending(mcp):
    uid = 90012
    ctx = _make_context()
    ctx.user_data["pending"] = {"call": {}, "price": 1.0}  # 模拟待确认订单
    up = _make_update(uid, "/prefs set 温度 冰")
    await main.cmd_prefs(up, ctx)
    assert db.get_prefs(uid) is None          # 待确认时不允许改偏好
    assert "待确认" in _last_reply(up)


@pytest.mark.asyncio
async def test_tg_reorder_no_history(mcp):
    uid = 90013
    db.set_token(uid, "T")
    up = _make_update(uid, "")
    await main.cmd_reorder(up, _make_context())
    assert "还没有可复购" in _last_reply(up)


@pytest.mark.asyncio
async def test_tg_pending_blocks_new_text(mcp):
    # 有待确认订单时再发文字（含老样子/NL）应被模态拦截，不覆盖 pending、不下单
    uid = 90015
    db.set_token(uid, "T")
    ctx = _make_context()
    ctx.user_data["pending"] = {"call": {}, "price": 12.0}
    up = _make_update(uid, "老样子")
    await main._handle_text(up, ctx, "老样子")
    assert "待确认" in _last_reply(up)
    assert ctx.user_data["pending"] == {"call": {}, "price": 12.0}  # 未被覆盖
    assert "createOrder" not in mcp.calls


@pytest.mark.asyncio
async def test_tg_reorder_through_confirm_gate(mcp):
    uid = 90014
    db.set_token(uid, "T")
    db.record_order(uid, "o-seed", "美式×1",
                    product_list=json.dumps([{"amount": 1, "productId": 2, "skuCode": "S"}]),
                    dept_id="1", lng=116.39, lat=39.98)
    ctx = _make_context()
    # 老样子 → 停在确认态，createOrder 未执行
    await main.cmd_reorder(_make_update(uid, "老样子"), ctx)
    assert ctx.user_data["pending"]["reorder"] is True
    assert "createOrder" not in mcp.calls
    assert "previewOrder" in mcp.calls
    # 点确认 → 执行 createOrder，且券取自新预览
    await main.on_callback(_make_update(uid, cb_data="order:confirm"), ctx)
    assert "createOrder" in mcp.calls
    create_args = next(a for n, a in mcp.calls_full if n == "createOrder")
    assert create_args.get("couponCodeList") == ["FRESH"]
    assert "pending" not in ctx.user_data  # 确认后清挂起

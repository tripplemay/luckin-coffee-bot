"""微信渠道：偏好 /prefs（查看/设置/清除/模态拦截）+ 老样子复购（确认门/券重配/失败回退）。"""
import json

import pytest

from bot.agent import OrderingAgent
from bot.mcp_client import MCPToolError
from core import db
from service.app import ChannelCore, _uid


@pytest.fixture(autouse=True)
def _suppress_onboarding(monkeypatch):
    monkeypatch.setattr(db, "touch_user", lambda *a, **k: False)


class ReorderMCP:
    def __init__(self, coupons=None, preview_fail=False, no_price=False,
                 preview_price=12.45, create_price=12.45, cancel_raises=False):
        self.calls = []
        self.calls_full = []
        self.coupons = coupons or []
        self.preview_fail = preview_fail
        self.no_price = no_price
        self.preview_price = preview_price
        self.create_price = create_price
        self.cancel_raises = cancel_raises

    async def call_tool(self, token, name, arguments):
        self.calls.append(name)
        self.calls_full.append((name, arguments))
        if name == "previewOrder":
            if self.preview_fail:
                raise MCPToolError("门店打烊中")
            data = {"couponCodeList": self.coupons,
                    "productInfoList": [{"name": "生椰拿铁", "amount": 1}]}
            if not self.no_price:  # no_price=True → 预览成功但无任何价格字段
                data["discountPrice"] = self.preview_price
            return {"success": True, "data": data}
        if name == "createOrder":
            return {"success": True, "data": {"orderIdStr": "R1", "payOrderUrl": "weixin://x",
                                              "needPay": True, "discountPrice": self.create_price}}
        if name == "cancelOrder":
            if self.cancel_raises:
                raise MCPToolError("取消失败")
            return {"success": True, "data": True}
        return {"success": True, "data": {}}


def _script(*msgs):
    it = iter(msgs)

    async def fake_chat(_messages):
        return next(it)
    return fake_chat


def _tc(name, args):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c_" + name, "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}]}


def _wire(mcp, monkeypatch, *chat_msgs):
    core = ChannelCore()
    core._mcp = mcp
    core._agent = OrderingAgent(mcp)  # type: ignore[arg-type]
    if chat_msgs:
        monkeypatch.setattr(core._agent, "_chat", _script(*chat_msgs))
    return core


def _seed_payload(user_key, order_id="seed1", summary="生椰拿铁×1"):
    db.record_order(_uid(user_key), order_id, summary,
                    product_list=json.dumps([{"amount": 1, "productId": 11447, "skuCode": "SP-1"}]),
                    dept_id="1", lng=116.39, lat=39.98)


@pytest.mark.asyncio
async def test_wechat_prefs_view_set_clear(monkeypatch):
    core = _wire(ReorderMCP(), monkeypatch)
    u = "wx_prefs_1"
    # 查看空态（无需登录）
    assert "还没有设置" in (await core.handle(u, "/prefs"))[0]["text"]
    # 设置标量
    assert "已记住" in (await core.handle(u, "/prefs set 温度 热"))[0]["text"]
    assert db.get_prefs(_uid(u))["temperature"] == "热"
    # 查看反映
    assert "热" in (await core.handle(u, "我的偏好"))[0]["text"]
    # 列表增项
    await core.handle(u, "/prefs set 忌口 +牛奶")
    assert db.get_prefs(_uid(u))["dietary"] == ["牛奶"]
    # 清单字段
    await core.handle(u, "/prefs clear 温度")
    assert db.get_prefs(_uid(u))["temperature"] is None
    # 全清
    await core.handle(u, "/prefs clear")
    assert db.get_prefs(_uid(u)) is None


@pytest.mark.asyncio
async def test_wechat_prefs_set_blocked_during_pending(monkeypatch):
    core = _wire(
        ReorderMCP(), monkeypatch,
        _tc("createOrder", {"deptId": 1, "productList": [{"amount": 1, "productId": 11447, "skuCode": "SP-1"}],
                            "longitude": 1, "latitude": 2}),
        {"role": "assistant", "content": "ok", "tool_calls": None},
    )
    u = "wx_prefs_modal"
    await core.handle(u, "/login T")
    await core.handle(u, "/loc 1,2")
    assert "确认" in (await core.handle(u, "下单"))[0]["text"]
    # 待确认时：设置被拦，查看仍可
    r = await core.handle(u, "/prefs set 温度 热")
    assert "待确认" in r[0]["text"]
    assert db.get_prefs(_uid(u)) is None
    assert "偏好" in (await core.handle(u, "/prefs"))[0]["text"]  # 查看放行
    await core.handle(u, "取消")


@pytest.mark.asyncio
async def test_wechat_reorder_through_confirm_gate(monkeypatch):
    mcp = ReorderMCP()
    core = _wire(mcp, monkeypatch)
    u = "wx_reorder_1"
    await core.handle(u, "/login T")
    _seed_payload(u)
    # 老样子 → 预览并停在确认态，createOrder 未执行
    r = await core.handle(u, "老样子")
    assert "确认" in r[0]["text"] and "复购" in r[0]["text"]
    assert "createOrder" not in mcp.calls
    assert "previewOrder" in mcp.calls
    # 确认 → 执行 createOrder + 出二维码
    r2 = await core.handle(u, "确认")
    assert "createOrder" in mcp.calls
    assert any(a["type"] == "image" for a in r2)


@pytest.mark.asyncio
async def test_wechat_reorder_uses_fresh_preview_coupons(monkeypatch):
    mcp = ReorderMCP(coupons=["FRESH-COUPON"])
    core = _wire(mcp, monkeypatch)
    u = "wx_reorder_coupon"
    await core.handle(u, "/login T")
    _seed_payload(u)
    await core.handle(u, "老样子")
    await core.handle(u, "确认")
    create_args = next(a for n, a in mcp.calls_full if n == "createOrder")
    assert create_args.get("couponCodeList") == ["FRESH-COUPON"]  # 用新预览的券，非存储旧券


@pytest.mark.asyncio
async def test_wechat_reorder_over_cap_blocked(monkeypatch):
    mcp = ReorderMCP()
    core = _wire(mcp, monkeypatch)
    u = "wx_reorder_cap"
    await core.handle(u, "/login T")
    _seed_payload(u)
    db.set_user_limit(_uid(u), daily_spend=5.0)  # 上限 5 元 < 复购价 12.45
    r = await core.handle(u, "老样子")
    assert "⛔" in r[0]["text"] or "超出" in r[0]["text"]
    assert "createOrder" not in mcp.calls  # 超额绝不下单


@pytest.mark.asyncio
async def test_wechat_reorder_staleness_falls_back_to_llm(monkeypatch):
    mcp = ReorderMCP(preview_fail=True)
    core = _wire(mcp, monkeypatch,
                 {"role": "assistant", "content": "那家店可能打烊了，帮你看看附近其它店～", "tool_calls": None})
    u = "wx_reorder_stale"
    await core.handle(u, "/login T")
    _seed_payload(u)
    r = await core.handle(u, "老样子")
    # 预览失败 → 回退 LLM（文本回复），绝不自动下单
    assert r[0]["type"] == "text"
    assert "createOrder" not in mcp.calls


@pytest.mark.asyncio
async def test_wechat_reorder_no_price_falls_back(monkeypatch):
    # 预览"成功"但没有任何价格字段 → 绝不出无价确认，回退 LLM（评审 MEDIUM 修复）
    mcp = ReorderMCP(no_price=True)
    core = _wire(mcp, monkeypatch,
                 {"role": "assistant", "content": "这单价格拿不到，帮你重新看看～", "tool_calls": None})
    u = "wx_reorder_noprice"
    await core.handle(u, "/login T")
    _seed_payload(u)
    r = await core.handle(u, "老样子")
    assert r[0]["type"] == "text"
    assert "createOrder" not in mcp.calls  # 没价格不进确认态、不下单


@pytest.mark.asyncio
async def test_wechat_overlimit_cancel_fails_records_and_warns(monkeypatch):
    # 实付远超确认价且超日限、自动取消又失败 → 落库订单(可查/可手动取消) + 诚实提示(不谎称未扣款)
    mcp = ReorderMCP(preview_price=5.0, create_price=80.0, cancel_raises=True)
    core = _wire(
        mcp, monkeypatch,
        _tc("createOrder", {"deptId": 1, "productList": [{"amount": 1, "productId": 1, "skuCode": "S"}],
                            "longitude": 1, "latitude": 2}),
        {"role": "assistant", "content": "ok", "tool_calls": None},
    )
    u = "wx_overlimit"
    await core.handle(u, "/login T")
    await core.handle(u, "/loc 1,2")
    db.set_user_limit(_uid(u), daily_spend=10.0)  # 上限 10：确认价 5 过，但实付 80 超
    assert "确认" in (await core.handle(u, "下单"))[0]["text"]
    r = await core.handle(u, "确认")
    txt = " ".join(a.get("text", "") for a in r)
    assert "超出单日上限" in txt and "手动取消" in txt
    assert "未扣款" not in txt  # 不谎称未扣款
    # 订单已落库 → 可在 /orders / /cancel 看到（未取消成功故仍可见）
    assert db.list_orders(_uid(u))  # 至少一条
    assert db.spend_today(_uid(u), db.today_cst()) >= 80.0  # 计入当日额度

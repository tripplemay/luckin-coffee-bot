"""渠道服务（微信版）编排测试：登录/位置门槛 + createOrder 文本确认护栏。"""
import json

import pytest

from bot.agent import OrderingAgent
from service.app import ChannelCore


class FakeMCP:
    def __init__(self):
        self.calls = []

    async def call_tool(self, token, name, arguments):
        self.calls.append(name)
        if name == "queryShopList":
            return {"success": True, "data": [{"deptId": 1, "deptName": "店"}]}
        if name == "searchProductForMcp":
            return {"success": True, "data": [{"productId": 11447, "skuCode": "SP9636-00001", "productName": "生椰拿铁", "estimatePrice": 16}]}
        if name == "previewOrder":
            return {"success": True, "data": {"discountPrice": 16, "couponCodeList": [], "productInfoList": [{"name": "生椰拿铁", "amount": 1, "estimatePrice": 16}]}}
        if name == "createOrder":
            return {"success": True, "data": {"orderIdStr": "999", "payOrderUrl": "weixin://wxpay/bizpayurl?pr=x", "payOrderQrCodeUrl": "https://x/qr", "needPay": True, "discountPrice": 16}}
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


def _wire(monkeypatch, *chat_msgs):
    core = ChannelCore()
    mcp = FakeMCP()
    core._mcp = mcp
    core._agent = OrderingAgent(mcp)  # type: ignore[arg-type]
    monkeypatch.setattr(core._agent, "_chat", _script(*chat_msgs))
    return core, mcp


@pytest.mark.asyncio
async def test_wechat_order_flow_and_guardrail(monkeypatch):
    core, mcp = _wire(
        monkeypatch,
        _tc("queryShopList", {"longitude": 116.39, "latitude": 39.98}),
        _tc("searchProductForMcp", {"deptId": 1, "query": "生椰拿铁"}),
        _tc("createOrder", {"deptId": 1, "productList": [{"amount": 1, "productId": 11447, "skuCode": "SP9636-00001"}], "longitude": 116.39, "latitude": 39.98}),
        {"role": "assistant", "content": "下单成功。", "tool_calls": None},
    )
    u = "wx_user_1"
    assert "请先登录" in (await core.handle(u, "来杯生椰拿铁"))[0]["text"]
    assert "登录成功" in (await core.handle(u, "/login TESTTOKEN"))[0]["text"]
    assert "/loc" in (await core.handle(u, "来杯生椰拿铁"))[0]["text"]
    assert "已定位" in (await core.handle(u, "/loc 116.39,39.98"))[0]["text"]

    # 下单 → 必须停在确认态，createOrder 未执行
    r = await core.handle(u, "来杯热的生椰拿铁")
    assert "确认" in r[0]["text"]
    assert "createOrder" not in mcp.calls
    assert mcp.calls == ["queryShopList", "searchProductForMcp", "previewOrder"]

    # 回复『确认』→ 执行 createOrder + 返回支付二维码
    r = await core.handle(u, "确认")
    assert "createOrder" in mcp.calls
    types = [a["type"] for a in r]
    assert "image" in types
    assert any("已创建订单" in a.get("text", "") for a in r if a["type"] == "text")


@pytest.mark.asyncio
async def test_wechat_cancel_does_not_order(monkeypatch):
    core, mcp = _wire(
        monkeypatch,
        _tc("createOrder", {"deptId": 1, "productList": [{"amount": 1, "productId": 11447, "skuCode": "SP9636-00001"}], "longitude": 1, "latitude": 2}),
        {"role": "assistant", "content": "好的", "tool_calls": None},
    )
    u = "wx_user_2"
    await core.handle(u, "/login T")
    await core.handle(u, "/loc 1,2")
    assert "确认" in (await core.handle(u, "下单"))[0]["text"]
    assert "已取消" in (await core.handle(u, "取消"))[0]["text"]
    assert "createOrder" not in mcp.calls


@pytest.mark.asyncio
async def test_loc_parsing_and_range(monkeypatch):
    core, _ = _wire(monkeypatch, {"role": "assistant", "content": "hi", "tool_calls": None})
    u = "wx_user_3"
    await core.handle(u, "/login T")
    # 坐标形态但纬度越界 → 范围错误
    assert "范围" in (await core.handle(u, "/loc 39.98,116.39"))[0]["text"]
    # 非坐标(地址) 且测试环境无 AMAP_KEY → 提示
    assert "地址" in (await core.handle(u, "/loc 安贞环宇荟"))[0]["text"]
    # 合法坐标 → 已定位
    assert "已定位" in (await core.handle(u, "/loc 116.392, 39.982"))[0]["text"]


@pytest.mark.asyncio
async def test_pending_order_is_modal(monkeypatch):
    core, mcp = _wire(
        monkeypatch,
        _tc("createOrder", {"deptId": 1, "productList": [{"amount": 1, "productId": 11447, "skuCode": "SP9636-00001"}], "longitude": 1, "latitude": 2}),
        {"role": "assistant", "content": "ok", "tool_calls": None},
    )
    u = "wx_modal"
    await core.handle(u, "/login T")
    await core.handle(u, "/loc 1,2")
    assert "确认" in (await core.handle(u, "下单"))[0]["text"]
    # 有待确认订单时，/loc 等命令被模态护栏拦截，不绕过去下单
    r = await core.handle(u, "/loc 100,50")
    assert "待确认" in r[0]["text"]
    assert "createOrder" not in mcp.calls
    assert "已取消" in (await core.handle(u, "取消"))[0]["text"]


@pytest.mark.asyncio
async def test_loc_geocode_and_remember(monkeypatch):
    core, mcp = _wire(
        monkeypatch,
        _tc("queryShopList", {"longitude": 116.39, "latitude": 39.98}),
        _tc("searchProductForMcp", {"deptId": 1, "query": "美式"}),
        {"role": "assistant", "content": "给你找到附近门店啦", "tool_calls": None},
    )
    u = "wx_geo"
    await core.handle(u, "/login T")
    # 地址 → 地理编码（mock 高德）
    async def fake_geo(addr):
        return (116.39, 39.98, "北京安贞环宇荟")
    monkeypatch.setattr(core, "_geocode", fake_geo)
    r = await core.handle(u, "/loc 安贞环宇荟")
    assert "已定位" in r[0]["text"] and "北京安贞环宇荟" in r[0]["text"]

    # 模拟服务重启：内存 state 清空，但 db 记着位置 → 直接点单不再要求设位置
    core._states.clear()
    r2 = await core.handle(u, "来杯美式")
    assert r2[0]["type"] == "text" and "先发" not in r2[0]["text"]


@pytest.mark.asyncio
async def test_msg_id_dedup(monkeypatch):
    core, _ = _wire(monkeypatch, {"role": "assistant", "content": "hi", "tool_calls": None})
    u = "wx_dedup"
    r1 = await core.handle(u, "/login T", msg_id="m1")
    assert "登录成功" in r1[0]["text"]
    assert await core.handle(u, "/login T", msg_id="m1") == []  # 同一条重投不重复处理


def test_voice_text_passthrough_echoes():
    """微信自带转写(voice_item.text)→ /voice 直接用，回显🎧 并走同一套逻辑（省 ASR）。"""
    from fastapi.testclient import TestClient

    from service.app import app
    with TestClient(app) as client:
        r = client.post("/voice", json={"user_key": "wx_voice_1", "text": "你好", "msg_id": "v1"})
    assert r.status_code == 200
    acts = r.json()["actions"]
    assert acts[0]["text"].startswith("🎧 听到：你好")
    assert len(acts) >= 2  # echo + handle 的回复


def test_voice_audio_without_asr_gracefully(monkeypatch):
    from fastapi.testclient import TestClient

    from core import asr
    from service.app import app
    monkeypatch.setattr(asr, "asr_enabled", lambda: False)
    with TestClient(app) as client:
        r = client.post("/voice", json={"user_key": "wx_v2", "audio_b64": "AAAA"})
    assert r.status_code == 200
    assert "开启" in r.json()["actions"][0]["text"]

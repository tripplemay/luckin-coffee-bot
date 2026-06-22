from bot import flows
from core import db

PREVIEW = {
    "code": 0, "msg": "success", "success": True,
    "data": {
        "discountPrice": 16, "privilegeMoney": 0,
        "shopInfo": {"deptName": "AI点单专用"},
        "productInfoList": [
            {"name": "耶加雪菲拿铁", "amount": 1, "additionDesc": "热", "estimatePrice": 16},
        ],
    },
}

CREATED = {
    "code": 0, "msg": "success", "success": True,
    "data": {
        "orderId": 7639308439653908490, "orderIdStr": "7639308439653908490",
        "payOrderUrl": "weixin://wxpay/bizpayurl?pr=ifbmtaEz1",
        "payOrderQrCodeUrl": "https://opentest03.lkcoffee.com/transfer/qrcode?token=xxxx",
        "discountPrice": 16, "needPay": True,
    },
}

STATUS = {
    "code": 0, "msg": "success", "success": True,
    "data": {"orderStatus": 60, "orderStatusName": "等待取餐",
             "takeMealCodeInfo": {"code": "A123"}},
}


def test_unwrap():
    assert flows.unwrap(PREVIEW) == PREVIEW["data"]
    assert flows.unwrap({"foo": 1}) == {"foo": 1}


def test_format_preview():
    text, price = flows.format_preview(PREVIEW)
    assert price == 16.0
    assert "耶加雪菲拿铁" in text
    assert "合计应付：¥16.00" in text


def test_format_order_created():
    text, qr, order_id, need_pay, pay_page = flows.format_order_created(CREATED)
    assert order_id == "7639308439653908490"
    assert need_pay is True
    # 二维码用微信原生支付码（直接付），中转页作为兜底按钮
    assert qr == "weixin://wxpay/bizpayurl?pr=ifbmtaEz1"
    assert pay_page and pay_page.startswith("https://")
    assert "已创建订单" in text


def test_format_order_created_no_pay():
    # 被券/余额全额覆盖：needPay=false → 免扫码，不返回二维码
    covered = {"success": True, "data": {"orderIdStr": "1", "needPay": False,
                                         "payOrderQrCodeUrl": "https://x/qr", "discountPrice": 0}}
    text, qr, order_id, need_pay, pay_page = flows.format_order_created(covered)
    assert need_pay is False
    assert qr is None
    assert pay_page is None
    assert "无需扫码" in text


def test_format_order_status():
    assert "等待取餐" in flows.format_order_status(STATUS)
    assert "A123" in flows.format_order_status(STATUS)


def test_order_brief():
    assert flows.order_brief(STATUS) == "等待取餐 · 取餐码 A123"
    pending = {"success": True, "data": {"orderStatus": 10, "orderStatusName": "待付款",
                                         "takeMealCodeInfo": {"code": "生成中"}}}
    assert flows.order_brief(pending) == "待付款"  # 取餐码生成中不展示


def test_preview_summary():
    assert flows.preview_summary(PREVIEW) == "耶加雪菲拿铁×1"
    assert flows.preview_summary({"data": {}}) == "订单"


def test_cancel_succeeded():
    env = lambda d, s=True, c=0: {"code": c, "msg": "ok", "data": d, "success": s}
    assert flows.cancel_succeeded(env(True)) is True
    assert flows.cancel_succeeded(env(1)) is True       # 容忍 1
    assert flows.cancel_succeeded(env("true")) is True  # 容忍 "true"
    assert flows.cancel_succeeded(env(False)) is False
    assert flows.cancel_succeeded({"code": 5, "msg": "已支付", "data": None, "success": False}) is False


def test_cancel_message():
    assert flows.cancel_message({"code": 5, "msg": "订单已支付，不可取消"}) == "订单已支付，不可取消"
    assert "可能" in flows.cancel_message({"data": False})


def test_spend_guard():
    # limit is 100 (conftest). fresh user -> 50 ok, 150 blocked.
    assert flows.spend_guard(2001, 50.0) is None
    assert flows.spend_guard(2001, 150.0) is not None
    # accumulation pushes over the limit
    db.record_spend(2002, db.today_cst(), 80.0, "o")  # 与 spend_guard 同一天边界(CST)，避免 UTC CI 错位
    assert flows.spend_guard(2002, 30.0) is not None  # 80 + 30 > 100
    assert flows.spend_guard(2002, 10.0) is None       # 80 + 10 <= 100

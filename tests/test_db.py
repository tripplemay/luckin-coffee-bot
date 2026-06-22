import sqlite3

from core import db
from core.config import get_settings


def test_token_roundtrip_and_encrypted():
    db.set_token(1001, "tok-secret-xyz", token_date="2026-07-22")
    rec = db.get_token(1001)
    assert rec is not None
    assert rec.token == "tok-secret-xyz"
    assert rec.token_date == "2026-07-22"

    # stored blob must NOT contain the plaintext token
    raw = sqlite3.connect(get_settings().db_path).execute(
        "SELECT enc_token FROM user_tokens WHERE tg_user_id=1001"
    ).fetchone()[0]
    assert b"tok-secret-xyz" not in raw


def test_delete_token():
    db.set_token(1002, "tok2")
    db.delete_token(1002)
    assert db.get_token(1002) is None


def test_order_history():
    db.record_order(3001, "o-100", "美式×1")
    db.record_order(3001, "o-101", "拿铁×2")
    db.record_order(3002, "o-200", "其它")
    orders = db.list_orders(3001, limit=5)
    assert [o["order_id"] for o in orders] == ["o-101", "o-100"]  # 最新在前 (rowid 兜底)
    assert orders[0]["summary"] == "拿铁×2"
    # upsert 覆盖 summary，不新增行
    db.record_order(3001, "o-100", "美式×1改")
    o100 = [o for o in db.list_orders(3001) if o["order_id"] == "o-100"][0]
    assert o100["summary"] == "美式×1改"
    assert len(db.list_orders(3001)) == 2
    # COALESCE：用 None 再记不抹掉已有标签
    db.record_order(3001, "o-100", None)
    o100 = [o for o in db.list_orders(3001) if o["order_id"] == "o-100"][0]
    assert o100["summary"] == "美式×1改"
    # 取消 → 软删除，不再出现在列表
    db.mark_order_cancelled(3001, "o-101")
    assert [o["order_id"] for o in db.list_orders(3001)] == ["o-100"]
    # 用户隔离
    assert [o["order_id"] for o in db.list_orders(3002)] == ["o-200"]


def test_login_nonce():
    db.create_login_nonce("n1", 5005)
    assert db.consume_login_nonce("n1") == 5005
    assert db.consume_login_nonce("n1") is None  # 单次：第二次取不到
    assert db.consume_login_nonce("nope") is None
    db.create_login_nonce("n2", 6006)
    assert db.consume_login_nonce("n2", max_age=-1) is None  # 过期


def test_location_persistence():
    assert db.get_location(7001) is None
    db.set_location(7001, 116.39, 39.98, "北京安贞")
    loc = db.get_location(7001)
    assert loc["lng"] == 116.39 and loc["lat"] == 39.98 and loc["label"] == "北京安贞"
    db.set_location(7001, 104.06, 30.57, "成都天府五街")  # upsert 覆盖
    assert db.get_location(7001)["label"] == "成都天府五街"


def test_spend_tracking():
    db.record_spend(1003, "2026-06-22", 16.0, "o1")
    db.record_spend(1003, "2026-06-22", 13.5, "o2")
    db.record_spend(1003, "2026-06-23", 99.0, "o3")
    assert db.spend_today(1003, "2026-06-22") == 29.5
    assert db.spend_today(1003, "2026-06-23") == 99.0
    assert db.spend_today(9999, "2026-06-22") == 0.0

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
    rec = db.consume_login_nonce("n1")
    assert rec is not None and rec.user_key == 5005
    assert rec.channel is None and rec.push_target is None  # 未指定渠道时为空
    assert db.consume_login_nonce("n1") is None  # 单次：第二次取不到
    assert db.consume_login_nonce("nope") is None
    # 渠道 + 回推目标随 nonce 一并保存、取回
    db.create_login_nonce("n2", 6006, channel="tg", push_target="6006")
    rec2 = db.consume_login_nonce("n2")
    assert rec2.user_key == 6006 and rec2.channel == "tg" and rec2.push_target == "6006"
    db.create_login_nonce("n3", 7007)
    assert db.consume_login_nonce("n3", max_age=-1) is None  # 过期
    # peek：校验存在/未过期但不删除（发短信前的轻量准入）
    db.create_login_nonce("n4", 7008)
    assert db.peek_login_nonce("n4") is True
    assert db.peek_login_nonce("n4") is True  # 不删除，可重复 peek
    assert db.peek_login_nonce("n4", max_age=-1) is False  # 过期
    assert db.peek_login_nonce("nope") is False
    assert db.peek_login_nonce("") is False


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


# ---- 用户偏好 (P0) ----

def test_prefs_unset_returns_none():
    assert db.get_prefs(4101) is None


def test_prefs_partial_merge_scalars():
    db.set_prefs(4102, temperature="热")
    assert db.get_prefs(4102)["temperature"] == "热"
    # 后续只设甜度，不该抹掉温度
    db.set_prefs(4102, sweetness="少糖")
    p = db.get_prefs(4102)
    assert p["temperature"] == "热" and p["sweetness"] == "少糖"
    assert p["cup_size"] is None
    # 未传的字段保持不动；显式传空串/None 清空单个字段
    db.set_prefs(4102, temperature="")
    p = db.get_prefs(4102)
    assert p["temperature"] is None and p["sweetness"] == "少糖"


def test_prefs_lists_replace_add_remove():
    # 整体替换
    db.set_prefs(4103, dietary=["牛奶"])
    assert db.get_prefs(4103)["dietary"] == ["牛奶"]
    # 增量添加（去重）
    db.set_prefs(4103, dietary_add=["椰子", "牛奶"])
    assert sorted(db.get_prefs(4103)["dietary"]) == ["椰子", "牛奶"]
    # 增量删除
    db.set_prefs(4103, dietary_remove=["牛奶"])
    assert db.get_prefs(4103)["dietary"] == ["椰子"]
    # usual + notes 同表共存，互不影响标量列
    db.set_prefs(4103, usual=["生椰拿铁"], notes="不要太烫", nickname="老板")
    p = db.get_prefs(4103)
    assert p["usual"] == ["生椰拿铁"] and p["notes"] == "不要太烫" and p["nickname"] == "老板"
    assert p["dietary"] == ["椰子"]  # 之前的忌口仍在
    # dietary=None 清空列表，但不影响 usual
    db.set_prefs(4103, dietary=None)
    p = db.get_prefs(4103)
    assert p["dietary"] == [] and p["usual"] == ["生椰拿铁"]


def test_prefs_clear_all():
    db.set_prefs(4104, temperature="冰", usual=["美式"])
    assert db.get_prefs(4104) is not None
    db.clear_prefs(4104)
    assert db.get_prefs(4104) is None
    db.clear_prefs(4104)  # 重复清除是无害 no-op


def test_prefs_none_user_key_is_noop():
    # 绝不写 NULL 主键，绝不抛异常
    db.set_prefs(None, temperature="热")
    assert db.get_prefs(None) is None
    n = sqlite3.connect(get_settings().db_path).execute(
        "SELECT COUNT(*) FROM user_prefs WHERE user_key IS NULL"
    ).fetchone()[0]
    assert n == 0


def test_prefs_user_isolation():
    db.set_prefs(4105, nickname="A")
    db.set_prefs(4106, nickname="B")
    assert db.get_prefs(4105)["nickname"] == "A"
    assert db.get_prefs(4106)["nickname"] == "B"


def test_prefs_empty_merge_leaves_no_junk_row():
    # 新用户只发"移除某忌口" → 合并后全空，不应建空行
    db.set_prefs(4107, dietary_remove=["牛奶"])
    assert db.get_prefs(4107) is None
    # 设一个字段再清空它 → 行被删除，get_prefs 回 None（而非空 dict）
    db.set_prefs(4108, temperature="热")
    db.set_prefs(4108, temperature="")
    assert db.get_prefs(4108) is None


# ---- 老样子复购：下单 payload 捕获 (P0) ----

def test_order_payload_capture_and_read():
    pl = '[{"amount": 1, "productId": 11, "skuCode": "SK-冰大杯"}]'
    db.record_order(4201, "o-pay1", "冰美式×1", product_list=pl, dept_id="D9", lng=104.06, lat=30.57)
    p = db.get_last_order_payload(4201)
    assert p is not None
    assert p["order_id"] == "o-pay1" and p["dept_id"] == "D9"
    assert p["lng"] == 104.06 and p["lat"] == 30.57
    assert p["product_list"] == [{"amount": 1, "productId": 11, "skuCode": "SK-冰大杯"}]
    assert p["summary"] == "冰美式×1"


def test_order_payload_skips_no_payload_and_cancelled():
    # 无 payload 的旧单不应作为复购候选
    db.record_order(4202, "o-nopay", "拿铁×1")
    assert db.get_last_order_payload(4202) is None
    # 有 payload 但已取消，也应被过滤
    db.record_order(4202, "o-cxl", "美式×1", product_list='[{"amount":1,"productId":1,"skuCode":"S"}]', dept_id="D1")
    db.mark_order_cancelled(4202, "o-cxl")
    assert db.get_last_order_payload(4202) is None
    # 新的有效 payload 单则可取
    db.record_order(4202, "o-ok", "生椰拿铁×1", product_list='[{"amount":1,"productId":2,"skuCode":"S2"}]', dept_id="D2")
    assert db.get_last_order_payload(4202)["order_id"] == "o-ok"


def test_list_recent_payloads_newest_first():
    for i in range(3):
        db.record_order(4203, f"o-{i}", f"单{i}", product_list=f'[{{"amount":1,"productId":{i},"skuCode":"S{i}"}}]', dept_id="D")
    items = db.list_recent_payloads(4203, limit=5)
    assert [it["order_id"] for it in items] == ["o-2", "o-1", "o-0"]
    assert all(isinstance(it["product_list"], list) for it in items)


def test_record_order_payload_coalesce_keeps_existing():
    # 先记 summary（无 payload），再补 payload，二者都应保留（单写入口 COALESCE）
    db.record_order(4204, "o-c", "卡布×1")
    db.record_order(4204, "o-c", None, product_list='[{"amount":1,"productId":3,"skuCode":"S3"}]', dept_id="D3")
    p = db.get_last_order_payload(4204)
    assert p["summary"] == "卡布×1" and p["dept_id"] == "D3"


def test_orders_legacy_migration(tmp_path, monkeypatch):
    # 旧库 orders 表缺新列 → init_db 应逐列补齐
    dbfile = str(tmp_path / "legacy.db")
    monkeypatch.setattr(get_settings(), "db_path", dbfile)
    conn = sqlite3.connect(dbfile)
    conn.executescript(
        "CREATE TABLE orders (tg_user_id INTEGER NOT NULL, order_id TEXT NOT NULL, "
        "summary TEXT, created_at INTEGER NOT NULL, cancelled_at INTEGER, "
        "PRIMARY KEY (tg_user_id, order_id));"
    )
    conn.commit()
    conn.close()
    db.init_db()
    cols = [r[1] for r in sqlite3.connect(dbfile).execute("PRAGMA table_info(orders)").fetchall()]
    for c in ("product_list", "dept_id", "lng", "lat"):
        assert c in cols
    # 迁移后 payload 写读可用
    db.record_order(1, "o-mig", "美式×1", product_list='[{"amount":1,"productId":1,"skuCode":"S"}]', dept_id="D1")
    assert db.get_last_order_payload(1)["dept_id"] == "D1"

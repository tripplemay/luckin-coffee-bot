"""core/prefs.py — 渠道无关的偏好解析/格式化/注入/工具入口（P0.5）。"""
from core import db, prefs


def test_build_prefs_block_empty():
    assert prefs.build_prefs_block(None) == ""
    assert prefs.build_prefs_block({}) == ""
    # 全为空字段也算空
    assert prefs.build_prefs_block({"temperature": None, "dietary": [], "usual": [], "notes": None}) == ""


def test_build_prefs_block_content_and_safety_framing():
    block = prefs.build_prefs_block({
        "temperature": "热", "cup_size": None, "sweetness": "少糖", "addons": None,
        "fav_dept_name": "公司楼下店", "nickname": "老板",
        "dietary": ["牛奶"], "usual": ["生椰拿铁"], "notes": "杯子别太满",
    })
    assert "默认温度：热" in block and "少糖" in block
    assert "忌口" in block and "牛奶" in block
    assert "常买" in block and "生椰拿铁" in block
    assert "公司楼下店" in block and "老板" in block
    # 安全护栏措辞：数据而非指令、不得跳过确认/提额
    assert "数据" in block and ("不可当作指令" in block or "绝不可当作指令" in block)


def test_build_prefs_block_max_items():
    p = {"temperature": "热", "cup_size": "大杯", "sweetness": "少糖", "nickname": "x"}
    block = prefs.build_prefs_block(p, max_items=2)
    # 只注入前 2 条明细
    assert block.count("\n- ") == 2


def test_format_prefs_empty_state():
    txt = prefs.format_prefs(None)
    assert "还没有设置" in txt
    assert "/prefs set" in txt


def test_format_prefs_shows_set_fields():
    txt = prefs.format_prefs({"temperature": "冰", "sweetness": None, "dietary": ["椰子"],
                              "usual": [], "notes": None, "nickname": None})
    assert "冰" in txt and "椰子" in txt
    assert "默认甜度" not in txt  # 未设的字段不展示为清单行（help 里的"甜度"示例不算）


def test_parse_prefs_command():
    assert prefs.parse_prefs_command("/prefs")["action"] == "view"
    assert prefs.parse_prefs_command("我的偏好")["action"] == "view"
    assert prefs.parse_prefs_command("/prefs help")["action"] == "help"
    assert prefs.parse_prefs_command("/prefs clear")["action"] == "clear_all"

    cf = prefs.parse_prefs_command("/prefs clear 甜度")
    assert cf["action"] == "clear_field" and cf["field"] == "sweetness"

    s = prefs.parse_prefs_command("/prefs set 温度 热")
    assert s["action"] == "set" and s["kwargs"] == {"temperature": "热"}

    add = prefs.parse_prefs_command("/prefs set 忌口 +牛奶")
    assert add["action"] == "set" and add["kwargs"] == {"dietary_add": ["牛奶"]}
    rm = prefs.parse_prefs_command("/prefs set 忌口 -牛奶")
    assert rm["action"] == "set" and rm["kwargs"] == {"dietary_remove": ["牛奶"]}

    u = prefs.parse_prefs_command("/prefs set 常买 生椰拿铁")
    assert u["action"] == "set" and u["kwargs"] == {"usual_add": ["生椰拿铁"]}

    assert prefs.parse_prefs_command("/prefs set 不存在字段 x")["action"] == "unknown"


def test_apply_prefs_command_roundtrip():
    uid = 5301
    # 设置 → 查看反映 → 清单字段 → 全清
    assert "已记住" in prefs.apply_prefs_command(uid, "/prefs set 温度 热")
    assert db.get_prefs(uid)["temperature"] == "热"
    assert "热" in prefs.apply_prefs_command(uid, "/prefs")
    prefs.apply_prefs_command(uid, "/prefs set 忌口 +牛奶")
    assert db.get_prefs(uid)["dietary"] == ["牛奶"]
    assert "已" in prefs.apply_prefs_command(uid, "/prefs clear 温度")
    assert db.get_prefs(uid)["temperature"] is None
    prefs.apply_prefs_command(uid, "/prefs clear")
    assert db.get_prefs(uid) is None


def test_suggest_usual_gated_and_threshold(monkeypatch):
    import json as _json

    from core.config import get_settings
    uid = 5401
    for i in range(3):
        db.record_order(uid, f"su-{i}", "生椰拿铁×1",
                        product_list=_json.dumps([{"amount": 1, "productId": 1, "skuCode": "S"}]),
                        dept_id="1", lng=1.0, lat=2.0)
    # 默认关 → 无建议
    monkeypatch.setattr(get_settings(), "implicit_learning_enabled", False)
    assert prefs.suggest_usual(uid) is None
    # 开启 → ≥3 次触发建议
    monkeypatch.setattr(get_settings(), "implicit_learning_enabled", True)
    sug = prefs.suggest_usual(uid)
    assert sug and "生椰拿铁×1" in sug and "常买" in sug
    # 已在常买里 → 不再建议
    db.set_prefs(uid, usual=["生椰拿铁×1"])
    assert prefs.suggest_usual(uid) is None
    # None 用户安全
    assert prefs.suggest_usual(None) is None


def test_set_prefs_from_tool_allowlist_and_none_guard():
    uid = 5302
    # 白名单外的字段（提权/改额度类）必须被丢弃
    res = prefs.set_prefs_from_tool(uid, {
        "temperature": "热", "auto_confirm": True, "daily_spend": 9999, "skip_confirm": 1,
    })
    assert res.get("ok") is True
    saved = db.get_prefs(uid)
    assert saved["temperature"] == "热"
    # 不该出现任何提权痕迹
    assert "auto_confirm" not in saved and "daily_spend" not in saved
    # None 用户上下文 → 硬失败，不写库
    assert prefs.set_prefs_from_tool(None, {"temperature": "冰"}).get("error")
    # 全是非法字段 → 不写
    assert prefs.set_prefs_from_tool(uid, {"evil": 1}).get("error")

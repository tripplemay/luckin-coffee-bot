"""web /api/location 集成测试：nonce → WGS-84→GCJ-02 → 落库（不触网）。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from core import db, push
from web.app import app


def test_set_location_converts_and_stores(monkeypatch):
    async def _noop(*a, **k):
        return True

    monkeypatch.setattr(push, "push_to_channel", _noop)  # 不真发回推
    db.create_login_nonce("loc-n1", 4242, channel="tg", push_target="4242")

    with TestClient(app) as client:
        r = client.post("/api/location", json={"t": "loc-n1", "lat": 39.90923, "lng": 116.397428})
    assert r.status_code == 200 and r.json()["ok"] is True

    loc = db.get_location(4242)
    assert loc is not None
    # 落库的是 GCJ-02：北京点应比 WGS-84 原值向东北偏移（lng、lat 都更大）
    assert loc["lng"] > 116.397428 and loc["lat"] > 39.90923

    # nonce 单次：再用即失效
    with TestClient(app) as client:
        r2 = client.post("/api/location", json={"t": "loc-n1", "lat": 39.9, "lng": 116.4})
    assert r2.status_code == 400


def test_landing_renders_telegram_only(monkeypatch):
    from web import app as webapp
    monkeypatch.setattr(webapp, "get_settings", lambda: type("S", (), {"bot_username": "mybot"})())
    with TestClient(webapp.app) as client:
        r = client.get("/")
    assert r.status_code == 200
    assert "mybot" in r.text and "{{BOT_USERNAME}}" not in r.text
    assert "t.me" in r.text  # Telegram 入口在
    assert "微信号" not in r.text  # 不再有"搜索微信号添加"那套错误引导


def test_set_location_rejects_bad_coords(monkeypatch):
    async def _noop(*a, **k):
        return True

    monkeypatch.setattr(push, "push_to_channel", _noop)
    db.create_login_nonce("loc-n2", 4243, channel="tg", push_target="4243")
    with TestClient(app) as client:
        r = client.post("/api/location", json={"t": "loc-n2", "lat": 999, "lng": 116.4})
    assert r.status_code == 400
    assert db.get_location(4243) is None  # 越界坐标未落库
    assert db.peek_login_nonce("loc-n2") is True  # 坏坐标不烧 nonce，仍可重试

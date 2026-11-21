"""过期拒确认红测（甲路径：过期整笔回滚拒写）。

红测一：把占位过期时刻拨到过去后确认，必须 409 且整笔回滚——
正式册零增、占位行零改、档期席位零改、审计零残留、无确认回执。
红测二：两人抢确认同一张过期占位，后到者只记争用，业务数据零写。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import app  # noqa: E402
from db import connect, init_db  # noqa: E402
import db as dbmod  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(dbmod, "DB_PATH", db)
    if db.exists():
        db.unlink()
    init_db()
    yield


def _login(c: TestClient, user: str, password: str = "123456"):
    r = c.post("/api/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _create_hold(c: TestClient, headers: dict, party: str = "P-EXP", seats: int = 2):
    r = c.post(
        "/api/holds",
        headers=headers,
        json={"slot_code": "S-AM-01", "party_key": party, "seats": seats},
    )
    assert r.status_code == 200, r.text
    assert r.json()["disposition"] == "占位"
    return r.json()["hold_id"]


def _expire_in_past(hold_id: int) -> str:
    """夹具：把过期时刻拨到过去（本地文件库直写）。"""
    past = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute("UPDATE holds SET expire_at=? WHERE id=?", (past, hold_id))
        conn.commit()
    return past


def _snapshot(hold_id: int):
    with connect() as conn:
        hold = conn.execute("SELECT * FROM holds WHERE id=?", (hold_id,)).fetchone()
        slot = conn.execute("SELECT * FROM slots WHERE code='S-AM-01'").fetchone()
        return {
            "hold_status": hold["status"],
            "expire_at": hold["expire_at"],
            "bookings": conn.execute("SELECT COUNT(*) c FROM bookings").fetchone()["c"],
            "slot_booked": slot["booked"],
            "audit": conn.execute("SELECT COUNT(*) c FROM audit_trail").fetchone()["c"],
            "contention": conn.execute(
                "SELECT COUNT(*) c FROM contention_log"
            ).fetchone()["c"],
        }


def test_expired_hold_confirm_rejected_with_full_rollback():
    """红测一：写过期仍确认成功——过期单不得晋升，整笔回滚拒写。"""
    with TestClient(app) as c:
        h = _login(c, "admin")
        hold_id = _create_hold(c, h)
        past = _expire_in_past(hold_id)
        before = _snapshot(hold_id)

        r = c.post("/api/bookings/confirm", headers=h, json={"hold_id": hold_id})

        # 拒写：无确认回执
        assert r.status_code == 409, r.text
        assert "booking_id" not in r.json()
        after = _snapshot(hold_id)

        # 五处同态：过期时刻列 / 确认回执 / 正式册行数 / 对账席位 / 审计条
        assert after["expire_at"] == past, "过期时刻列不得被确认链路改写"
        assert after["hold_status"] == "占位中", "甲路径整笔回滚：占位行零改"
        assert after["bookings"] == before["bookings"], "正式册零增"
        assert after["slot_booked"] == before["slot_booked"], "档期席位零改"
        assert after["audit"] == before["audit"], "回滚后不得残留半截审计"

        # 对账席位仍互证
        recon = c.get("/api/reconcile", headers=h).json()
        assert recon["all_ok"] is True
        am = [x for x in recon["items"] if x["slot_code"] == "S-AM-01"][0]
        assert am["slot_booked"] == am["booking_seats"] == before["slot_booked"]

        # 该占位任何后续确认依旧被拒，永远进不了正式册
        r2 = c.post("/api/bookings/confirm", headers=h, json={"hold_id": hold_id})
        assert r2.status_code == 409
        assert _snapshot(hold_id)["bookings"] == before["bookings"]


def test_two_clerks_race_confirm_expired_hold_only_contention_logged():
    """红测二：双人抢确认过期单，档锁互斥，后到只记争用、业务零写。"""
    with TestClient(app) as c:
        admin = _login(c, "admin")
        clerk = _login(c, "clerk")
        hold_id = _create_hold(c, admin, party="P-RACE")
        _expire_in_past(hold_id)
        lock_key = "slot:S-AM-01"
        # 先到者持档锁（该端点自身会记一条锁争用审计，与确认链路无关，
        # 因此业务基线在持锁之后再取）
        rl = c.post("/api/locks/acquire", headers=admin, json={"lock_key": lock_key})
        assert rl.status_code == 200, rl.text
        before = _snapshot(hold_id)

        # 后到者抢同一档期的确认：争用拒写
        rc = c.post("/api/bookings/confirm", headers=clerk, json={"hold_id": hold_id})
        assert rc.status_code == 409, rc.text
        assert "booking_id" not in rc.json()
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM contention_log WHERE lock_key=? AND actor=?",
                (lock_key, "clerk"),
            ).fetchone()
        assert row is not None, "后到者必须留争用记录"

        mid = _snapshot(hold_id)
        assert mid["bookings"] == before["bookings"], "争用方不得写正式册"
        assert mid["slot_booked"] == before["slot_booked"], "争用方不得改席位"
        assert mid["hold_status"] == "占位中", "争用方不得改占位行"
        assert mid["audit"] == before["audit"], "争用方不得写业务审计"

        # 先到者拿到锁后确认过期单：依旧过期拒写、整笔回滚
        rr = c.post("/api/bookings/confirm", headers=admin, json={"hold_id": hold_id})
        assert rr.status_code == 409, rr.text
        assert "booking_id" not in rr.json()

        after = _snapshot(hold_id)
        assert after["bookings"] == before["bookings"]
        assert after["slot_booked"] == before["slot_booked"]
        assert after["hold_status"] == "占位中"
        assert after["expire_at"] == before["expire_at"]
        # 争用记录恰好一条，且没有任何一方晋升成功
        assert after["contention"] == before["contention"] + 1

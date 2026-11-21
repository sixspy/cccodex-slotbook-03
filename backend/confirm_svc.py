"""确认入册：占位 → 正式预订，写快照 generation。

过期拒确认卡 fixtures/expired_confirm_card.json（2026-11-21 封卡）：
只走甲路径——确认发现 expire_at 已过，正式册零增、占位行零改、
档期席位零改、不出确认回执，整笔回滚后拒写，拒写与回滚同成败。
禁止与乙路径（过期影子核销切换）混用。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, require_writer
from db import connect, write_audit
from locks import log_contention, release, require_lock

router = APIRouter(tags=["confirm"])


class ConfirmIn(BaseModel):
    hold_id: int


@router.get("/api/bookings")
def list_bookings(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT b.*, s.code AS slot_code FROM bookings b
               JOIN slots s ON s.id=b.slot_id ORDER BY b.id DESC LIMIT 200"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/bookings/confirm")
def confirm_hold(body: ConfirmIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        hold = conn.execute("SELECT * FROM holds WHERE id=?", (body.hold_id,)).fetchone()
        if not hold:
            raise HTTPException(404, "占位不存在")
        slot = conn.execute("SELECT * FROM slots WHERE id=?", (hold["slot_id"],)).fetchone()
        if not slot:
            raise HTTPException(404, "档期不存在")
        lock_key = f"slot:{slot['code']}"
        # 档锁互斥：抢不到锁时争用登记独立落库（不随业务事务回滚），业务零写
        try:
            require_lock(conn, lock_key, user)
        except HTTPException:
            conn.rollback()
            log_contention(lock_key, user, "confirm_contention", f"hold={body.hold_id}")
            raise
        try:
            hold = conn.execute("SELECT * FROM holds WHERE id=?", (body.hold_id,)).fetchone()
            if hold["status"] != "占位中":
                raise HTTPException(409, "占位已非占位中，禁止确认")
            now = datetime.now().isoformat(timespec="seconds")
            if hold["expire_at"] < now:
                # 甲路径：过期整笔回滚拒写。本事务内不得有任何残留写入，
                # 占位行（含 expire_at）、正式册、档期席位一律零改。
                conn.rollback()
                raise HTTPException(409, "占位已过期，确认整笔回滚拒写")
            remain = int(slot["capacity"]) - int(slot["booked"])
            if hold["seats"] > remain:
                raise HTTPException(409, "确认时容量不足，请释放或改候补")
            cur = conn.execute(
                """INSERT INTO bookings(slot_id,hold_id,party_key,seats,snapshot_gen,status,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    slot["id"],
                    hold["id"],
                    hold["party_key"],
                    hold["seats"],
                    slot["generation"],
                    "已确认",
                    now,
                ),
            )
            conn.execute(
                "UPDATE holds SET status='已确认' WHERE id=?", (hold["id"],)
            )
            conn.execute(
                "UPDATE slots SET booked = booked + ? WHERE id=?",
                (hold["seats"], slot["id"]),
            )
            write_audit(
                conn,
                user,
                "booking_confirm",
                hold["party_key"],
                {"booking_id": cur.lastrowid, "gen": slot["generation"]},
            )
            conn.commit()
            return {"ok": True, "booking_id": cur.lastrowid, "snapshot_gen": slot["generation"]}
        finally:
            release(conn, lock_key, user)
            conn.commit()

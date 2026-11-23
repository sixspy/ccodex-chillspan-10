"""温谱导出：正式/影子分轨，差额未清禁正式导出，影子须结清后晋升合并。"""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release, try_acquire
from models import SpectrumIn
from reconcile_svc import batch_gap, record_gap_ledger

router = APIRouter(tags=["spectrum"])

FORMAL = "formal"
SHADOW = "shadow"
SHADOW_STATUS = "影子待晋升"
FORMAL_STATUS = "已导出"


def _recon_lock_key(batch_id: int) -> str:
    return f"recon:batch:{batch_id}"


def _batch_with_chamber(conn, batch_id: int):
    return conn.execute(
        """SELECT b.*, c.chamber_code FROM temp_batches b
           JOIN cold_chambers c ON c.id=b.chamber_id WHERE b.id=?""",
        (batch_id,),
    ).fetchone()


def _checksum(batch) -> tuple[bytes, str, str, list]:
    points = json.loads(batch["temp_points"] or "[]")
    payload = json.dumps(
        {
            "batch_code": batch["batch_code"],
            "generation": batch["generation"],
            "points": points,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    ref = f"SP-{batch['batch_code']}-G{batch['generation']}"
    return payload, checksum, ref, points


@router.get("/api/spectrum")
def list_exports(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT s.*, b.batch_code FROM spectrum_exports s
               JOIN temp_batches b ON b.id=s.batch_id ORDER BY s.id DESC"""
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["gap"] = batch_gap(conn, int(r["batch_id"]))
            items.append(d)
        return {"items": items}


@router.post("/api/spectrum/export")
def export_spectrum(body: SpectrumIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    mode = body.mode or FORMAL
    if mode not in (FORMAL, SHADOW):
        raise HTTPException(422, "导出模式非法（仅 formal / shadow）")

    lock_key = _recon_lock_key(body.batch_id)
    conn = connect()
    try:
        acquired = try_acquire(conn, lock_key, user)
        conn.commit()
        if not acquired:
            raise HTTPException(409, "争用：后到只记争用，不写业务")

        batch = _batch_with_chamber(conn, body.batch_id)
        if not batch:
            raise HTTPException(404, "批次不存在")
        if batch["status"] != "已封批":
            raise HTTPException(409, "须先封批再导出温谱")

        gap = batch_gap(conn, body.batch_id)
        gap_uncleared = bool(gap and gap["uncleared"])
        _, checksum, ref, points = _checksum(batch)
        existing = conn.execute(
            "SELECT * FROM spectrum_exports WHERE export_ref=?", (ref,)
        ).fetchone()

        if mode == SHADOW:
            if gap_uncleared is False and existing and existing["status"] == FORMAL_STATUS:
                raise HTTPException(409, "正式温谱已导出，无需影子包")
            if existing:
                raise HTTPException(409, "该代温谱已导出或待晋升")
            cur = conn.execute(
                """INSERT INTO spectrum_exports(batch_id,export_ref,point_count,checksum,
                   status,created_at) VALUES (?,?,?,?,?,?)""",
                (
                    body.batch_id,
                    ref,
                    len(points),
                    checksum,
                    SHADOW_STATUS,
                    now_iso(),
                ),
            )
            record_gap_ledger(
                conn,
                batch_id=body.batch_id,
                batch_code=batch["batch_code"],
                chamber_code=batch["chamber_code"],
                action="影子导出",
                detail=f"差额未清影子包 {ref}，不对外",
                actor=user,
            )
            write_audit(
                conn,
                user,
                "spectrum_shadow",
                ref,
                {"checksum": checksum, "gap_uncleared": gap_uncleared},
            )
            conn.commit()
            return {
                "id": cur.lastrowid,
                "export_ref": ref,
                "point_count": len(points),
                "checksum": checksum,
                "status": SHADOW_STATUS,
                "formal_export_delta": 0,
            }

        # 正式导出：差额未清一律拒收，零增且不改对账状态。
        if gap_uncleared:
            detail = {
                "reason": "差额未清，禁止正式导出温谱",
                "gap": gap,
                "formal_export_delta": 0,
            }
            record_gap_ledger(
                conn,
                batch_id=body.batch_id,
                batch_code=batch["batch_code"],
                chamber_code=batch["chamber_code"],
                action="正式导出拒收",
                detail=json.dumps(detail, ensure_ascii=False),
                actor=user,
            )
            write_audit(conn, user, "spectrum_reject", ref, detail)
            raise HTTPException(409, detail)

        if existing and existing["status"] == FORMAL_STATUS:
            raise HTTPException(409, "该代温谱已导出")

        if existing and existing["status"] == SHADOW_STATUS:
            # 差额已结清：影子包晋升合并为正式温谱。
            conn.execute(
                "UPDATE spectrum_exports SET status=? WHERE id=?",
                (FORMAL_STATUS, existing["id"]),
            )
            record_gap_ledger(
                conn,
                batch_id=body.batch_id,
                batch_code=batch["batch_code"],
                chamber_code=batch["chamber_code"],
                action="影子晋升",
                detail=f"差额结清，影子包 {ref} 晋升合并",
                actor=user,
            )
            write_audit(conn, user, "spectrum_promote", ref, {"checksum": checksum})
            conn.commit()
            return {
                "id": existing["id"],
                "export_ref": ref,
                "point_count": existing["point_count"],
                "checksum": existing["checksum"],
                "status": FORMAL_STATUS,
                "promoted": True,
                "formal_export_delta": 1,
            }

        cur = conn.execute(
            """INSERT INTO spectrum_exports(batch_id,export_ref,point_count,checksum,
               status,created_at) VALUES (?,?,?,?,?,?)""",
            (
                body.batch_id,
                ref,
                len(points),
                checksum,
                FORMAL_STATUS,
                now_iso(),
            ),
        )
        write_audit(conn, user, "spectrum_export", ref, {"checksum": checksum})
        conn.commit()
        return {
            "id": cur.lastrowid,
            "export_ref": ref,
            "point_count": len(points),
            "checksum": checksum,
            "status": FORMAL_STATUS,
            "formal_export_delta": 1,
        }
    finally:
        release(conn, lock_key, user)
        conn.commit()
        conn.close()


@router.get("/api/spectrum/{export_ref}")
def get_export(export_ref: str, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            """SELECT s.*, b.temp_points, b.batch_code, b.chamber_id, c.chamber_code
               FROM spectrum_exports s
               JOIN temp_batches b ON b.id=s.batch_id
               JOIN cold_chambers c ON c.id=b.chamber_id
               WHERE s.export_ref=?""",
            (export_ref,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "导出不存在")
        if row["status"] == SHADOW_STATUS:
            # 影子包未晋升不得对外，记偏离台账并拒写。
            record_gap_ledger(
                conn,
                batch_id=int(row["batch_id"]),
                batch_code=row["batch_code"],
                chamber_code=row["chamber_code"],
                action="差额导出偏航",
                detail=f"影子包 {export_ref} 未晋升即请求对外",
                actor=user,
            )
            write_audit(
                conn,
                user,
                "spectrum_shadow_leak",
                export_ref,
                {"gap": dict(batch_gap(conn, int(row["batch_id"])) or {})},
            )
            conn.commit()
            raise HTTPException(409, "影子包未晋升，禁止对外")
        data = dict(row)
        data["temp_points"] = json.loads(data["temp_points"] or "[]")
        data["gap"] = batch_gap(conn, int(row["batch_id"]))
        return data

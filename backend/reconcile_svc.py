"""仓间配额 vs 在批差额对账 + 互证只读 + 差额导出台账（单一差额口径）。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import connect, now_iso

router = APIRouter(tags=["reconcile"])

# 五页（对账、温谱、批次、封批、血缘）差额展示唯一来源。
GAP_SOURCE = "reconcile_svc:chamber_quota_vs_active:v1"


def chamber_gap(conn, chamber_id: int) -> dict:
    """差额口径：在批 - 配额 > 0 即未清（差额未清）。"""
    chamber = conn.execute(
        "SELECT * FROM cold_chambers WHERE id=?", (chamber_id,)
    ).fetchone()
    if not chamber:
        raise HTTPException(404, "仓间不存在")
    quota = int(chamber["quota_batches"])
    active = conn.execute(
        """SELECT COUNT(*) c FROM temp_batches
           WHERE chamber_id=? AND status NOT IN ('已封批','作废')""",
        (chamber_id,),
    ).fetchone()["c"]
    sealed = conn.execute(
        """SELECT COUNT(*) c FROM temp_batches
           WHERE chamber_id=? AND status='已封批'""",
        (chamber_id,),
    ).fetchone()["c"]
    active = int(active)
    gap = active - quota
    return {
        "chamber_id": int(chamber["id"]),
        "chamber_code": chamber["chamber_code"],
        "region": chamber["region"],
        "quota_batches": quota,
        "active_batches": active,
        "sealed_batches": int(sealed),
        "gap": gap,
        "balanced": active <= quota,
        "uncleared": gap > 0,
        "source": GAP_SOURCE,
    }


def batch_gap(conn, batch_id: int) -> dict | None:
    row = conn.execute(
        "SELECT chamber_id FROM temp_batches WHERE id=?", (batch_id,)
    ).fetchone()
    if not row:
        return None
    return chamber_gap(conn, int(row["chamber_id"]))


def record_gap_ledger(
    conn,
    *,
    batch_id: int | None,
    batch_code: str,
    chamber_code: str,
    action: str,
    detail: str,
    actor: str,
    status: str = "封口",
) -> int:
    """定稿写入差额导出台账，当日封口。"""
    cur = conn.execute(
        """INSERT INTO recon_gap_ledger(batch_id,batch_code,chamber_code,day,
           action,status,detail,actor,created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            batch_id,
            batch_code,
            chamber_code,
            date.today().isoformat(),
            action,
            status,
            detail,
            actor,
            now_iso(),
        ),
    )
    return int(cur.lastrowid)


def _chamber_recon(conn, chamber) -> dict:
    g = chamber_gap(conn, int(chamber["id"]))
    return {
        **g,
        "quota_left": g["quota_batches"] - g["active_batches"],
        "proof": {
            "formula": "active_batches <= quota_batches",
            "left": g["active_batches"],
            "right": g["quota_batches"],
        },
    }


@router.get("/api/reconcile")
def reconcile_all(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        chambers = conn.execute("SELECT * FROM cold_chambers ORDER BY id").fetchall()
        items = [_chamber_recon(conn, c) for c in chambers]
        bad = [i for i in items if not i["balanced"]]
        ledger = conn.execute(
            "SELECT * FROM recon_gap_ledger ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {
            "items": items,
            "gap_ledger": [dict(r) for r in ledger],
            "summary": {
                "total": len(items),
                "balanced": len(items) - len(bad),
                "unbalanced": len(bad),
                "gap_uncleared_batches": sum(
                    i["active_batches"] - i["quota_batches"]
                    for i in bad
                ),
            },
        }


@router.get("/api/reconcile/{chamber_code}")
def reconcile_one(chamber_code: str, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        ch = conn.execute(
            "SELECT * FROM cold_chambers WHERE chamber_code=?", (chamber_code,)
        ).fetchone()
        if not ch:
            raise HTTPException(404, "仓间不存在")
        return _chamber_recon(conn, ch)


@router.get("/api/proof/batch/{batch_code}")
def batch_proof(batch_code: str, authorization: AuthHeader = None):
    """批次互证：封批代 + 温谱导出 + 开放游标 + 差额。"""
    require_user(authorization)
    with connect() as conn:
        batch = conn.execute(
            "SELECT * FROM temp_batches WHERE batch_code=?", (batch_code,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, "批次不存在")
        open_exc = conn.execute(
            """SELECT COUNT(*) c FROM excursion_cursors
               WHERE batch_id=? AND status='开放'""",
            (batch["id"],),
        ).fetchone()["c"]
        exports = conn.execute(
            """SELECT COUNT(*) c FROM spectrum_exports
               WHERE batch_id=? AND status='已导出'""",
            (batch["id"],),
        ).fetchone()["c"]
        sealed = batch["status"] == "已封批"
        spectrum_ok = (exports >= 1) if sealed else True
        return {
            "batch_code": batch_code,
            "status": batch["status"],
            "generation": batch["generation"],
            "open_excursions": open_exc,
            "spectrum_exports": exports,
            "sealed": sealed,
            "excursion_closed": open_exc == 0,
            "spectrum_matches_seal": spectrum_ok,
            "gap": batch_gap(conn, int(batch["id"])),
            "proof": {
                "formula": "sealed => spectrum>=1 and open_excursions==0",
                "sealed": sealed,
                "open_excursions": open_exc,
                "exports": exports,
            },
        }

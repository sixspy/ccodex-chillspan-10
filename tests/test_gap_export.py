"""差额未清 vs 温谱导出分轨红测（乙路径：影子包 + 结清后晋升合并）。

三路：
- 失败测甲：差额未清时正式温谱零增拒收，对账状态零改，台账当日封口。
- 失败测乙：影子包未晋升不得对外；差额结清后正式导出晋升合并。
- 争用失败测：双人同批拉温谱须抢对账锁，后到争用留痕且零写。

业务码未改前，本文件必须红；改码后三测必须绿。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db

ROOT = Path(__file__).resolve().parents[1]
GAP_FIXTURE = ROOT / "fixtures/recon_gap.json"

SEALED_CODE = "TB-CK-GAP-05-003"
ACTIVE_CODE_A = "TB-CK-GAP-05-001"
ACTIVE_CODE_B = "TB-CK-GAP-05-002"

FORMAL_REJECT = "正式导出拒收"
DEVIATION = "差额导出偏航"
PROMOTION = "影子晋升"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _plant_gap() -> dict:
    """夹具入库：配额 1、在批 2（超额）+ 1 个已封批批次 => 差额未清。"""
    fx = json.loads(GAP_FIXTURE.read_text(encoding="utf-8"))
    ch = fx["chamber"]
    probe = fx["probe"]
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO cold_chambers(chamber_code,region,name,target_min,target_max,
               quota_batches,status,created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (
                ch["chamber_code"],
                ch["region"],
                ch["name"],
                ch["target_min"],
                ch["target_max"],
                ch["quota_batches"],
                ch["status"],
                "2026-11-23T08:00:00",
            ),
        )
        chamber_id = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO probes(probe_code,chamber_id,model,online_status,
               calibration_due,created_at) VALUES (?,?,?,?,?,?)""",
            (
                probe["probe_code"],
                chamber_id,
                probe["model"],
                probe["online_status"],
                probe["calibration_due"],
                "2026-11-23T08:00:00",
            ),
        )
        probe_id = cur.lastrowid
        ids: dict[str, int] = {}
        for b in fx["batches"]:
            generation = int(b.get("generation", 1))
            cur = conn.execute(
                """INSERT INTO temp_batches(batch_code,chamber_id,probe_id,cargo_ref,
                   temp_points,generation,status,min_observed,max_observed,created_at,sealed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    b["batch_code"],
                    chamber_id,
                    probe_id,
                    b["cargo_ref"],
                    json.dumps(b["temp_points"], ensure_ascii=False),
                    generation,
                    b["status"],
                    b.get("min_observed"),
                    b.get("max_observed"),
                    "2026-11-23T08:00:00",
                    b.get("sealed_at"),
                ),
            )
            ids[b["batch_code"]] = int(cur.lastrowid)
        sealed_id = ids[SEALED_CODE]
        conn.execute(
            """INSERT INTO seal_snapshots(batch_id,batch_generation,manifest,status,sealed_at)
               VALUES (?,?,?,?,?)""",
            (sealed_id, 2, "差额夹具封批清单", "已封批", "2026-11-20T09:00:00"),
        )
        conn.execute(
            """INSERT INTO lineage_edges(from_batch_id,to_batch_id,edge_type,detail,created_at)
               VALUES (?,?,?,?,?)""",
            (sealed_id, ids[ACTIVE_CODE_A], "分装衍生", "差额夹具血缘", "2026-11-21T10:00:00"),
        )
        conn.commit()
    return {
        "chamber_id": chamber_id,
        "probe_id": probe_id,
        "chamber_code": ch["chamber_code"],
        "batch_ids": ids,
    }


def _counts(batch_id: int) -> tuple[int, int]:
    with connect() as conn:
        formal = conn.execute(
            "SELECT COUNT(*) c FROM spectrum_exports WHERE batch_id=? AND status='已导出'",
            (batch_id,),
        ).fetchone()["c"]
        shadow = conn.execute(
            "SELECT COUNT(*) c FROM spectrum_exports WHERE batch_id=? AND status='影子待晋升'",
            (batch_id,),
        ).fetchone()["c"]
    return int(formal), int(shadow)


def _ledger(c: TestClient, h: dict, batch_code: str) -> list[dict]:
    rows = c.get("/api/reconcile", headers=h).json().get("gap_ledger", [])
    return [r for r in rows if r["batch_code"] == batch_code]


def _today() -> str:
    return date.today().isoformat()


def _sig(g: dict) -> tuple:
    return (
        g["quota_batches"],
        g["active_batches"],
        g["sealed_batches"],
        g["gap"],
        g["balanced"],
        g["uncleared"],
        g["source"],
    )


def _assert_same_source(gaps: list[dict]) -> None:
    assert len(gaps) == 5, f"五页证据不足：{len(gaps)}"
    sigs = {_sig(g) for g in gaps}
    assert len(sigs) == 1, f"五页差额不同源：{sigs}"


def _five_page_gaps(
    c: TestClient, h: dict, seed: dict, sealed_code: str, spectrum_gap: dict
) -> list[dict]:
    rec = c.get(f"/api/reconcile/{seed['chamber_code']}", headers=h).json()
    batches = {
        b["batch_code"]: b for b in c.get("/api/batches", headers=h).json()["items"]
    }
    seals = {
        s["batch_code"]: s for s in c.get("/api/seals", headers=h).json()["items"]
    }
    edges = c.get("/api/lineage", headers=h).json()["items"]
    edge = next(e for e in edges if e["from_code"] == sealed_code)
    return [
        rec,
        spectrum_gap,
        batches[sealed_code]["gap"],
        seals[sealed_code]["gap"],
        edge["gap"],
    ]


def test_formal_export_rejected_when_gap_uncleared():
    """失败测甲：未清差额仍出正式温谱 => 红；改后零增拒收、对账零改。"""
    c = TestClient(app)
    h = _auth(c)
    seed = _plant_gap()
    batch_id = seed["batch_ids"][SEALED_CODE]

    recon_before = c.get(
        f"/api/reconcile/{seed['chamber_code']}", headers=h
    ).json()
    assert recon_before["balanced"] is False
    assert recon_before["uncleared"] is True
    assert _counts(batch_id) == (0, 0)

    r = c.post(
        "/api/spectrum/export", headers=h, json={"batch_id": batch_id}
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict) and detail["gap"]["uncleared"] is True

    assert _counts(batch_id) == (0, 0), "差额未清仍新增了温谱导出"

    recon_after = c.get(
        f"/api/reconcile/{seed['chamber_code']}", headers=h
    ).json()
    assert recon_after == recon_before, "对账状态被导出拒收动作改写"

    led = _ledger(c, h, SEALED_CODE)
    assert any(
        x["action"] == FORMAL_REJECT and x["status"] == "封口" and x["day"] == _today()
        for x in led
    ), "差额导出台账未当日封口"

    _assert_same_source(
        _five_page_gaps(c, h, seed, SEALED_CODE, detail["gap"])
    )


def test_shadow_blocked_until_gap_cleared_then_promoted():
    """失败测乙：影子包未晋升却对外 => 红；结清后晋升合并，否则偏航拒写。"""
    c = TestClient(app)
    h = _auth(c)
    seed = _plant_gap()
    batch_id = seed["batch_ids"][SEALED_CODE]

    r = c.post(
        "/api/spectrum/export",
        headers=h,
        json={"batch_id": batch_id, "mode": "shadow"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "影子待晋升"
    assert body["formal_export_delta"] == 0
    ref = body["export_ref"]
    assert _counts(batch_id) == (0, 1)

    spectrum_item = next(
        x
        for x in c.get("/api/spectrum", headers=h).json()["items"]
        if x["export_ref"] == ref
    )
    _assert_same_source(
        _five_page_gaps(c, h, seed, SEALED_CODE, spectrum_item["gap"])
    )

    # 影子未晋升却对外 => 拒收 + 偏航入账，温谱零改
    outward = c.get(f"/api/spectrum/{ref}", headers=h)
    assert outward.status_code == 409, outward.text
    assert "影子" in outward.text
    assert _counts(batch_id) == (0, 1)
    assert any(
        x["action"] == DEVIATION and x["status"] == "封口" for x in _ledger(c, h, SEALED_CODE)
    ), "偏离台账未记差额导出偏航"

    # 未清差额下正式导出仍拒收
    blocked = c.post(
        "/api/spectrum/export", headers=h, json={"batch_id": batch_id}
    )
    assert blocked.status_code == 409, blocked.text
    assert _counts(batch_id) == (0, 1)

    # 结清差额：封批一条在批（封批会减少在批计数，属正常结清动作）
    seal = c.post(
        "/api/seals",
        headers=h,
        json={"batch_id": seed["batch_ids"][ACTIVE_CODE_A]},
    )
    assert seal.status_code == 200, seal.text
    recon = c.get(f"/api/reconcile/{seed['chamber_code']}", headers=h).json()
    assert recon["balanced"] is True
    assert recon["uncleared"] is False

    # 正式导出触发晋升合并
    promoted = c.post(
        "/api/spectrum/export", headers=h, json={"batch_id": batch_id}
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["status"] == "已导出"
    assert _counts(batch_id) == (1, 0)
    assert any(
        x["action"] == PROMOTION and x["status"] == "封口"
        for x in _ledger(c, h, SEALED_CODE)
    ), "晋升未入差额导出台账"

    outward2 = c.get(f"/api/spectrum/{ref}", headers=h)
    assert outward2.status_code == 200
    assert len(outward2.json()["temp_points"]) == 2


def test_two_sessions_contend_on_same_gap_batch_export():
    """争用失败测：双人同批抢导出 => 红；改后后到 409、争用留痕、零写。"""
    c = TestClient(app)
    h_admin = _auth(c, "admin")
    h_ops = _auth(c, "ops")
    seed = _plant_gap()
    batch_id = seed["batch_ids"][SEALED_CODE]
    lock_key = f"recon:batch:{batch_id}"

    # 先到会话已持有对账锁（模拟同批拉温谱在途）
    assert (
        c.post(
            "/api/locks/acquire", headers=h_admin, json={"lock_key": lock_key}
        ).status_code
        == 200
    )

    r = c.post(
        "/api/spectrum/export",
        headers=h_ops,
        json={"batch_id": batch_id, "mode": "shadow"},
    )
    assert r.status_code == 409, r.text

    cont = c.get("/api/contention", headers=h_admin).json()["items"]
    assert any(
        x["lock_key"] == lock_key and x["actor"] == "ops" for x in cont
    ), "后到争用未留痕"

    assert _counts(batch_id) == (0, 0), "争用后到仍写了温谱"

    c.post("/api/locks/release", headers=h_admin, json={"lock_key": lock_key})

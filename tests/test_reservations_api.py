from __future__ import annotations

from fastapi.testclient import TestClient


def _setup_material(client, headers, *, accession_no: str, lot_no: str, grams: float = 500) -> tuple[int, int]:
    source = client.post("/api/germplasm/sources", headers=headers, json={
        "source_code": f"S-{accession_no}", "provider_name": "合作站", "country_code": "CN",
        "locality": "北方站", "restrictions": {},
    })
    assert source.status_code == 201, source.text
    accession = client.post("/api/germplasm/accessions", headers=headers, json={
        "accession_no": accession_no, "scientific_name": "Triticum aestivum", "crop_name": "小麦",
        "source_id": source.json()["id"], "acquisition_type": "交换",
        "received_on": "2026-09-20", "passport": {}, "created_by": "登记员",
    })
    assert accession.status_code == 201, accession.text
    accepted = client.post(f"/api/germplasm/accessions/{accession.json()['id']}/transition", headers=headers, json={
        "target_status": "accepted", "reason": "手续齐全", "expected_version": 1, "actor": "审核员",
    })
    assert accepted.status_code == 200, accepted.text
    location = client.post("/api/germplasm/locations", headers=headers, json={
        "location_code": f"L-{lot_no}", "facility": "中期库", "room": "一室", "rack": "A", "shelf": "1",
        "capacity_grams": 3000, "temperature_c": 4, "humidity_percent": 35,
    })
    assert location.status_code == 201, location.text
    lot = client.post("/api/germplasm/lots", headers=headers, json={
        "lot_no": lot_no, "accession_id": accession.json()["id"], "harvest_year": 2025,
        "initial_weight_grams": grams, "moisture_percent": 8, "treatment": "清选", "created_by": "登记员",
    })
    assert lot.status_code == 201, lot.text
    placed = client.post("/api/germplasm/placements", headers=headers, json={
        "lot_id": lot.json()["id"], "location_id": location.json()["id"], "weight_grams": grams,
        "container_code": f"B-{lot_no}", "idempotency_key": f"place-{lot_no}", "actor": "保管员",
    })
    assert placed.status_code == 201, placed.text
    return accession.json()["id"], lot.json()["id"]


def test_reservation_http_lifecycle_and_evidence(client, admin):
    headers = admin["headers"]
    accession_id, lot_id = _setup_material(client, headers, accession_no="HTTP-R-1", lot_no="HTTP-LOT-R1")
    created = client.post("/api/germplasm/distributions", headers=headers, json={
        "request_no": "HTTP-D-1", "requester": "作物研究所", "purpose": "抗旱鉴定",
        "items": [{"accession_id": accession_id, "quantity_grams": 120}],
    })
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]
    submitted = client.post(f"/api/germplasm/distributions/{request_id}/submit?expected_version=1", headers=headers)
    assert submitted.status_code == 200, submitted.text
    decided = client.post(f"/api/germplasm/distributions/{request_id}/decision", headers=headers, json={
        "approve": True, "expected_version": 2, "actor": "资源审核员", "reason": "同意", "reservation_days": 5,
    })
    assert decided.status_code == 200, decided.text
    reservation = decided.json()["reservation"]
    assert reservation["status"] == "active"
    assert reservation["expires_at"].endswith("+00:00") or "T" in reservation["expires_at"]
    rid = reservation["id"]

    # 申请与批次查询都展示可用/已预约/已出库数量。
    detail = client.get(f"/api/germplasm/distributions/{request_id}", headers=headers).json()
    assert detail["reservation"]["reserved_grams"] == 120
    lot_detail = client.get(f"/api/germplasm/lots/{lot_id}", headers=headers).json()
    balance = lot_detail["balance"]
    assert balance["on_hand_grams"] == 500
    assert balance["reserved_grams"] == 120
    assert balance["available_for_reservation_grams"] == 380
    assert balance["distributed_grams"] == 0

    # 无权限（未认证）不能拣货。
    picking = client.post(f"/api/germplasm/reservations/{rid}/picking", json={"actor": "拣货员甲"})
    assert picking.status_code == 401
    picking = client.post(f"/api/germplasm/reservations/{rid}/picking", headers=headers, json={"actor": "拣货员甲"})
    assert picking.status_code == 200, picking.text

    line_id = reservation["lines"][0]["id"]
    pick = client.post(f"/api/germplasm/reservation-lines/{line_id}/picks", headers=headers, json={
        "quantity_grams": 120, "idempotency_key": "http-pick-1", "actor": "拣货员甲",
        "shipment_no": "HTTP-SH-1", "consignee": "作物研究所", "reason": "",
    })
    assert pick.status_code == 201, pick.text
    assert pick.json()["reservation"]["status"] == "fulfilled"

    lot_detail = client.get(f"/api/germplasm/lots/{lot_id}", headers=headers).json()
    assert lot_detail["balance"]["on_hand_grams"] == 380
    assert lot_detail["balance"]["distributed_grams"] == 120

    # 批次视角的分配证据：顺序、申请、规则、最终去向。
    evidence = client.get(f"/api/germplasm/lots/{lot_id}/reservations", headers=headers).json()
    allocation = evidence["allocations"][0]
    assert allocation["request_no"] == "HTTP-D-1"
    assert allocation["sequence_no"] == 1
    assert "latest_valid_viability" in allocation["rule"]["selected_by"]
    assert evidence["balance"]["distributed_grams"] == 120
    line_view = client.get(f"/api/germplasm/distributions/{request_id}/reservation", headers=headers).json()
    assert line_view["lines"][0]["fulfillments"][0]["shipment_no"] == "HTTP-SH-1"
    assert line_view["lines"][0]["fulfillments"][0]["consignee"] == "作物研究所"
    assert {event["action"] for event in line_view["events"]} >= {"created", "pick_started", "picked", "fulfilled"}


def test_expired_reservation_reclaimed_on_service_restart(client, admin):
    import os

    from app.database import close_connection
    from app.main import app

    headers = admin["headers"]
    accession_id, lot_id = _setup_material(client, headers, accession_no="HTTP-R-2", lot_no="HTTP-LOT-R2")
    created = client.post("/api/germplasm/distributions", headers=headers, json={
        "request_no": "HTTP-D-2", "requester": "x", "purpose": "田间鉴定",
        "items": [{"accession_id": accession_id, "quantity_grams": 80}],
    })
    request_id = created.json()["id"]
    client.post(f"/api/germplasm/distributions/{request_id}/submit?expected_version=1", headers=headers)
    decided = client.post(f"/api/germplasm/distributions/{request_id}/decision", headers=headers, json={
        "approve": True, "expected_version": 2, "actor": "资源审核员", "reservation_days": 1,
    })
    rid = decided.json()["reservation"]["id"]

    # 手动把到期时间改到过去，然后重启服务，启动钩子应自动回收。
    import sqlite3

    close_connection()
    connection = sqlite3.connect(os.environ["GERMPLASM_DATABASE_PATH"], timeout=30)
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("UPDATE reservations SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (rid,))
    connection.commit()
    connection.close()

    with TestClient(app) as restarted:
        reclaim = restarted.post("/api/germplasm/reservations/reclaim-expired", headers=headers)
        # 启动时已经回收，接口返回 0 条待回收。
        assert reclaim.status_code == 200
        assert reclaim.json()["reclaimed_count"] == 0
        reservation = restarted.get(f"/api/germplasm/reservations/{rid}", headers=headers).json()
        assert reservation["status"] == "released"
        assert reservation["release_reason"] == "expired"
        lot_detail = restarted.get(f"/api/germplasm/lots/{lot_id}", headers=headers).json()
        assert lot_detail["balance"]["reserved_grams"] == 0
        request_row = restarted.get(f"/api/germplasm/distributions/{request_id}", headers=headers).json()
        assert request_row["status"] == "submitted"

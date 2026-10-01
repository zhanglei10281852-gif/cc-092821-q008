from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.clock import FrozenClock
from app.core.errors import ConflictError
from app.database import transaction
from app.germplasm.service import GermplasmService
from tests.test_germplasm_workflow import create_accepted_accession


def make_lot(service: GermplasmService, accession: dict, suffix: str, weight: float, sealed_on: str, harvest_year: int) -> dict:
    location = service.inventory.create_location({
        "location_code": f"LOC-{suffix}", "facility": "长期库", "room": "低温一室", "rack": "R1", "shelf": "S1",
        "capacity_grams": 10_000, "temperature_c": -18, "humidity_percent": 30,
    })
    lot = service.inventory.create_lot({
        "lot_no": f"LOT-{suffix}", "accession_id": accession["id"], "parent_lot_id": None,
        "harvest_year": harvest_year, "initial_weight_grams": weight, "moisture_percent": 7.5,
        "treatment": "清选干燥", "sealed_on": sealed_on, "created_by": "登记员",
    })
    service.inventory.place_lot({
        "lot_id": lot["id"], "location_id": location["id"], "weight_grams": weight,
        "container_code": f"BOX-{suffix}", "idempotency_key": f"place-{suffix}", "actor": "保管员",
    })
    return service.repository.lot_detail(lot["id"])


def submit_request(service: GermplasmService, accession: dict, quantity: float, no: str) -> dict:
    request = service.distribution.create_request({
        "request_no": no, "requester": "外部研究所", "purpose": "抗旱鉴定",
        "items": [{"accession_id": accession["id"], "quantity_grams": quantity}],
    })
    return service.distribution.submit(request["id"], 1)


def approve(service: GermplasmService, request: dict, hours: int = 72) -> dict:
    return service.distribution.decide(request["id"], {
        "approve": True, "expected_version": request["version"], "actor": "审核员甲",
        "reason": "材料充足", "reservation_hours": hours,
    })


@pytest.fixture()
def clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 10, 1, 9, 0, tzinfo=UTC))


@pytest.fixture()
def service(clock: FrozenClock, tmp_path):
    import os

    from app.database import close_connection, init_db

    os.environ["GERMPLASM_DATABASE_PATH"] = str(tmp_path / "reservations.db")
    close_connection()
    init_db()
    with transaction(immediate=True) as connection:
        svc = GermplasmService(connection, clock)
        yield svc
    close_connection()


def test_approval_spreads_across_lots_in_fefo_order_and_locks_weight(service):
    accession = create_accepted_accession(service, "A")
    early = make_lot(service, accession, "EARLY", 30, "2026-09-01", 2024)
    later = make_lot(service, accession, "LATER", 500, "2026-09-10", 2025)
    request = submit_request(service, accession, 50, "DIST-FEFO")
    approved = approve(service, request)

    assert approved["status"] == "approved"
    reservations = approved["reservations"]
    assert len(reservations) == 2
    assert reservations[0]["lot_id"] == early["id"]
    assert reservations[0]["quantity_grams"] == 30
    assert reservations[0]["allocation_rank"] == 1
    assert reservations[1]["lot_id"] == later["id"]
    assert reservations[1]["quantity_grams"] == 20
    # 来源规则写进快照，可供出库人员举证
    rules = reservations[0]["rule_snapshot"]["rules"]
    assert any(rule.startswith("fefo:") for rule in rules)
    assert any(rule.startswith("viability:") for rule in rules)

    early_view = service.repository.lot_detail(early["id"])
    assert early_view["quantities"] == {
        "on_hand_grams": 30.0, "reserved_grams": 30.0, "picking_grams": 0.0,
        "available_grams": 0.0, "outbound_grams": 0.0,
    }
    line = approved["items"][0]["quantities"]
    assert line["reserved_grams"] == 50
    assert line["outbound_grams"] == 0


def test_insufficient_stock_aborts_whole_approval_without_locks(service):
    accession = create_accepted_accession(service, "B")
    make_lot(service, accession, "SMALL", 30, "2026-09-01", 2025)
    request = submit_request(service, accession, 50, "DIST-SHORT")
    with pytest.raises(ConflictError):
        approve(service, request)
    request = service.repository.require_distribution(request["id"])
    assert request["status"] == "submitted"
    assert service.connection.execute("SELECT COUNT(*) FROM lot_reservations").fetchone()[0] == 0
    lot_view = service.repository.lot_detail(
        service.connection.execute("SELECT id FROM seed_lots WHERE lot_no='LOT-SMALL'").fetchone()[0]
    )
    assert lot_view["quantities"]["reserved_grams"] == 0.0


def test_concurrent_approvals_cannot_oversell_and_replay_does_not_relock(service):
    accession = create_accepted_accession(service, "C")
    lot = make_lot(service, accession, "ONLY", 50, "2026-09-01", 2025)
    first = submit_request(service, accession, 40, "DIST-C1")
    second = submit_request(service, accession, 40, "DIST-C2")
    third = submit_request(service, accession, 5, "DIST-C3")

    approved = approve(service, first)
    assert approved["status"] == "approved"

    # 第二份申请并发审批：余额仅剩 10g，必须失败且不写入任何预约
    with pytest.raises(ConflictError):
        approve(service, second)
    # 审批重放（相同 expected_version）不得重复占用
    with pytest.raises(ConflictError):
        service.distribution.decide(first["id"], {
            "approve": True, "expected_version": first["version"], "actor": "审核员乙",
            "reservation_hours": 72,
        })
    active = service.connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(quantity_grams),0) FROM lot_reservations WHERE lot_id=? AND status='active'",
        (lot["id"],),
    ).fetchone()
    assert active[0] == 1 and active[1] == 40

    # 剩余 10g 中 5g 仍可被第三份申请预约
    approved_third = approve(service, third)
    assert approved_third["reservations"][0]["quantity_grams"] == 5


def test_withdraw_is_blocked_by_reserved_weight_until_reclaim(service, clock: FrozenClock):
    accession = create_accepted_accession(service, "D")
    lot = make_lot(service, accession, "LOCK", 50, "2026-09-01", 2025)
    request = submit_request(service, accession, 40, "DIST-LOCK")
    approve(service, request, hours=1)

    with pytest.raises(ConflictError) as exc:
        service.inventory.withdraw({
            "lot_id": lot["id"], "quantity_grams": 20, "movement_type": "领用",
            "idempotency_key": "withdraw-locked-1", "actor": "保管员", "reason": "试验",
        })
    assert exc.value.context["free_grams"] == 10

    # 到期后由回收任务释放，重量回到可动用余额
    clock.advance(hours=2)
    result = service.distribution.reclaim_expired()
    assert result["released_count"] == 1
    withdrawal = service.inventory.withdraw({
        "lot_id": lot["id"], "quantity_grams": 20, "movement_type": "领用",
        "idempotency_key": "withdraw-locked-2", "actor": "保管员", "reason": "试验",
    })
    assert withdrawal["lot"]["available_weight_grams"] == 30
    expired = service.repository.require_distribution(request["id"])
    assert expired["status"] == "expired"
    reservation = service.repository.reservation_detail(result["reservations"][0])
    assert reservation["status"] == "released"
    assert "到期" in reservation["release_reason"]
    assert any(event["event_type"] == "released" for event in reservation["events"])


def test_cancel_request_releases_balance(service):
    accession = create_accepted_accession(service, "E")
    lot = make_lot(service, accession, "CAN", 100, "2026-09-01", 2025)
    request = submit_request(service, accession, 60, "DIST-CANCEL")
    approve(service, request)
    cancelled = service.distribution.cancel(request["id"], {"actor": "申请人", "reason": "研究计划取消"})
    assert cancelled["status"] == "cancelled"
    quantities = service.repository.lot_detail(lot["id"])["quantities"]
    assert quantities["reserved_grams"] == 0
    assert quantities["available_grams"] == 100


def test_quality_hold_releases_active_reservation_but_picking_must_be_rolled_back(service, clock: FrozenClock):
    accession = create_accepted_accession(service, "F")
    lot_a = make_lot(service, accession, "HOLD-A", 30, "2026-09-01", 2024)
    lot_b = make_lot(service, accession, "HOLD-B", 500, "2026-09-10", 2025)
    request = submit_request(service, accession, 50, "DIST-HOLD")
    approved = approve(service, request)
    first_reservation, second_reservation = approved["reservations"]

    service.reservations.start_picking(first_reservation["id"], "出库员张三")
    hold = service.inventory.impose_hold({
        "lot_id": lot_a["id"], "hold_type": "质量", "reason": "复检异常", "actor": "审核员",
    })
    # 拣货中的预约不能被自动释放
    assert hold["released_reservations"] == []
    detail_a = service.repository.reservation_detail(first_reservation["id"])
    assert detail_a["status"] == "picking"

    # 另一批次仍处于 active，冻结它时立即释放
    hold_b = service.inventory.impose_hold({
        "lot_id": lot_b["id"], "hold_type": "质量", "reason": "含水量异常", "actor": "审核员",
    })
    assert hold_b["released_reservations"] == [second_reservation["id"]]
    detail_b = service.repository.reservation_detail(second_reservation["id"])
    assert detail_b["status"] == "released"
    assert "质量状态恶化" in detail_b["release_reason"]

    # 拣货中的预约只能由有权限的人回退：到期回收也不会动它，整单取消被拒绝
    clock.advance(hours=96)
    assert service.reservations.sweep_expired() == []
    assert service.repository.reservation_detail(first_reservation["id"])["status"] == "picking"
    with pytest.raises(ConflictError):
        service.reservations.cancel_request(request["id"], "申请人", "不要了")
    # 回退时发现批次已冻结：释放余额并记录原因，而不是把预约留在不可发放批次上
    rolled_back = service.reservations.rollback(first_reservation["id"], "主管", "暂停发放等待复核")
    assert rolled_back["status"] == "released"
    assert "不可发放" in rolled_back["release_reason"]


def test_picking_takeover_and_full_shipment_provenance(service):
    accession = create_accepted_accession(service, "G")
    lot_a = make_lot(service, accession, "SHIP-A", 30, "2026-09-01", 2024)
    make_lot(service, accession, "SHIP-B", 500, "2026-09-10", 2025)
    request = submit_request(service, accession, 50, "DIST-SHIP")
    approved = approve(service, request)
    r_a, r_b = approved["reservations"]

    service.reservations.start_picking(r_a["id"], "出库员张三")
    service.reservations.start_picking(r_b["id"], "出库员张三")
    taken = service.reservations.takeover(r_a["id"], "出库员李四", "张三换班")
    assert taken["picked_by"] == "出库员李四"

    ship_a = service.reservations.ship(r_a["id"], {
        "outbound_no": "OUT-1", "recipient": "外部研究所", "actor": "出库员李四", "note": "顺丰冷链",
    })
    assert ship_a["replayed"] is False
    assert ship_a["outbound"]["quantity_grams"] == 30
    # 出库再调用是幂等的，不会重复扣减
    replay = service.reservations.ship(r_a["id"], {
        "outbound_no": "OUT-1", "recipient": "外部研究所", "actor": "出库员李四",
    })
    assert replay["replayed"] is True

    lot_a_view = service.repository.lot_detail(lot_a["id"])
    assert lot_a_view["available_weight_grams"] == 0
    assert lot_a_view["status"] == "depleted"
    assert lot_a_view["quantities"]["outbound_grams"] == 30

    service.reservations.ship(r_b["id"], {
        "outbound_no": "OUT-2", "recipient": "外部研究所", "actor": "出库员李四",
    })
    final = service.repository.require_distribution(request["id"])
    assert final["status"] == "fulfilled"

    # API 可证明每份材料的分配顺序与最终去向
    trace = service.repository.reservation_detail(r_b["id"])
    chain = [event["event_type"] for event in trace["events"]]
    assert chain == ["created", "picking", "fulfilled"]
    assert trace["outbound"]["outbound_no"] == "OUT-2"
    assert trace["rule_snapshot"]["candidate_order"] == 2
    movement = service.connection.execute(
        "SELECT movement_type FROM lot_movements WHERE id=?", (trace["outbound"]["movement_id"],)
    ).fetchone()
    assert movement[0] == "出库发放"


def test_cannot_start_picking_after_expiry(service, clock: FrozenClock):
    accession = create_accepted_accession(service, "H")
    make_lot(service, accession, "EXP", 100, "2026-09-01", 2025)
    request = submit_request(service, accession, 40, "DIST-EXP")
    approved = approve(service, request, hours=1)
    reservation = approved["reservations"][0]
    clock.advance(hours=2)
    with pytest.raises(ConflictError):
        service.reservations.start_picking(reservation["id"], "出库员")
    detail = service.repository.reservation_detail(reservation["id"])
    assert detail["status"] == "released"


def test_low_viability_alert_releases_active_reservation(service):
    accession = create_accepted_accession(service, "I")
    lot = make_lot(service, accession, "VIA", 500, "2026-09-01", 2025)
    request = submit_request(service, accession, 20, "DIST-VIA")
    approved = approve(service, request)
    reservation = approved["reservations"][0]

    protocol = service.viability.create_protocol({
        "protocol_code": "RICE-LOW", "crop_name": "水稻", "sample_size": 100, "replicate_count": 1,
        "temperature_c": 25, "duration_days": 14, "normal_seedling_rule": "根芽完整", "created_by": "负责人",
    })
    test = service.viability.schedule_test({
        "test_no": "VT-LOW", "lot_id": lot["id"], "protocol_id": protocol["id"], "test_type": "周期复检",
        "sampled_grams": 5, "scheduled_for": "2026-10-02", "requested_by": "检测员",
        "idempotency_key": "schedule-low-1",
    })
    service.viability.start_test(test["id"], {"performed_by": "检测员", "expected_version": 1})
    service.viability.add_count(test["id"], {
        "replicate_no": 1, "seeds_tested": 100, "normal_count": 40,
        "abnormal_count": 20, "dead_count": 40, "fresh_count": 0,
        "observation_day": 14, "observed_by": "检测员",
    })
    service.viability.complete_test(test["id"], {"performed_by": "检测员", "expected_version": 2})

    detail = service.repository.reservation_detail(reservation["id"])
    assert detail["status"] == "released"
    assert "活力" in detail["release_reason"]


def test_held_and_restricted_lots_are_excluded_from_selection(service):
    accession = create_accepted_accession(service, "J")
    frozen = make_lot(service, accession, "FROZEN", 500, "2026-09-01", 2024)
    free = make_lot(service, accession, "FREE", 500, "2026-09-20", 2025)
    service.inventory.impose_hold({
        "lot_id": frozen["id"], "hold_type": "检疫", "reason": "待检疫", "actor": "审核员",
    })
    request = submit_request(service, accession, 20, "DIST-EXCLUDE")
    approved = approve(service, request)
    assert approved["reservations"][0]["lot_id"] == free["id"]


def test_http_reservation_lifecycle(client, admin):
    headers = admin["headers"]
    source = client.post("/api/germplasm/sources", headers=headers, json={
        "source_code": "HTTP-RSV-S", "provider_name": "合作站", "country_code": "CN", "restrictions": {},
    })
    accession = client.post("/api/germplasm/accessions", headers=headers, json={
        "accession_no": "HTTP-RSV-A", "scientific_name": "Triticum aestivum", "crop_name": "小麦",
        "source_id": source.json()["id"], "acquisition_type": "交换",
        "received_on": "2026-09-20", "passport": {}, "created_by": "登记员",
    })
    accession_id = accession.json()["id"]
    client.post(f"/api/germplasm/accessions/{accession_id}/transition", headers=headers, json={
        "target_status": "accepted", "reason": "手续齐全", "expected_version": 1, "actor": "审核员",
    })
    location = client.post("/api/germplasm/locations", headers=headers, json={
        "location_code": "HTTP-RSV-L", "facility": "中期库", "room": "一室", "rack": "A", "shelf": "1",
        "capacity_grams": 3000, "temperature_c": 4, "humidity_percent": 35,
    }).json()
    lot = client.post("/api/germplasm/lots", headers=headers, json={
        "lot_no": "HTTP-RSV-LOT", "accession_id": accession_id, "harvest_year": 2025,
        "initial_weight_grams": 100, "treatment": "清选", "sealed_on": "2026-09-25", "created_by": "登记员",
    }).json()
    client.post("/api/germplasm/placements", headers=headers, json={
        "lot_id": lot["id"], "location_id": location["id"], "weight_grams": 100,
        "container_code": "HTTP-RSV-BOX", "idempotency_key": "http-rsv-place", "actor": "保管员",
    })
    distribution = client.post("/api/germplasm/distributions", headers=headers, json={
        "request_no": "HTTP-RSV-D", "requester": "外部研究所", "purpose": "抗旱鉴定",
        "items": [{"accession_id": accession_id, "quantity_grams": 40}],
    }).json()
    client.post(f"/api/germplasm/distributions/{distribution['id']}/submit?expected_version=1", headers=headers)
    decision = client.post(f"/api/germplasm/distributions/{distribution['id']}/decision", headers=headers, json={
        "approve": True, "expected_version": 2, "actor": "审核员", "reservation_hours": 48,
    })
    assert decision.status_code == 200, decision.text
    reservation_id = decision.json()["reservations"][0]["id"]

    lot_view = client.get(f"/api/germplasm/lots/{lot['id']}", headers=headers).json()
    assert lot_view["quantities"]["reserved_grams"] == 40
    assert lot_view["quantities"]["available_grams"] == 60

    request_view = client.get(f"/api/germplasm/distributions/{distribution['id']}", headers=headers).json()
    assert request_view["items"][0]["quantities"]["reserved_grams"] == 40
    assert request_view["items"][0]["source_rules"]

    picking = client.post(f"/api/germplasm/reservations/{reservation_id}/picking", headers=headers, json={"actor": "出库员"})
    assert picking.status_code == 200, picking.text
    shipped = client.post(f"/api/germplasm/reservations/{reservation_id}/shipment", headers=headers, json={
        "outbound_no": "HTTP-OUT-1", "recipient": "外部研究所", "actor": "出库员",
    })
    assert shipped.status_code == 201, shipped.text

    trace = client.get(f"/api/germplasm/reservations/{reservation_id}/trace", headers=headers)
    assert trace.status_code == 200
    body = trace.json()
    assert body["outbound"]["outbound_no"] == "HTTP-OUT-1"
    assert [event["event_type"] for event in body["events"]] == ["created", "picking", "fulfilled"]
    assert any(rule.startswith("fefo:") for rule in body["allocation"]["source_rules"])

    final = client.get(f"/api/germplasm/distributions/{distribution['id']}", headers=headers).json()
    assert final["status"] == "fulfilled"


def test_reclaim_endpoint_requires_authentication(client):
    response = client.post("/api/germplasm/distributions/reclaim-expired")
    assert response.status_code == 401

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import FrozenClock
from app.core.errors import ConflictError
from app.database import close_connection, database_path, transaction
from app.germplasm.service import GermplasmService
from tests.test_germplasm_workflow import create_accepted_accession, create_stored_lot


def make_lot(service: GermplasmService, accession, suffix: str, weight: float = 500,
             sealed_on: str = "2026-09-02", harvest_year: int = 2025) -> dict:
    location = service.inventory.create_location({
        "location_code": f"COLD-R-{suffix}", "facility": "长期库", "room": "低温一室",
        "rack": "R1", "shelf": "S1", "capacity_grams": 10_000,
        "temperature_c": -18, "humidity_percent": 30,
    })
    lot = service.inventory.create_lot({
        "lot_no": f"LOT-R-{suffix}", "accession_id": accession["id"], "parent_lot_id": None,
        "harvest_year": harvest_year, "initial_weight_grams": weight, "moisture_percent": 7.5,
        "treatment": "清选干燥", "sealed_on": sealed_on, "created_by": "登记员",
    })
    service.inventory.place_lot({
        "lot_id": lot["id"], "location_id": location["id"], "weight_grams": weight,
        "container_code": f"BOX-R-{suffix}", "idempotency_key": f"place-r-{suffix}", "actor": "保管员",
    })
    return service.repository.lot_detail(lot["id"])


def submit_request(service: GermplasmService, accession, grams: float, number: str) -> dict:
    request = service.distribution.create_request({
        "request_no": number, "requester": "作物研究所", "purpose": "抗旱鉴定",
        "items": [{"accession_id": accession["id"], "quantity_grams": grams}],
    })
    return service.distribution.submit(request["id"], request["version"])


def approve(service: GermplasmService, request: dict, actor: str = "资源审核员", days: int = 7) -> dict:
    return service.distribution.decide(request["id"], {
        "approve": True, "expected_version": request["version"], "actor": actor,
        "reason": "材料充足", "reservation_days": days,
    })


def test_approval_locks_weight_and_reports_balances(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession, lot, _ = create_stored_lot(service)
        request = submit_request(service, accession, 120, "D-R-001")
        result = approve(service, request)
        assert result["status"] == "approved"
        reservation = result["reservation"]
        assert reservation["status"] == "active"
        assert reservation["reserved_grams"] == 120
        assert reservation["lines"][0]["quantity_grams"] == 120
        assert reservation["lines"][0]["rule"]["selected_by"][0] == "resource_restriction"
        balance = service.reservations.lot_balance(lot["id"])
        assert balance == {
            "lot_id": lot["id"], "on_hand_grams": 500.0, "reserved_grams": 120.0,
            "available_for_reservation_grams": 380.0, "distributed_grams": 0.0,
        }
        # 锁定重量不能被普通领用占用。
        with pytest.raises(ConflictError) as exc:
            service.inventory.withdraw({
                "lot_id": lot["id"], "quantity_grams": 400, "movement_type": "领用",
                "idempotency_key": "withdraw-lock-1", "actor": "保管员", "reason": "试验",
            })
        assert exc.value.context["reserved_grams"] == 120


def test_split_across_lots_uses_fefo_and_records_sequence(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession = create_accepted_accession(service, "split1")
        older = make_lot(service, accession, "old", weight=30, sealed_on="2025-01-10", harvest_year=2024)
        newer = make_lot(service, accession, "new", weight=500, sealed_on="2026-09-02", harvest_year=2026)
        request = submit_request(service, accession, 100, "D-R-002")
        result = approve(service, request)
        lines = result["reservation"]["lines"]
        assert [(line["lot_id"], line["quantity_grams"]) for line in lines] == [
            (older["id"], 30.0), (newer["id"], 70.0),
        ]
        assert [line["sequence_no"] for line in lines] == [1, 2]
        assert lines[0]["rule"]["sealed_on"] == "2025-01-10"
        assert service.reservations.lot_balance(older["id"])["reserved_grams"] == 30.0
        assert service.reservations.lot_balance(newer["id"])["reserved_grams"] == 70.0


def test_latest_valid_viability_ranks_ahead_of_sealed_date(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession = create_accepted_accession(service, "via1")
        tested = make_lot(service, accession, "tested", weight=500, sealed_on="2026-09-02")
        untested = make_lot(service, accession, "untested", weight=500, sealed_on="2020-01-01")
        protocol = service.viability.create_protocol({
            "protocol_code": "VIA-R", "crop_name": "水稻", "sample_size": 100, "replicate_count": 1,
            "temperature_c": 25, "duration_days": 14, "normal_seedling_rule": "根芽完整",
            "created_by": "技术负责人",
        })
        test = service.viability.schedule_test({
            "test_no": "VT-R-1", "lot_id": tested["id"], "protocol_id": protocol["id"],
            "test_type": "入库初检", "sampled_grams": 5, "scheduled_for": "2026-09-20",
            "requested_by": "检测员", "idempotency_key": "sch-via-1",
        })
        service.viability.start_test(test["id"], {"performed_by": "检测员", "expected_version": 1})
        service.viability.add_count(test["id"], {
            "replicate_no": 1, "seeds_tested": 100, "normal_count": 90,
            "abnormal_count": 5, "dead_count": 5, "fresh_count": 0,
            "observation_day": 14, "observed_by": "检测员",
        })
        service.viability.complete_test(test["id"], {"performed_by": "检测员", "expected_version": 2})
        request = submit_request(service, accession, 20, "D-R-003")
        lines = approve(service, request)["reservation"]["lines"]
        # 有最近有效活力结果的批次优先，即使其密封日期更晚。
        assert lines[0]["lot_id"] == tested["id"]
        assert lines[0]["rule"]["viability"]["germination_percent"] == 90.0
        assert untested["id"] not in {line["lot_id"] for line in lines}


def test_approval_replay_does_not_double_lock(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession, lot, _ = create_stored_lot(service)
        request = submit_request(service, accession, 120, "D-R-004")
        first = approve(service, request)
        request_row = service.repository.require_distribution(request["id"])
        # 重放：请求已批准，再次执行同一审批动作返回既有预约，不再占用重量。
        replay = service.distribution.decide(request["id"], {
            "approve": True, "expected_version": request_row["version"], "actor": "资源审核员",
            "reason": "重试", "reservation_days": 7,
        })
        assert replay["reservation"]["id"] == first["reservation"]["id"]
        assert service.reservations.lot_balance(lot["id"])["reserved_grams"] == 120.0
        assert len(service.reservations.reservation_detail(first["reservation"]["id"])["lines"]) == 1


def test_concurrent_approvals_never_oversell(client):
    close_connection()
    import sqlite3

    from app.database import _create_connection

    db_path = database_path()
    base = sqlite3.connect(db_path, timeout=30)
    base.row_factory = sqlite3.Row
    base.execute("PRAGMA journal_mode=WAL")
    base.execute("PRAGMA busy_timeout=30000")

    def setup() -> tuple[int, int]:
        service = GermplasmService(base)
        accession, lot, _ = create_stored_lot(service)
        r1 = submit_request(service, accession, 500, "D-R-C1")
        r2 = submit_request(service, accession, 500, "D-R-C2")
        base.commit()
        return r1["id"], r2["id"]

    request_1, request_2 = setup()
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def worker(request_id: int) -> None:
        connection = _create_connection()
        try:
            barrier.wait()
            service = GermplasmService(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                request = service.repository.require_distribution(request_id)
                service.distribution.decide(request_id, {
                    "approve": True, "expected_version": request["version"],
                    "actor": f"审核员-{request_id}", "reason": "并发", "reservation_days": 7,
                })
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            outcomes.append("approved")
        except ConflictError:
            outcomes.append("shortfall")
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(rid,)) for rid in (request_1, request_2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["approved", "shortfall"]
    verify = GermplasmService(base)
    lot_id = int(base.execute("SELECT id FROM seed_lots WHERE lot_no='LOT-001'").fetchone()[0])
    assert verify.reservations.lot_balance(lot_id)["reserved_grams"] == 500.0
    base.close()


def test_expired_reservation_is_reclaimed_and_can_be_reapproved(client):
    clock = FrozenClock(datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection, clock)
        accession, lot, _ = create_stored_lot(service)
        request = submit_request(service, accession, 120, "D-R-005")
        approve(service, request, days=3)
        assert service.reservations.lot_balance(lot["id"])["reserved_grams"] == 120.0
        clock.current += timedelta(days=3, minutes=1)
        reclaimed = service.reservations.expire_due()
        assert len(reclaimed) == 1
        assert reclaimed[0]["release_reason"] == "expired"
        assert reclaimed[0]["events"][-1]["action"] == "released"
        assert service.reservations.lot_balance(lot["id"])["reserved_grams"] == 0.0
        # 申请退回待审批，可以再次批准并生成新预约。
        request_row = service.repository.require_distribution(request["id"])
        assert request_row["status"] == "submitted"
        second = approve(service, request_row)
        assert second["reservation"]["status"] == "active"
        assert service.reservations.lot_balance(lot["id"])["reserved_grams"] == 120.0


def test_cancel_releases_balance(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession, lot, _ = create_stored_lot(service)
        request = submit_request(service, accession, 120, "D-R-006")
        approved = approve(service, request)
        detail = service.reservations.cancel(request["id"], "资源审核员", "机构撤回申请")
        assert detail["status"] == "released"
        assert detail["release_reason"] == "cancelled"
        assert service.repository.require_distribution(request["id"])["status"] == "cancelled"
        assert service.reservations.lot_balance(lot["id"])["reserved_grams"] == 0.0


def test_quality_hold_releases_active_lock_and_replenishes(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession = create_accepted_accession(service, "q1")
        first = make_lot(service, accession, "q-first", weight=100, sealed_on="2025-01-01")
        second = make_lot(service, accession, "q-second", weight=500, sealed_on="2026-01-01")
        request = submit_request(service, accession, 100, "D-R-007")
        approved = approve(service, request)
        assert approved["reservation"]["lines"][0]["lot_id"] == first["id"]
        # 质量状态恶化：施加冻结，原锁定释放并自动从次优批次补足。
        service.inventory.impose_hold({
            "lot_id": first["id"], "hold_type": "质量", "reason": "复检发芽率骤降", "actor": "质量员",
        })
        assert service.reservations.lot_balance(first["id"])["reserved_grams"] == 0.0
        detail = service.reservations.reservation_detail(approved["reservation"]["id"])
        active = [line for line in detail["lines"] if line["status"] == "active"]
        assert [(line["lot_id"], line["quantity_grams"]) for line in active] == [(second["id"], 100.0)]
        released = [line for line in detail["lines"] if line["status"] == "released"]
        assert released[0]["release_reason"] == "quality_hold"
        actions = [event["action"] for event in detail["events"]]
        assert "line_released" in actions and "replenished" in actions


def test_quality_hold_with_no_alternative_returns_request_to_submitted(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession, lot, _ = create_stored_lot(service)
        request = submit_request(service, accession, 120, "D-R-008")
        approved = approve(service, request)
        service.inventory.impose_hold({
            "lot_id": lot["id"], "hold_type": "质量", "reason": "霉变", "actor": "质量员",
        })
        detail = service.reservations.reservation_detail(approved["reservation"]["id"])
        assert detail["status"] == "released"
        assert detail["release_reason"] == "quality_hold"
        assert service.repository.require_distribution(request["id"])["status"] == "submitted"


def test_picking_reservation_requires_takeover_or_manager_rollback(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession, lot, _ = create_stored_lot(service)
        request = submit_request(service, accession, 100, "D-R-009")
        approved = approve(service, request)
        rid = approved["reservation"]["id"]
        service.reservations.start_picking(rid, "拣货员甲")
        # 拣货中不能取消、不能到期回收。
        with pytest.raises(ConflictError):
            service.reservations.cancel(request["id"], "资源审核员", "尝试取消")
        assert service.reservations.expire_due() == []
        # 拣货中施加质量冻结不会抢占锁定。
        service.inventory.impose_hold({
            "lot_id": lot["id"], "hold_type": "质量", "reason": "异常", "actor": "质量员",
        })
        assert service.reservations.lot_balance(lot["id"])["reserved_grams"] == 100.0
        # 有权限的人接管后整单回退，已拣重量退回库存。
        service.reservations.takeover(rid, "主管")
        rolled = service.reservations.rollback(rid, "主管", "质量异常，整单退回")
        assert rolled["status"] == "released"
        assert service.repository.require_distribution(request["id"])["status"] == "submitted"


def test_pick_fulfill_and_trace_final_destination(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession = create_accepted_accession(service, "pick1")
        lot_a = make_lot(service, accession, "pa", weight=30, sealed_on="2025-01-01")
        lot_b = make_lot(service, accession, "pb", weight=500, sealed_on="2026-01-01")
        request = submit_request(service, accession, 50, "D-R-010")
        approved = approve(service, request)
        rid = approved["reservation"]["id"]
        service.reservations.start_picking(rid, "拣货员甲")
        lines = service.reservations.reservation_detail(rid)["lines"]
        first_pick = service.reservations.pick(lines[0]["id"], {
            "quantity_grams": 30, "idempotency_key": "pick-010-a", "actor": "拣货员甲",
            "shipment_no": "SH-010", "consignee": "作物研究所", "reason": "",
        })
        assert first_pick["replayed"] is False
        replay = service.reservations.pick(lines[0]["id"], {
            "quantity_grams": 30, "idempotency_key": "pick-010-a", "actor": "拣货员甲",
            "shipment_no": "SH-010", "consignee": "作物研究所", "reason": "",
        })
        assert replay["replayed"] is True
        service.reservations.pick(lines[1]["id"], {
            "quantity_grams": 20, "idempotency_key": "pick-010-b", "actor": "拣货员甲",
            "shipment_no": "SH-010", "consignee": "作物研究所", "reason": "",
        })
        detail = service.reservations.reservation_detail(rid)
        assert detail["status"] == "fulfilled"
        assert service.repository.require_distribution(request["id"])["status"] == "fulfilled"
        assert service.reservations.lot_balance(lot_a["id"])["distributed_grams"] == 30.0
        assert service.reservations.lot_balance(lot_b["id"])["distributed_grams"] == 20.0
        # 批次视角的分配证据：顺序、申请、去向。
        allocations = service.reservations.lot_allocations(lot_a["id"])
        assert allocations[0]["request_no"] == "D-R-010"
        assert allocations[0]["sequence_no"] == 1
        assert allocations[0]["rule"]["lot_no"] == "LOT-R-pa"
        shipments = service.reservations.reservation_detail(rid)["lines"][0]["fulfillments"]
        assert shipments[0]["shipment_no"] == "SH-010"
        assert shipments[0]["consignee"] == "作物研究所"


def test_partial_pick_then_return_reopens_line(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession, lot, _ = create_stored_lot(service)
        request = submit_request(service, accession, 100, "D-R-011")
        rid = approve(service, request)["reservation"]["id"]
        service.reservations.start_picking(rid, "拣货员甲")
        line = service.reservations.reservation_detail(rid)["lines"][0]
        service.reservations.pick(line["id"], {
            "quantity_grams": 40, "idempotency_key": "pick-011", "actor": "拣货员甲",
            "shipment_no": "SH-011", "consignee": "x", "reason": "",
        })
        assert service.reservations.lot_balance(lot["id"])["on_hand_grams"] == 460.0
        returned = service.reservations.return_line(line["id"], {
            "quantity_grams": 40, "idempotency_key": "ret-011", "actor": "拣货员甲",
            "reason": "容器破损，退回重拣",
        })
        assert returned["line"]["status"] == "picking"
        assert returned["line"]["picked_grams"] == 0
        assert service.reservations.lot_balance(lot["id"])["on_hand_grams"] == 500.0
        with pytest.raises(ConflictError):
            service.reservations.return_line(line["id"], {
                "quantity_grams": 1, "idempotency_key": "ret-011-over", "actor": "拣货员甲",
                "reason": "超额回退",
            })


def test_restricted_resource_cannot_be_reserved(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession, lot, _ = create_stored_lot(service)
        request = submit_request(service, accession, 10, "D-R-012")
        # 提交后资源被限制发放（来源/护照限制同理），批准选批时必须被资源规则拦截。
        service.accessions.transition(accession["id"], {
            "target_status": "restricted", "reason": "MTA 限制对外发放",
            "expected_version": accession["version"], "actor": "审核员",
        })
        with pytest.raises(ConflictError):
            approve(service, request)


def test_insufficient_weight_fails_whole_approval(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession, lot, _ = create_stored_lot(service)
        first = submit_request(service, accession, 450, "D-R-013")
        approve(service, first)
        second = submit_request(service, accession, 100, "D-R-014")
        with pytest.raises(ConflictError) as exc:
            approve(service, second)
        assert exc.value.context["shortfall_grams"] == 50.0
        # 失败的整单审批不得留下任何锁定。
        assert service.reservations.lot_balance(lot["id"])["reserved_grams"] == 450.0
        assert service.repository.require_distribution(second["id"])["status"] == "submitted"

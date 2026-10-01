from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection, transaction
from app.germplasm.schemas import (
    AccessionCreate,
    AccessionPatch,
    AccessionTransition,
    AlertDecision,
    CountCreate,
    DistributionCancel,
    DistributionCreate,
    DistributionDecision,
    HoldCreate,
    HoldRelease,
    LocationCreate,
    LotCreate,
    MovePlacement,
    PlacementCreate,
    PolicyCreate,
    ProtocolCreate,
    ReadingCreate,
    ReservationActor,
    ReservationPick,
    ReservationReturn,
    ReservationRollback,
    SourceCreate,
    TestComplete,
    TestCreate,
    TestInvalidate,
    TestStart,
    WithdrawalCreate,
)
from app.germplasm.service import GermplasmService


router = APIRouter(prefix="/api/germplasm", tags=["种质资源"])


def _service() -> GermplasmService:
    return GermplasmService(get_connection())


@router.get("/dashboard")
def dashboard(principal: Principal = Depends(current_principal)) -> dict:
    principal.require("accessions.read")
    return _service().dashboard()


@router.post("/sources", status_code=201)
def create_source(data: SourceCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("accessions.write")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).accessions.create_source(data.model_dump(mode="json"))


@router.put("/sources/{source_id}/restrictions")
def update_source_restrictions(
    source_id: int,
    restrictions: dict[str, Any],
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("accessions.write")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).accessions.update_source_restrictions(source_id, restrictions)


@router.post("/accessions", status_code=201)
def create_accession(data: AccessionCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("accessions.write")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).accessions.create_accession(data.model_dump(mode="json"))


@router.get("/accessions")
def list_accessions(
    status: str | None = None,
    crop: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("accessions.read")
    items, total = _service().repository.list_accessions(status=status, crop=crop, limit=limit, offset=offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/accessions/{accession_id}")
def accession_detail(accession_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("accessions.read")
    return _service().repository.accession_detail(accession_id)


@router.patch("/accessions/{accession_id}")
def update_accession(
    accession_id: int,
    data: AccessionPatch,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("accessions.write")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).accessions.update_accession(
            accession_id, data.model_dump(mode="json", exclude_unset=True)
        )


@router.post("/accessions/{accession_id}/transition")
def transition_accession(
    accession_id: int,
    data: AccessionTransition,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("accessions.write")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).accessions.transition(accession_id, data.model_dump(mode="json"))


@router.get("/accessions/{accession_id}/restrictions")
def accession_restrictions(accession_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("accessions.read")
    return _service().accessions.restrictions_for(accession_id)


@router.post("/locations", status_code=201)
def create_location(data: LocationCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("inventory.write")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).inventory.create_location(data.model_dump(mode="json"))


@router.get("/locations/{location_id}")
def location_detail(location_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("inventory.read")
    return _service().repository.location_detail(location_id)


@router.post("/locations/{location_id}/status/{status}")
def change_location_status(
    location_id: int,
    status: str,
    expected_version: int,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("inventory.write")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).inventory.change_location_status(location_id, status, expected_version)


@router.post("/lots", status_code=201)
def create_lot(data: LotCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("inventory.write")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).inventory.create_lot(data.model_dump(mode="json"))


@router.get("/lots/{lot_id}")
def lot_detail(lot_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("inventory.read")
    service = _service()
    detail = service.repository.lot_detail(lot_id)
    detail["balance"] = service.reservations.lot_balance(lot_id)
    return detail


@router.get("/lots/{lot_id}/reservations")
def lot_reservations(lot_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("inventory.read")
    service = _service()
    lot = service.repository.require_lot(lot_id)
    return {
        "lot": lot,
        "balance": service.reservations.lot_balance(lot_id),
        "allocations": service.reservations.lot_allocations(lot_id),
    }


@router.get("/lots/{lot_id}/reconcile")
def reconcile_lot(lot_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("inventory.read")
    return _service().inventory.reconcile(lot_id)


@router.post("/placements", status_code=201)
def place_lot(data: PlacementCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("inventory.write")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).inventory.place_lot(data.model_dump(mode="json"))


@router.post("/placements/{placement_id}/move")
def move_placement(
    placement_id: int,
    data: MovePlacement,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("inventory.write")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).inventory.move_placement(placement_id, data.model_dump(mode="json"))


@router.post("/withdrawals", status_code=201)
def withdraw(data: WithdrawalCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("inventory.write")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).inventory.withdraw(data.model_dump(mode="json"))


@router.post("/holds", status_code=201)
def impose_hold(data: HoldCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("quality.review")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).inventory.impose_hold(data.model_dump(mode="json"))


@router.post("/holds/{hold_id}/release")
def release_hold(
    hold_id: int,
    data: HoldRelease,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("quality.review")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).inventory.release_hold(hold_id, data.actor, data.reason)


@router.post("/protocols", status_code=201)
def create_protocol(data: ProtocolCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("viability.write")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).viability.create_protocol(data.model_dump(mode="json"))


@router.post("/tests", status_code=201)
def schedule_test(data: TestCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("viability.write")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).viability.schedule_test(data.model_dump(mode="json"))


@router.get("/tests/{test_id}")
def test_detail(test_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("viability.read")
    return _service().repository.test_detail(test_id)


@router.post("/tests/{test_id}/start")
def start_test(test_id: int, data: TestStart, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("viability.write")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).viability.start_test(test_id, data.model_dump(mode="json"))


@router.post("/tests/{test_id}/counts", status_code=201)
def add_count(test_id: int, data: CountCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("viability.write")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).viability.add_count(test_id, data.model_dump(mode="json"))


@router.put("/counts/{count_id}")
def replace_count(count_id: int, data: CountCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("viability.write")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).viability.replace_count(count_id, data.model_dump(mode="json"))


@router.post("/tests/{test_id}/complete")
def complete_test(test_id: int, data: TestComplete, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("viability.write")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).viability.complete_test(test_id, data.model_dump(mode="json"))


@router.post("/tests/{test_id}/invalidate")
def invalidate_test(test_id: int, data: TestInvalidate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("quality.review")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).viability.invalidate_test(test_id, data.model_dump(mode="json"))


@router.post("/policies", status_code=201)
def create_policy(data: PolicyCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("quality.review")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).viability.create_policy(data.model_dump(mode="json"))


@router.get("/retest-schedules/due")
def due_schedules(
    before: date,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(current_principal),
) -> list[dict]:
    principal.require("viability.read")
    return _service().viability.due_schedules(before, limit)


@router.post("/readings", status_code=201)
def add_reading(data: ReadingCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("inventory.write")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).quality.add_reading(data.model_dump(mode="python"))


@router.post("/readings/import")
def import_readings(rows: list[ReadingCreate], principal: Principal = Depends(current_principal)) -> dict:
    principal.require("inventory.write")
    payload = [item.model_dump(mode="python") for item in rows]
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).quality.import_readings(payload)


@router.get("/locations/{location_id}/environment")
def environment_summary(
    location_id: int,
    hours: int = Query(default=24, ge=1, le=24 * 90),
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("inventory.read")
    return _service().quality.location_summary(location_id, hours)


@router.get("/alerts")
def open_alerts(severity: str | None = None, principal: Principal = Depends(current_principal)) -> list[dict]:
    principal.require("quality.review")
    return _service().quality.open_alerts(severity)


@router.post("/alerts/{alert_id}/decision")
def decide_alert(alert_id: int, data: AlertDecision, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("quality.review")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).quality.decide_alert(alert_id, data.model_dump(mode="json"))


@router.post("/distributions", status_code=201)
def create_distribution(data: DistributionCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("accessions.read")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).distribution.create_request(data.model_dump(mode="json"))


@router.post("/distributions/{request_id}/submit")
def submit_distribution(
    request_id: int,
    expected_version: int,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("accessions.read")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).distribution.submit(request_id, expected_version)


@router.post("/distributions/{request_id}/decision")
def decide_distribution(
    request_id: int,
    data: DistributionDecision,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("distribution.approve")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).distribution.decide(request_id, data.model_dump(mode="json"))


@router.get("/distributions/{request_id}")
def distribution_detail(request_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("accessions.read")
    service = _service()
    detail = service.repository.distribution_detail(request_id)
    row = service.reservations.latest_reservation_of_request(request_id)
    if row is not None:
        detail["reservation"] = service.reservations.reservation_detail(int(row["id"]))
    return detail


@router.get("/distributions/{request_id}/reservation")
def distribution_reservation(request_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("accessions.read")
    service = _service()
    row = service.reservations.latest_reservation_of_request(request_id)
    if row is None:
        from app.core.errors import NotFoundError

        raise NotFoundError("该申请没有库存预约记录")
    return service.reservations.reservation_detail(int(row["id"]))


@router.post("/distributions/{request_id}/cancel")
def cancel_distribution(
    request_id: int,
    data: DistributionCancel,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("distribution.manage")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).reservations.cancel(request_id, data.actor, data.reason)


@router.post("/reservations/reclaim-expired")
def reclaim_expired(principal: Principal = Depends(current_principal)) -> dict:
    principal.require("distribution.manage")
    with transaction(immediate=True) as connection:
        reclaimed = GermplasmService(connection).reservations.expire_due()
    return {"reclaimed_count": len(reclaimed), "reservations": reclaimed}


@router.get("/reservations/{reservation_id}")
def reservation_detail(reservation_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("accessions.read")
    return _service().reservations.reservation_detail(reservation_id)


@router.post("/reservations/{reservation_id}/picking")
def start_picking(
    reservation_id: int,
    data: ReservationActor,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("distribution.pick")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).reservations.start_picking(reservation_id, data.actor)


@router.post("/reservations/{reservation_id}/takeover")
def takeover_reservation(
    reservation_id: int,
    data: ReservationActor,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("distribution.manage")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).reservations.takeover(reservation_id, data.actor)


@router.post("/reservations/{reservation_id}/rollback")
def rollback_reservation(
    reservation_id: int,
    data: ReservationRollback,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("distribution.manage")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).reservations.rollback(reservation_id, data.actor, data.reason)


@router.post("/reservation-lines/{line_id}/picks", status_code=201)
def pick_reservation_line(
    line_id: int,
    data: ReservationPick,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("distribution.pick")
    payload = data.model_dump(mode="json")
    payload["_can_manage"] = principal.can("distribution.manage")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).reservations.pick(line_id, payload)


@router.post("/reservation-lines/{line_id}/returns", status_code=201)
def return_reservation_line(
    line_id: int,
    data: ReservationReturn,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("distribution.pick")
    payload = data.model_dump(mode="json")
    payload["_can_manage"] = principal.can("distribution.manage")
    with transaction(immediate=True) as connection:
        return GermplasmService(connection).reservations.return_line(line_id, payload)

from __future__ import annotations

import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, ValidationError
from app.germplasm.repository import GermplasmRepository, record


class InventoryService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = GermplasmRepository(connection)

    def create_location(self, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = to_storage(self.clock.now())
        try:
            cursor = self.connection.execute(
                "INSERT INTO storage_locations(location_code,facility,room,rack,shelf,capacity_grams,temperature_c,"
                "humidity_percent,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    data["location_code"], data["facility"], data["room"], data["rack"], data["shelf"],
                    data["capacity_grams"], data["temperature_c"], data["humidity_percent"], timestamp, timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("库位编码已经存在") from exc
        return self.repository.location_detail(int(cursor.lastrowid))

    def change_location_status(self, location_id: int, status: str, expected_version: int) -> dict[str, Any]:
        if status not in {"active", "maintenance", "closed"}:
            raise ValidationError("库位状态无效")
        before = self.repository.require_location(location_id)
        if int(before["version"]) != expected_version:
            raise ConflictError("库位版本冲突", context={"current_version": before["version"]})
        if status == "closed" and self.repository.location_usage(location_id) > 0:
            raise ConflictError("库位中仍有种子容器，不能关闭")
        timestamp = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE storage_locations SET status=?,version=version+1,updated_at=? WHERE id=? AND version=?",
            (status, timestamp, location_id, expected_version),
        )
        return self.repository.location_detail(location_id)

    def create_lot(self, data: dict[str, Any]) -> dict[str, Any]:
        accession = self.repository.require_accession(int(data["accession_id"]))
        if accession["status"] not in {"accepted", "restricted", "quarantine"}:
            raise ConflictError("资源尚未进入可接收状态，不能建立种子批次")
        parent = None
        if data.get("parent_lot_id"):
            parent = self.repository.require_lot(int(data["parent_lot_id"]))
            if int(parent["accession_id"]) != int(data["accession_id"]):
                raise ValidationError("子批次必须与父批次属于同一资源")
        timestamp = to_storage(self.clock.now())
        try:
            cursor = self.connection.execute(
                "INSERT INTO seed_lots(lot_no,accession_id,parent_lot_id,harvest_year,initial_weight_grams,"
                "available_weight_grams,moisture_percent,treatment,sealed_on,status,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
                (
                    data["lot_no"], data["accession_id"], data.get("parent_lot_id"), data["harvest_year"],
                    data["initial_weight_grams"], data["initial_weight_grams"], data.get("moisture_percent"),
                    data.get("treatment", ""), data.get("sealed_on"), data["created_by"], timestamp, timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("种子批次编号已经存在") from exc
        lot_id = int(cursor.lastrowid)
        if parent:
            self.connection.execute(
                "INSERT INTO lot_movements(lot_id,movement_type,quantity_grams,idempotency_key,actor,reason,created_at) "
                "VALUES(?,'盘点调整',0,?,?,?,?)",
                (lot_id, f"lineage-{lot_id}", data["created_by"], f"由父批次 {parent['lot_no']} 建立", timestamp),
            )
        return self.repository.lot_detail(lot_id)

    def place_lot(self, data: dict[str, Any]) -> dict[str, Any]:
        previous = self.repository.movement_by_key(data["idempotency_key"])
        if previous:
            placement = self.repository.require_placement(int(previous["placement_id"]))
            return {"placement": placement, "replayed": True}
        lot = self.repository.require_lot(int(data["lot_id"]))
        location = self.repository.require_location(int(data["location_id"]))
        if lot["status"] in {"depleted", "disposed"}:
            raise ConflictError("批次已经耗尽或报废")
        if location["status"] != "active":
            raise ConflictError("目标库位当前不可用")
        active_weight = float(self.connection.execute(
            "SELECT COALESCE(SUM(weight_grams),0) FROM lot_placements WHERE lot_id=? AND removed_at IS NULL",
            (lot["id"],),
        ).fetchone()[0])
        if active_weight + float(data["weight_grams"]) > float(lot["available_weight_grams"]) + 1e-9:
            raise ValidationError("摆放重量超过批次可用重量")
        used = self.repository.location_usage(int(location["id"]))
        if used + float(data["weight_grams"]) > float(location["capacity_grams"]) + 1e-9:
            raise ConflictError("目标库位容量不足", context={"available_grams": location["capacity_grams"] - used})
        timestamp = to_storage(self.clock.now())
        try:
            cursor = self.connection.execute(
                "INSERT INTO lot_placements(lot_id,location_id,weight_grams,container_code,placed_at) VALUES(?,?,?,?,?)",
                (lot["id"], location["id"], data["weight_grams"], data["container_code"], timestamp),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("容器编码与入库时间冲突") from exc
        placement_id = int(cursor.lastrowid)
        self.connection.execute(
            "INSERT INTO lot_movements(lot_id,placement_id,movement_type,quantity_grams,to_location_id,idempotency_key,"
            "actor,reason,created_at) VALUES(?,?,'入库',?,?,?,?,?,?)",
            (lot["id"], placement_id, data["weight_grams"], location["id"], data["idempotency_key"], data["actor"], "首次入库", timestamp),
        )
        self.connection.execute(
            "UPDATE seed_lots SET status='stored',version=version+1,updated_at=? WHERE id=?",
            (timestamp, lot["id"]),
        )
        return {"placement": self.repository.require_placement(placement_id), "replayed": False}

    def move_placement(self, placement_id: int, data: dict[str, Any]) -> dict[str, Any]:
        previous = self.repository.movement_by_key(data["idempotency_key"])
        if previous:
            return {"placement": self.repository.require_placement(int(previous["placement_id"])), "replayed": True}
        placement = self.repository.require_placement(placement_id)
        if placement["removed_at"]:
            raise ConflictError("容器已经移出原库位")
        if int(placement["version"]) != int(data["expected_version"]):
            raise ConflictError("容器摆放版本冲突", context={"current_version": placement["version"]})
        target = self.repository.require_location(int(data["target_location_id"]))
        if target["status"] != "active":
            raise ConflictError("目标库位当前不可用")
        used = self.repository.location_usage(int(target["id"]))
        if used + float(placement["weight_grams"]) > float(target["capacity_grams"]) + 1e-9:
            raise ConflictError("目标库位容量不足", context={"available_grams": target["capacity_grams"] - used})
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "INSERT INTO lot_placements(lot_id,location_id,weight_grams,container_code,placed_at) VALUES(?,?,?,?,?)",
            (placement["lot_id"], target["id"], placement["weight_grams"], placement["container_code"], timestamp),
        )
        new_id = int(cursor.lastrowid)
        updated = self.connection.execute(
            "UPDATE lot_placements SET removed_at=?,version=version+1 WHERE id=? AND version=? AND removed_at IS NULL",
            (timestamp, placement_id, data["expected_version"]),
        )
        if updated.rowcount != 1:
            raise ConflictError("容器摆放版本冲突")
        self.connection.execute(
            "INSERT INTO lot_movements(lot_id,placement_id,movement_type,quantity_grams,from_location_id,to_location_id,"
            "idempotency_key,actor,reason,created_at) VALUES(?,?,'移库',?,?,?,?,?,?,?)",
            (
                placement["lot_id"], new_id, placement["weight_grams"], placement["location_id"], target["id"],
                data["idempotency_key"], data["actor"], data["reason"], timestamp,
            ),
        )
        return {"placement": self.repository.require_placement(new_id), "replayed": False}

    def withdraw(self, data: dict[str, Any]) -> dict[str, Any]:
        previous = self.repository.movement_by_key(data["idempotency_key"])
        if previous:
            return {"lot": self.repository.lot_detail(int(previous["lot_id"])), "movement": previous, "replayed": True}
        lot = self.repository.require_lot(int(data["lot_id"]))
        holds = self.repository.active_holds(int(lot["id"]))
        if holds:
            raise ConflictError("批次存在未解除的质量或权限冻结", context={"holds": [item["id"] for item in holds]})
        quantity = float(data["quantity_grams"])
        locked = float(self.connection.execute(
            "SELECT COALESCE(SUM(quantity_grams - picked_grams),0) FROM reservation_lines "
            "WHERE lot_id=? AND status IN ('active','picking')", (lot["id"],)
        ).fetchone()[0])
        if quantity > float(lot["available_weight_grams"]) - locked + 1e-9:
            raise ConflictError(
                "批次可用重量不足（已被发放预约锁定的重量不能领用或报废）",
                context={"on_hand_grams": lot["available_weight_grams"], "reserved_grams": round(locked, 6)},
            )
        timestamp = to_storage(self.clock.now())
        remaining = round(float(lot["available_weight_grams"]) - quantity, 6)
        status = "depleted" if remaining <= 1e-9 else lot["status"]
        self.connection.execute(
            "UPDATE seed_lots SET available_weight_grams=?,status=?,version=version+1,updated_at=? WHERE id=?",
            (remaining, status, timestamp, lot["id"]),
        )
        cursor = self.connection.execute(
            "INSERT INTO lot_movements(lot_id,movement_type,quantity_grams,idempotency_key,actor,reason,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (lot["id"], data["movement_type"], -quantity, data["idempotency_key"], data["actor"], data["reason"], timestamp),
        )
        return {
            "lot": self.repository.lot_detail(int(lot["id"])),
            "movement": record(self.connection.execute("SELECT * FROM lot_movements WHERE id=?", (cursor.lastrowid,)).fetchone()),
            "replayed": False,
        }

    def impose_hold(self, data: dict[str, Any]) -> dict[str, Any]:
        lot = self.repository.require_lot(int(data["lot_id"]))
        existing = self.connection.execute(
            "SELECT * FROM lot_holds WHERE lot_id=? AND hold_type=? AND released_at IS NULL",
            (lot["id"], data["hold_type"]),
        ).fetchone()
        if existing:
            raise ConflictError("该类型冻结已经存在")
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "INSERT INTO lot_holds(lot_id,hold_type,reason,imposed_by,imposed_at) VALUES(?,?,?,?,?)",
            (lot["id"], data["hold_type"], data["reason"], data["actor"], timestamp),
        )
        self.connection.execute(
            "UPDATE seed_lots SET status='held',version=version+1,updated_at=? WHERE id=? AND status NOT IN ('depleted','disposed')",
            (timestamp, lot["id"]),
        )
        # 质量状态恶化：释放该批次上尚未进入拣货的预约锁定，并尝试用其他批次补足。
        from app.germplasm.reservations import ReservationService

        ReservationService(self.connection, self.clock).release_lot_for_quality(
            int(lot["id"]), f"{data['hold_type']}冻结：{data['reason']}"
        )
        return record(self.connection.execute("SELECT * FROM lot_holds WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def release_hold(self, hold_id: int, actor: str, reason: str) -> dict[str, Any]:
        hold = record(self.connection.execute("SELECT * FROM lot_holds WHERE id=?", (hold_id,)).fetchone())
        if not hold:
            raise ValidationError("冻结记录不存在")
        if hold["released_at"]:
            raise ConflictError("冻结记录已经解除")
        timestamp = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE lot_holds SET released_by=?,released_at=?,release_reason=? WHERE id=? AND released_at IS NULL",
            (actor, timestamp, reason, hold_id),
        )
        remaining = self.repository.active_holds(int(hold["lot_id"]))
        if not remaining:
            self.connection.execute(
                "UPDATE seed_lots SET status=CASE WHEN available_weight_grams<=0 THEN 'depleted' ELSE 'stored' END,"
                "version=version+1,updated_at=? WHERE id=? AND status='held'",
                (timestamp, hold["lot_id"]),
            )
        return record(self.connection.execute("SELECT * FROM lot_holds WHERE id=?", (hold_id,)).fetchone()) or {}

    def reconcile(self, lot_id: int) -> dict[str, Any]:
        lot = self.repository.require_lot(lot_id)
        movement_total = float(self.connection.execute(
            "SELECT COALESCE(SUM(quantity_grams),0) FROM lot_movements WHERE lot_id=? AND movement_type IN ('取样','领用','报废','归还','盘点调整')",
            (lot_id,),
        ).fetchone()[0])
        expected_available = round(float(lot["initial_weight_grams"]) + movement_total, 6)
        placed_weight = float(self.connection.execute(
            "SELECT COALESCE(SUM(weight_grams),0) FROM lot_placements WHERE lot_id=? AND removed_at IS NULL", (lot_id,)
        ).fetchone()[0])
        return {
            "lot_id": lot_id,
            "recorded_available_grams": lot["available_weight_grams"],
            "expected_available_grams": expected_available,
            "active_placement_grams": placed_weight,
            "available_matches_ledger": abs(float(lot["available_weight_grams"]) - expected_available) < 1e-6,
            "placements_within_available": placed_weight <= float(lot["available_weight_grams"]) + 1e-6,
        }

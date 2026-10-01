from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, ValidationError
from app.germplasm.repository import GermplasmRepository, record, records

# 选批规则（写入 rule_snapshot.rules，出库人员可据此证明分配顺序）
RULE_RESOURCE = "resource:资源须为正式接收且来源/护照未禁止发放"
RULE_HOLD = "hold:存在未解除冻结或批次不在库状态的批次不参与分配"
RULE_QUALITY = "quality:存在未关闭严重质量告警的批次不参与分配"
RULE_VIABILITY = "viability:优先采用最近一次已完成且未作废的活力结果，无有效检测的批次排在最后"
RULE_FEFO = "fefo:先到期先用，按封存日期、收获年份、批次编号升序消耗"
SELECTION_RULES = [RULE_RESOURCE, RULE_HOLD, RULE_QUALITY, RULE_VIABILITY, RULE_FEFO]

ACTIVE_RESERVATION_STATES = ("active", "picking")
TERMINAL_REQUEST_STATES = {"rejected", "fulfilled", "cancelled", "expired"}


class ReservationService:
    """有期限的库存预约：批准即锁重量，到期/取消/质量恶化释放，拣货后只能接管或回退。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = GermplasmRepository(connection)

    # ------------------------------------------------------------------ 审批分配

    def allocate_request(self, request_id: int, items: list[dict[str, Any]], reservation_hours: int, actor: str) -> None:
        """在批准事务内为每条申请明细选择一个或多个批次并锁定重量。

        调用方已经持有 IMMEDIATE 写事务；SQLite 的写锁保证并发审批串行化，
        加上 trg_reservation_no_oversell 触发器，任何重放或并发都不会重复占用。
        """
        timestamp = to_storage(self.clock.now())
        expires_at = to_storage(self.clock.now() + timedelta(hours=reservation_hours))
        plans: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for item in items:
            candidates = self._candidates(int(item["accession_id"]))
            plan = self._build_plan(candidates, float(item["quantity_grams"]))
            if plan is None:
                raise ConflictError(
                    "没有满足重量、质量冻结与活力条件的可发放批次",
                    context={"accession_id": item["accession_id"], "requested_grams": item["quantity_grams"]},
                )
            plans.append((item, plan))
        for item, plan in plans:
            self._persist_plan(request_id, int(item["id"]), plan, expires_at, actor, timestamp)

    def _candidates(self, accession_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT l.id AS lot_id, l.lot_no, l.status, l.available_weight_grams,
                   l.harvest_year, l.sealed_on,
                   v.id AS viability_test_id, v.germination_percent, v.completed_at AS viability_completed_at,
                   l.available_weight_grams - COALESCE((
                       SELECT SUM(r.quantity_grams) FROM lot_reservations r
                       WHERE r.lot_id=l.id AND r.status IN ('active','picking')
                   ), 0) AS reservable_grams
            FROM seed_lots l
            JOIN accessions a ON a.id=l.accession_id AND a.status='accepted'
            LEFT JOIN collection_sources s ON s.id=a.source_id
            LEFT JOIN viability_tests v ON v.id=(
                SELECT id FROM viability_tests
                WHERE lot_id=l.id AND status='completed'
                ORDER BY completed_at DESC,id DESC LIMIT 1
            )
            WHERE l.accession_id=? AND l.status='stored'
              AND COALESCE(json_extract(s.restrictions_json, '$.no_distribution'), 0)=0
              AND COALESCE(json_extract(a.passport_json, '$.restrictions.no_distribution'), 0)=0
              AND NOT EXISTS (SELECT 1 FROM lot_holds h WHERE h.lot_id=l.id AND h.released_at IS NULL)
              AND NOT EXISTS (
                  SELECT 1 FROM quality_alerts q
                  WHERE q.lot_id=l.id AND q.severity='critical' AND q.status IN ('open','acknowledged')
              )
            ORDER BY CASE WHEN v.id IS NULL THEN 1 ELSE 0 END,
                     CASE WHEN l.sealed_on IS NULL THEN 1 ELSE 0 END, l.sealed_on,
                     l.harvest_year, l.lot_no, l.id
            """,
            (accession_id,),
        ).fetchall()
        return [dict(row) for row in rows if float(row["reservable_grams"]) > 1e-9]

    def _build_plan(self, candidates: list[dict[str, Any]], quantity: float) -> list[dict[str, Any]] | None:
        remaining = round(quantity, 6)
        plan: list[dict[str, Any]] = []
        for candidate in candidates:
            reservable = round(float(candidate["reservable_grams"]), 6)
            if reservable <= 0:
                continue
            take = round(min(remaining, reservable), 6)
            if take <= 0:
                break
            plan.append({"candidate": candidate, "quantity_grams": take})
            remaining = round(remaining - take, 6)
            if remaining <= 1e-9:
                break
        if remaining > 1e-9:
            return None
        for rank, entry in enumerate(plan, start=1):
            entry["allocation_rank"] = rank
        return plan

    def _persist_plan(
        self,
        request_id: int,
        item_id: int,
        plan: list[dict[str, Any]],
        expires_at: str,
        actor: str,
        timestamp: str,
    ) -> None:
        for entry in plan:
            candidate = entry["candidate"]
            rank = entry["allocation_rank"]
            snapshot = {
                "rules": SELECTION_RULES,
                "candidate_order": rank,
                "lot_no": candidate["lot_no"],
                "sealed_on": candidate["sealed_on"],
                "harvest_year": candidate["harvest_year"],
                "viability_test_id": candidate["viability_test_id"],
                "viability_completed_at": candidate["viability_completed_at"],
                "germination_percent": candidate["germination_percent"],
                "reservable_grams_at_decision": round(float(candidate["reservable_grams"]), 6),
            }
            try:
                cursor = self.connection.execute(
                    "INSERT INTO lot_reservations(reservation_no,request_id,item_id,lot_id,quantity_grams,"
                    "allocation_rank,rule_snapshot_json,status,expires_at,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,'active',?,?,?)",
                    (
                        f"RSV-{item_id}-{rank}", request_id, item_id, candidate["lot_id"], entry["quantity_grams"],
                        rank, json.dumps(snapshot, ensure_ascii=False, sort_keys=True), expires_at, actor, timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("预约重量超过批次可预约余额，库存可能已被并发审批占用") from exc
            reservation_id = int(cursor.lastrowid)
            self._event(reservation_id, "created", actor, "批准时按规则锁定库存", snapshot, timestamp)
        first_lot = plan[0]["candidate"]["lot_id"]
        self.connection.execute(
            "UPDATE distribution_items SET allocated_lot_id=?,status='allocated' WHERE id=?",
            (first_lot, item_id),
        )

    # ------------------------------------------------------------------ 释放

    def sweep_expired(self) -> list[dict[str, Any]]:
        """回收所有到期预约。服务重启、审批和拣货前都会调用，天然幂等。"""
        now = to_storage(self.clock.now())
        expired = records(self.connection.execute(
            "SELECT * FROM lot_reservations WHERE status='active' AND expires_at<=? ORDER BY id",
            (now,),
        ).fetchall())
        results: list[dict[str, Any]] = []
        for reservation in expired:
            results.append(self._release(reservation, "system", "预约到期自动释放", timestamp=now))
        return results

    def release_for_deterioration(self, lot_id: int, actor: str, reason: str) -> list[dict[str, Any]]:
        """批次质量恶化（如新冻结）时释放其尚未拣货的预约；已拣货的只能人工回退。"""
        timestamp = to_storage(self.clock.now())
        active = records(self.connection.execute(
            "SELECT * FROM lot_reservations WHERE lot_id=? AND status='active' ORDER BY id",
            (lot_id,),
        ).fetchall())
        return [
            self._release(reservation, actor, f"质量状态恶化：{reason}", timestamp=timestamp)
            for reservation in active
        ]

    def cancel_request(self, request_id: int, actor: str, reason: str) -> dict[str, Any]:
        request = self.repository.require_distribution(request_id)
        if request["status"] == "cancelled":
            return self.repository.distribution_detail(request_id)
        if request["status"] in {"rejected", "fulfilled", "expired"}:
            raise ConflictError("当前申请状态不能取消", context={"status": request["status"]})
        picking = self.connection.execute(
            "SELECT COUNT(*) FROM lot_reservations WHERE request_id=? AND status='picking'", (request_id,)
        ).fetchone()[0]
        if int(picking):
            raise ConflictError("已有预约进入拣货，不能整单取消，请由有权限人员回退或接管后处理")
        timestamp = to_storage(self.clock.now())
        active = records(self.connection.execute(
            "SELECT * FROM lot_reservations WHERE request_id=? AND status='active' ORDER BY id", (request_id,)
        ).fetchall())
        for reservation in active:
            self._release(reservation, actor, f"申请取消：{reason}", timestamp=timestamp)
        self.connection.execute(
            "UPDATE distribution_requests SET status='cancelled',version=version+1 WHERE id=?", (request_id,)
        )
        self.connection.execute(
            "UPDATE distribution_items SET status='unavailable' WHERE request_id=? AND status IN ('requested','allocated')",
            (request_id,),
        )
        return self.repository.distribution_detail(request_id)

    def _release(self, reservation: dict[str, Any], actor: str, reason: str, *, timestamp: str | None = None) -> dict[str, Any]:
        """把预约余额还给批次。拣货中的预约拒绝自动释放，必须走回退流程。"""
        current = self.repository.require_reservation(int(reservation["id"]))
        if current["status"] == "released":
            return current
        if current["status"] == "picking":
            raise ConflictError("预约已进入拣货，只能由有权限人员接管或回退", context={"reservation_id": current["id"]})
        if current["status"] == "fulfilled":
            raise ConflictError("预约已出库，不能释放", context={"reservation_id": current["id"]})
        timestamp = timestamp or to_storage(self.clock.now())
        cursor = self.connection.execute(
            "UPDATE lot_reservations SET status='released',released_by=?,released_at=?,release_reason=?,"
            "version=version+1 WHERE id=? AND status='active'",
            (actor, timestamp, reason, current["id"]),
        )
        if cursor.rowcount != 1:
            return self.repository.require_reservation(int(current["id"]))
        self._event(int(current["id"]), "released", actor, reason, {"expires_at": current["expires_at"]}, timestamp)
        self._sync_request_after_release(int(current["request_id"]), timestamp)
        return self.repository.require_reservation(int(current["id"]))

    def _sync_request_after_release(self, request_id: int, timestamp: str) -> None:
        request = self.repository.require_distribution(request_id)
        if request["status"] not in {"approved", "picking", "expired"}:
            return
        for item in records(self.connection.execute(
            "SELECT * FROM distribution_items WHERE request_id=?", (request_id,)
        ).fetchall()):
            if item["status"] in {"fulfilled", "unavailable"}:
                continue
            covered = float(self.connection.execute(
                "SELECT COALESCE(SUM(quantity_grams),0) FROM lot_reservations "
                "WHERE item_id=? AND status IN ('active','picking','fulfilled')",
                (item["id"],),
            ).fetchone()[0])
            if covered + 1e-9 < float(item["quantity_grams"]):
                # 部分批次被释放后出现缺口：标记为不可完整出库，等待取消或人工处置
                self.connection.execute(
                    "UPDATE distribution_items SET status='unavailable' WHERE id=? AND status!='fulfilled'",
                    (item["id"],),
                )
        outstanding = int(self.connection.execute(
            "SELECT COUNT(*) FROM lot_reservations WHERE request_id=? AND status IN ('active','picking')",
            (request_id,),
        ).fetchone()[0])
        if outstanding == 0:
            # 再无有效预约：全部出库则已在出库流程置 fulfilled，否则按到期收口
            unfulfilled_items = int(self.connection.execute(
                "SELECT COUNT(*) FROM distribution_items WHERE request_id=? AND status!='fulfilled'",
                (request_id,),
            ).fetchone()[0])
            if unfulfilled_items > 0:
                self.connection.execute(
                    "UPDATE distribution_requests SET status='expired',version=version+1 "
                    "WHERE id=? AND status!='cancelled'",
                    (request_id,),
                )
                self.connection.execute(
                    "UPDATE distribution_items SET status='unavailable' WHERE request_id=? "
                    "AND status IN ('requested','allocated','released','picking')",
                    (request_id,),
                )
        del timestamp

    # ------------------------------------------------------------------ 拣货/接管/回退

    def start_picking(self, reservation_id: int, actor: str) -> dict[str, Any]:
        self.sweep_expired()
        reservation = self.repository.require_reservation(reservation_id)
        now = to_storage(self.clock.now())
        if reservation["status"] == "released":
            raise ConflictError("预约已释放，不能拣货", context={"reason": reservation["release_reason"]})
        if reservation["status"] != "active":
            raise ConflictError("只有生效中的预约可以开始拣货", context={"status": reservation["status"]})
        if reservation["expires_at"] <= now:
            raise ConflictError("预约已到期，不能拣货")
        self._guard_lot_available(reservation)
        cursor = self.connection.execute(
            "UPDATE lot_reservations SET status='picking',picked_by=?,picked_at=?,version=version+1 "
            "WHERE id=? AND status='active' AND expires_at>?",
            (actor, now, reservation_id, now),
        )
        if cursor.rowcount != 1:
            raise ConflictError("预约状态已变化，拣货失败")
        self._event(reservation_id, "picking", actor, "开始拣货", {}, now)
        self.connection.execute(
            "UPDATE distribution_requests SET status='picking',version=version+1 "
            "WHERE id=? AND status='approved'", (reservation["request_id"],)
        )
        self.connection.execute(
            "UPDATE distribution_items SET status='picking' WHERE id=? AND status='allocated'",
            (reservation["item_id"],),
        )
        return self.repository.require_reservation(reservation_id)

    def takeover(self, reservation_id: int, actor: str, reason: str) -> dict[str, Any]:
        reservation = self.repository.require_reservation(reservation_id)
        if reservation["status"] != "picking":
            raise ConflictError("只有拣货中的预约可以接管", context={"status": reservation["status"]})
        if not reason.strip():
            raise ValidationError("接管时必须填写原因")
        timestamp = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE lot_reservations SET picked_by=?,version=version+1 WHERE id=? AND status='picking'",
            (actor, reservation_id),
        )
        self._event(reservation_id, "takeover", actor, reason, {"previous_picked_by": reservation["picked_by"]}, timestamp)
        return self.repository.require_reservation(reservation_id)

    def rollback(self, reservation_id: int, actor: str, reason: str) -> dict[str, Any]:
        reservation = self.repository.require_reservation(reservation_id)
        if reservation["status"] != "picking":
            raise ConflictError("只有拣货中的预约可以回退", context={"status": reservation["status"]})
        if not reason.strip():
            raise ValidationError("回退时必须填写原因")
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "UPDATE lot_reservations SET status='active',picked_by=NULL,version=version+1 "
            "WHERE id=? AND status='picking'",
            (reservation_id,),
        )
        if cursor.rowcount != 1:
            raise ConflictError("预约状态已变化，回退失败")
        self._event(reservation_id, "rollback", actor, reason, {"previous_picked_by": reservation["picked_by"]}, timestamp)
        try:
            self._guard_lot_available(self.repository.require_reservation(reservation_id))
        except ConflictError as exc:
            # 拣货期间批次已冻结或出现严重告警：回退后立即释放余额并记录原因
            detail = ";".join(str(v) for v in (exc.context or {}).values()) or exc.message
            return self._release(
                self.repository.require_reservation(reservation_id), actor,
                f"回退时批次已不可发放：{detail}", timestamp=timestamp,
            )
        remaining_picking = int(self.connection.execute(
            "SELECT COUNT(*) FROM lot_reservations WHERE request_id=? AND status='picking'",
            (reservation["request_id"],),
        ).fetchone()[0])
        if remaining_picking == 0:
            self.connection.execute(
                "UPDATE distribution_requests SET status='approved',version=version+1 WHERE id=? AND status='picking'",
                (reservation["request_id"],),
            )
            self.connection.execute(
                "UPDATE distribution_items SET status='allocated' WHERE id=? AND status='picking'",
                (reservation["item_id"],),
            )
        return self.repository.require_reservation(reservation_id)

    # ------------------------------------------------------------------ 出库

    def ship(self, reservation_id: int, data: dict[str, Any]) -> dict[str, Any]:
        reservation = self.repository.require_reservation(reservation_id)
        if reservation["status"] == "fulfilled":
            return self._shipment_response(int(reservation["id"]), replayed=True)
        if reservation["status"] != "picking":
            raise ConflictError("只有拣货中的预约可以出库", context={"status": reservation["status"]})
        existing = self.connection.execute(
            "SELECT * FROM outbound_records WHERE reservation_id=?", (reservation_id,)
        ).fetchone()
        if existing:
            return self._shipment_response(int(existing["id"]), replayed=True)
        self._guard_lot_available(reservation)
        quantity = round(float(reservation["quantity_grams"]), 6)
        timestamp = to_storage(self.clock.now())
        deduct = self.connection.execute(
            "UPDATE seed_lots SET available_weight_grams=ROUND(available_weight_grams-?,6),"
            "status=CASE WHEN ROUND(available_weight_grams-?,6)<=0 THEN 'depleted' ELSE status END,"
            "version=version+1,updated_at=? WHERE id=? AND available_weight_grams>=?",
            (quantity, quantity, timestamp, reservation["lot_id"], quantity),
        )
        if deduct.rowcount != 1:
            raise ConflictError("批次实际库存不足，预约无法出库")
        movement_cursor = self.connection.execute(
            "INSERT INTO lot_movements(lot_id,movement_type,quantity_grams,idempotency_key,actor,reason,created_at) "
            "VALUES(?, '出库发放', ?, ?, ?, ?, ?)",
            (
                reservation["lot_id"], -quantity, f"reservation-ship-{reservation_id}", data["actor"],
                f"发放预约 {reservation['reservation_no']} 出库", timestamp,
            ),
        )
        try:
            outbound_cursor = self.connection.execute(
                "INSERT INTO outbound_records(outbound_no,reservation_id,request_id,item_id,lot_id,movement_id,"
                "quantity_grams,recipient,note,shipped_by,shipped_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    data["outbound_no"], reservation_id, reservation["request_id"], reservation["item_id"],
                    reservation["lot_id"], movement_cursor.lastrowid, quantity, data["recipient"],
                    data.get("note", ""), data["actor"], timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("出库单号已经存在") from exc
        self.connection.execute(
            "UPDATE lot_reservations SET status='fulfilled',fulfilled_by=?,fulfilled_at=?,version=version+1 "
            "WHERE id=? AND status='picking'",
            (data["actor"], timestamp, reservation_id),
        )
        self._event(reservation_id, "fulfilled", data["actor"], f"出库单 {data['outbound_no']}", {
            "outbound_no": data["outbound_no"], "quantity_grams": quantity,
        }, timestamp)
        self._finalize_item_and_request(reservation, timestamp)
        return self._shipment_response(int(outbound_cursor.lastrowid), replayed=False)

    def _finalize_item_and_request(self, reservation: dict[str, Any], timestamp: str) -> None:
        item = self.repository.require_distribution_item(int(reservation["item_id"]))
        requested = round(float(item["quantity_grams"]), 6)
        fulfilled = round(float(self.connection.execute(
            "SELECT COALESCE(SUM(quantity_grams),0) FROM lot_reservations WHERE item_id=? AND status='fulfilled'",
            (item["id"],),
        ).fetchone()[0]), 6)
        if requested - fulfilled <= 1e-9:
            self.connection.execute(
                "UPDATE distribution_items SET status='fulfilled' WHERE id=?", (item["id"],)
            )
        else:
            self.connection.execute(
                "UPDATE distribution_items SET status='allocated' WHERE id=? AND status='picking'", (item["id"],)
            )
        pending_items = int(self.connection.execute(
            "SELECT COUNT(*) FROM distribution_items WHERE request_id=? AND status!='fulfilled'",
            (reservation["request_id"],),
        ).fetchone()[0])
        if pending_items == 0:
            self.connection.execute(
                "UPDATE distribution_requests SET status='fulfilled',version=version+1 WHERE id=?",
                (reservation["request_id"],),
            )
        else:
            still_picking = int(self.connection.execute(
                "SELECT COUNT(*) FROM lot_reservations WHERE request_id=? AND status='picking'",
                (reservation["request_id"],),
            ).fetchone()[0])
            if still_picking == 0:
                self.connection.execute(
                    "UPDATE distribution_requests SET status='approved',version=version+1 WHERE id=? AND status='picking'",
                    (reservation["request_id"],),
                )
        del timestamp

    def _shipment_response(self, outbound_or_reservation_id: int, *, replayed: bool) -> dict[str, Any]:
        row = record(self.connection.execute(
            "SELECT * FROM outbound_records WHERE id=? OR reservation_id=? ORDER BY id LIMIT 1",
            (outbound_or_reservation_id, outbound_or_reservation_id),
        ).fetchone())
        return {"outbound": row, "replayed": replayed}

    def _guard_lot_available(self, reservation: dict[str, Any]) -> None:
        lot = self.repository.require_lot(int(reservation["lot_id"]))
        holds = self.repository.active_holds(int(lot["id"]))
        if holds:
            raise ConflictError("批次存在未解除的质量或权限冻结，请先回退该预约", context={"holds": [h["id"] for h in holds]})
        open_critical = self.connection.execute(
            "SELECT COUNT(*) FROM quality_alerts WHERE lot_id=? AND severity='critical' AND status IN ('open','acknowledged')",
            (lot["id"],),
        ).fetchone()[0]
        if int(open_critical):
            raise ConflictError("批次存在未关闭的严重质量告警，请先回退该预约")

    def _event(self, reservation_id: int, event_type: str, actor: str, reason: str, detail: dict[str, Any], timestamp: str) -> None:
        self.connection.execute(
            "INSERT INTO reservation_events(reservation_id,event_type,actor,reason,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (reservation_id, event_type, actor, reason, json.dumps(detail, ensure_ascii=False, sort_keys=True), timestamp),
        )

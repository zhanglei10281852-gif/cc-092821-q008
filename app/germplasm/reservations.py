from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.germplasm.repository import GermplasmRepository, record, records

RULE_VERSION = "v1:resource_restriction>quality_freeze>latest_valid_viability>fefo"
WEIGHT_EPS = 1e-6


def _loads(raw: str | None) -> dict[str, Any]:
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}

# 候选批次排序：可发放资源 -> 无质量冻结 -> 有最近有效活力结果优先（结果越新越优先）
# -> 先到期先用（密封日期/收获年份越早越优先）-> 批次编号兜底，保证顺序确定。
CANDIDATE_SQL = """
SELECT l.*,
       (SELECT id FROM viability_tests WHERE lot_id=l.id AND status='completed'
        ORDER BY completed_at DESC,id DESC LIMIT 1) AS viability_test_id,
       (SELECT germination_percent FROM viability_tests WHERE lot_id=l.id AND status='completed'
        ORDER BY completed_at DESC,id DESC LIMIT 1) AS germination_percent,
       (SELECT completed_at FROM viability_tests WHERE lot_id=l.id AND status='completed'
        ORDER BY completed_at DESC,id DESC LIMIT 1) AS viability_completed_at,
       COALESCE((
           SELECT SUM(quantity_grams - picked_grams) FROM reservation_lines
           WHERE lot_id=l.id AND status IN ('active','picking')
       ),0) AS locked_grams
FROM seed_lots l
WHERE l.accession_id=? AND l.status='stored'
  AND l.available_weight_grams - COALESCE((
      SELECT SUM(quantity_grams - picked_grams) FROM reservation_lines
      WHERE lot_id=l.id AND status IN ('active','picking')
  ),0) > ?
  AND NOT EXISTS (
      SELECT 1 FROM lot_holds h WHERE h.lot_id=l.id AND h.released_at IS NULL
  )
ORDER BY CASE WHEN (
      SELECT id FROM viability_tests WHERE lot_id=l.id AND status='completed'
      ORDER BY completed_at DESC,id DESC LIMIT 1) IS NULL THEN 1 ELSE 0 END,
      (SELECT completed_at FROM viability_tests WHERE lot_id=l.id AND status='completed'
      ORDER BY completed_at DESC,id DESC LIMIT 1) DESC,
      COALESCE(l.sealed_on, l.harvest_year || '-12-31') ASC,
      l.harvest_year ASC,
      l.lot_no ASC
"""


class ReservationService:
    """有期限的库存预约：审批事务内选批并锁定重量，支持到期回收、质量释放、拣货与回退。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = GermplasmRepository(connection)

    # ------------------------------------------------------------------ 审批锁定

    def reserve_for_decision(self, request_id: int, data: dict[str, Any]) -> dict[str, Any]:
        """在批准事务中为每个申请明细选择一个或多个批次并锁定重量。

        调用方必须持有 IMMEDIATE 事务：BEGIN IMMEDIATE 将并发审批串行化，
        加上 reservation_lines 上的超卖触发器，形成双重防超卖。
        """
        request = self.repository.require_distribution(request_id)
        existing = self._open_reservation_row(request_id)
        if existing is not None:
            # 审批重放：预约已经存在，直接返回既有分配，绝不重复占用。
            return self.reservation_detail(int(existing["id"]))
        if request["status"] != "submitted":
            raise ConflictError("只有已提交申请可以审批")
        if int(request["version"]) != int(data["expected_version"]):
            raise ConflictError("发放申请版本冲突", context={"current_version": request["version"]})

        detail = self.repository.distribution_detail(request_id)
        ttl_days = int(data.get("reservation_days") or 7)
        if not 1 <= ttl_days <= 90:
            raise ValidationError("预约有效期必须在 1 到 90 天之间")
        now = self.clock.now()
        timestamp = to_storage(now)
        expires_at = to_storage(now + timedelta(days=ttl_days))

        # 先计算全部明细的分配，全部满足后再写库，任一批次不足则整单失败回滚。
        planned: list[dict[str, Any]] = []
        for item in detail["items"]:
            plan = self._plan_item(int(item["id"]), int(item["accession_id"]), float(item["quantity_grams"]))
            planned.extend(plan)

        cursor = self.connection.execute(
            "INSERT INTO reservations(request_id,status,expires_at,allocation_rule,approved_by,created_at,updated_at) "
            "VALUES(?, 'active', ?, ?, ?, ?, ?)",
            (request_id, expires_at, RULE_VERSION, data["actor"], timestamp, timestamp),
        )
        reservation_id = int(cursor.lastrowid)
        for seq, line in enumerate(planned, start=1):
            line["rule"]["rank"] = seq
            self.connection.execute(
                "INSERT INTO reservation_lines(reservation_id,request_item_id,lot_id,sequence_no,quantity_grams,"
                "status,rule_json,created_at) VALUES(?,?,?,?,?,'active',?,?)",
                (
                    reservation_id, line["request_item_id"], line["lot_id"], seq,
                    round(line["quantity_grams"], 6),
                    json.dumps(line["rule"], ensure_ascii=False, sort_keys=True), timestamp,
                ),
            )
        self.connection.execute(
            "UPDATE distribution_items SET status='allocated' WHERE request_id=?", (request_id,)
        )
        self.connection.execute(
            "UPDATE distribution_requests SET status='approved',reviewed_by=?,reviewed_at=?,decision_reason=?,"
            "version=version+1 WHERE id=?",
            (data["actor"], timestamp, data.get("reason", ""), request_id),
        )
        self._event(reservation_id, "created", data["actor"], {
            "ttl_days": ttl_days,
            "expires_at": expires_at,
            "lines": [
                {"sequence_no": line["rule"]["rank"], "lot_id": line["lot_id"],
                 "quantity_grams": round(line["quantity_grams"], 6)}
                for line in planned
            ],
        })
        return self.reservation_detail(reservation_id)

    def _plan_item(self, item_id: int, accession_id: int, quantity: float) -> list[dict[str, Any]]:
        access = self.accessions_gate(accession_id)
        if not access["distribution_allowed"]:
            raise ConflictError("资源存在发放限制，不能预约", context={"accession_id": accession_id})
        remaining = round(quantity, 6)
        plan: list[dict[str, Any]] = []
        rows = self.connection.execute(CANDIDATE_SQL, (accession_id, WEIGHT_EPS)).fetchall()
        for row in rows:
            if remaining <= WEIGHT_EPS:
                break
            free = round(float(row["available_weight_grams"]) - float(row["locked_grams"]), 6)
            if free <= WEIGHT_EPS:
                continue
            take = round(min(remaining, free), 6)
            # 同一申请内资源不重复，故同批次在本单只可能被本行使用；
            # locked_grams 为其他申请的有效锁定，选择时直接扣除。
            plan.append({
                "request_item_id": item_id,
                "lot_id": int(row["id"]),
                "quantity_grams": take,
                "rule": {
                    "lot_no": row["lot_no"],
                    "selected_by": [
                        "resource_restriction",
                        "no_quality_freeze",
                        "lot_stored",
                        "latest_valid_viability",
                        "fefo",
                    ],
                    "viability": None if row["viability_test_id"] is None else {
                        "test_id": int(row["viability_test_id"]),
                        "germination_percent": row["germination_percent"],
                        "completed_at": row["viability_completed_at"],
                    },
                    "sealed_on": row["sealed_on"],
                    "harvest_year": int(row["harvest_year"]),
                    "on_hand_grams": float(row["available_weight_grams"]),
                    "other_reserved_grams": float(row["locked_grams"]),
                    "free_grams_before_lock": free,
                },
            })
            remaining = round(remaining - take, 6)
        if remaining > WEIGHT_EPS:
            raise ConflictError(
                "没有满足重量、质量冻结与活力条件的可发放批次",
                context={"accession_id": accession_id, "shortfall_grams": remaining},
            )
        return plan

    def accessions_gate(self, accession_id: int) -> dict[str, Any]:
        """资源限制检查：资源须为正式接收，来源与护照均无 no_distribution 限制。"""
        accession = self.repository.require_accession(accession_id)
        source_rules: dict[str, Any] = {}
        if accession.get("source_id"):
            source_rules = self.repository.require_source(int(accession["source_id"])).get("restrictions", {})
        passport_rules = accession.get("passport", {}).get("restrictions", {})
        blocked = bool(source_rules.get("no_distribution") or passport_rules.get("no_distribution"))
        return {
            "accession_id": accession_id,
            "status": accession["status"],
            "distribution_allowed": accession["status"] == "accepted" and not blocked,
        }

    # ------------------------------------------------------------------ 释放

    def cancel(self, request_id: int, actor: str, reason: str) -> dict[str, Any]:
        request = self.repository.require_distribution(request_id)
        row = self._open_reservation_row(request_id)
        if row is None:
            raise ConflictError("该申请没有生效中的预约")
        if row["status"] == "picking":
            raise ConflictError("预约已经进入拣货，只能由有权限的人接管或整单回退")
        reservation_id = int(row["id"])
        timestamp = to_storage(self.clock.now())
        self._release_reservation(reservation_id, "cancelled", actor, timestamp, reason or "申请取消")
        return self.reservation_detail(reservation_id)

    def expire_due(self) -> list[dict[str, Any]]:
        """回收所有已到期但尚未进入拣货的预约，供服务重启与定时任务调用。"""
        now = to_storage(self.clock.now())
        rows = self.connection.execute(
            "SELECT * FROM reservations WHERE status='active' AND expires_at<=? ORDER BY id", (now,)
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            reservation_id = int(row["id"])
            self._release_reservation(reservation_id, "expired", "system", now, f"预约于 {row['expires_at']} 到期")
            results.append(self.reservation_detail(reservation_id))
        return results

    def release_lot_for_quality(self, lot_id: int, reason_detail: str) -> dict[str, Any]:
        """批次被质量冻结时，释放其上尚未进入拣货的锁定；拣货中的锁定只能人工接管/回退。

        释放后尝试用其他合格批次补足同一申请明细的缺口，补不齐则把明细标记为 unavailable。
        """
        now = to_storage(self.clock.now())
        line_rows = self.connection.execute(
            "SELECT * FROM reservation_lines WHERE lot_id=? AND status='active' ORDER BY reservation_id,sequence_no",
            (lot_id,),
        ).fetchall()
        affected_items: dict[int, set[int]] = {}
        for line in line_rows:
            reservation_id = int(line["reservation_id"])
            affected_items.setdefault(reservation_id, set()).add(int(line["request_item_id"]))
            self._release_line(int(line["id"]), "quality_hold", "system", now, reason_detail)
        for reservation_id, item_ids in affected_items.items():
            reservation = self.require_reservation(reservation_id)
            if reservation["status"] != "active":
                continue
            self._replenish(reservation_id, item_ids, now)
            if self._open_line_count(reservation_id) == 0:
                self._release_reservation(reservation_id, "quality_hold", "system", now, reason_detail)
            else:
                self._event(reservation_id, "partially_released", "system",
                            {"lot_id": lot_id, "reason": reason_detail, "item_ids": sorted(item_ids)})
        return {"lot_id": lot_id, "reservations": [detail for detail in (
            self.reservation_detail(rid) for rid in sorted(affected_items)
        )]}

    def release_accession_for_restriction(self, accession_id: int, reason_detail: str) -> list[int]:
        """资源转为限制/退出保存时，释放其下全部未拣货锁定并尝试补足。"""
        now = to_storage(self.clock.now())
        line_rows = self.connection.execute(
            "SELECT ln.* FROM reservation_lines ln JOIN seed_lots l ON l.id=ln.lot_id "
            "WHERE l.accession_id=? AND ln.status='active' ORDER BY ln.reservation_id,ln.sequence_no",
            (accession_id,),
        ).fetchall()
        affected: dict[int, set[int]] = {}
        for line in line_rows:
            reservation_id = int(line["reservation_id"])
            affected.setdefault(reservation_id, set()).add(int(line["request_item_id"]))
            self._release_line(int(line["id"]), "restriction", "system", now, reason_detail)
        for reservation_id, item_ids in affected.items():
            reservation = self.require_reservation(reservation_id)
            if reservation["status"] != "active":
                continue
            self._replenish(reservation_id, item_ids, now)
            if self._open_line_count(reservation_id) == 0:
                self._release_reservation(reservation_id, "restriction", "system", now, reason_detail)
        return sorted(affected)

    def _open_line_count(self, reservation_id: int) -> int:
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM reservation_lines WHERE reservation_id=? AND status IN ('active','picking')",
            (reservation_id,),
        ).fetchone()[0])

    def _replenish(self, reservation_id: int, item_ids: set[int], timestamp: str) -> None:
        """质量/限制释放后，按同一套选批规则为缺口追加锁定行。"""
        next_seq = int(self.connection.execute(
            "SELECT COALESCE(MAX(sequence_no),0) FROM reservation_lines WHERE reservation_id=?",
            (reservation_id,),
        ).fetchone()[0])
        for item_id in sorted(item_ids):
            item = record(self.connection.execute(
                "SELECT * FROM distribution_items WHERE id=?", (item_id,)
            ).fetchone())
            if item is None:
                continue
            committed = float(self.connection.execute(
                "SELECT COALESCE(SUM(quantity_grams),0) FROM reservation_lines "
                "WHERE request_item_id=? AND status IN ('active','picking','fulfilled')", (item_id,)
            ).fetchone()[0])
            shortfall = round(float(item["quantity_grams"]) - committed, 6)
            if shortfall <= WEIGHT_EPS:
                self.connection.execute(
                    "UPDATE distribution_items SET status='allocated' WHERE id=?", (item_id,)
                )
                continue
            try:
                plan = self._plan_item(item_id, int(item["accession_id"]), shortfall)
            except ConflictError:
                self.connection.execute(
                    "UPDATE distribution_items SET status='unavailable' WHERE id=?", (item_id,)
                )
                self._event(reservation_id, "shortfall", "system",
                            {"item_id": item_id, "shortfall_grams": shortfall})
                continue
            for line in plan:
                next_seq += 1
                line["rule"]["rank"] = next_seq
                line["rule"]["replenished_after"] = "quality_release"
                self.connection.execute(
                    "INSERT INTO reservation_lines(reservation_id,request_item_id,lot_id,sequence_no,quantity_grams,"
                    "status,rule_json,created_at) VALUES(?,?,?,?,?,'active',?,?)",
                    (
                        reservation_id, line["request_item_id"], line["lot_id"], next_seq,
                        round(line["quantity_grams"], 6),
                        json.dumps(line["rule"], ensure_ascii=False, sort_keys=True), timestamp,
                    ),
                )
            self.connection.execute(
                "UPDATE distribution_items SET status='allocated' WHERE id=?", (item_id,)
            )
            self._event(reservation_id, "replenished", "system", {
                "item_id": item_id, "lines": [
                    {"lot_id": line["lot_id"], "quantity_grams": line["quantity_grams"]} for line in plan
                ],
            })

    def _release_reservation(self, reservation_id: int, reason: str, actor: str, timestamp: str, detail: str) -> None:
        for line in self.connection.execute(
            "SELECT * FROM reservation_lines WHERE reservation_id=? AND status='active' ORDER BY sequence_no",
            (reservation_id,),
        ).fetchall():
            self._release_line(int(line["id"]), reason, actor, timestamp, detail)
        request_id = int(self.require_reservation(reservation_id)["request_id"])
        self.connection.execute(
            "UPDATE reservations SET status='released',release_reason=?,released_at=?,released_by=?,updated_at=? WHERE id=?",
            (reason, timestamp, actor, timestamp, reservation_id),
        )
        self.connection.execute(
            "UPDATE distribution_items SET allocated_lot_id=NULL,status='requested' WHERE request_id=?",
            (request_id,),
        )
        if reason == "cancelled":
            new_status = "cancelled"
        else:
            new_status = "submitted"
        self.connection.execute(
            "UPDATE distribution_requests SET status=?,version=version+1 WHERE id=?", (new_status, request_id)
        )
        self._event(reservation_id, "released", actor, {"reason": reason, "detail": detail})

    def _release_line(self, line_id: int, reason: str, actor: str, timestamp: str, detail: str) -> None:
        cursor = self.connection.execute(
            "UPDATE reservation_lines SET status='released',release_reason=?,released_at=?,released_by=? "
            "WHERE id=? AND status='active'",
            (reason, timestamp, actor, line_id),
        )
        if cursor.rowcount:
            self._event(
                int(self.connection.execute("SELECT reservation_id FROM reservation_lines WHERE id=?", (line_id,)).fetchone()[0]),
                "line_released", actor, {"line_id": line_id, "reason": reason, "detail": detail},
                line_id=line_id,
            )

    # ------------------------------------------------------------------ 拣货

    def start_picking(self, reservation_id: int, actor: str) -> dict[str, Any]:
        reservation = self.require_reservation(reservation_id)
        if reservation["status"] != "active":
            raise ConflictError("只有生效中的预约可以开始拣货")
        timestamp = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE reservation_lines SET status='picking' WHERE reservation_id=? AND status='active'",
            (reservation_id,),
        )
        self.connection.execute(
            "UPDATE reservations SET status='picking',updated_at=? WHERE id=?", (timestamp, reservation_id)
        )
        self.connection.execute(
            "UPDATE distribution_requests SET status='picking' WHERE id=?", (reservation["request_id"],)
        )
        self._event(reservation_id, "pick_started", actor, {})
        return self.reservation_detail(reservation_id)

    def pick(self, line_id: int, data: dict[str, Any]) -> dict[str, Any]:
        return self._fulfill(line_id, data, kind="pick")

    def return_line(self, line_id: int, data: dict[str, Any]) -> dict[str, Any]:
        return self._fulfill(line_id, data, kind="return")

    def _fulfill(self, line_id: int, data: dict[str, Any], *, kind: str) -> dict[str, Any]:
        line = self.require_line(line_id)
        reservation = self.require_reservation(int(line["reservation_id"]))
        if reservation["status"] != "picking":
            raise ConflictError("只有拣货中的预约可以登记拣货或回退")
        if line["status"] not in {"picking", "fulfilled"}:
            raise ConflictError("该预约明细已释放，不能登记拣货")
        actor = str(data["actor"])
        if reservation["taken_over_by"] and reservation["taken_over_by"] != actor and not data.get("_can_manage"):
            raise ConflictError("预约已由他人接管，请联系接管人或有权限的人处理",
                                context={"taken_over_by": reservation["taken_over_by"]})
        quantity = round(float(data["quantity_grams"]), 6)
        if quantity <= 0:
            raise ValidationError("拣货/回退重量必须为正数")
        replay = self.connection.execute(
            "SELECT * FROM reservation_fulfillments WHERE idempotency_key=?", (data["idempotency_key"],)
        ).fetchone()
        if replay is not None:
            if int(replay["reservation_line_id"]) != line_id or replay["kind"] != kind:
                raise ConflictError("同一幂等键对应了不同的拣货操作")
            payload = self._fulfillment_payload(int(replay["id"]))
            payload["replayed"] = True
            return payload
        lot = self.repository.require_lot(int(line["lot_id"]))
        timestamp = to_storage(self.clock.now())
        if kind == "pick":
            if line["status"] == "fulfilled":
                raise ConflictError("该明细已拣货完成")
            holds = self.repository.active_holds(int(lot["id"]))
            if holds:
                raise ConflictError("批次存在未解除的质量冻结，不能拣货，请申请接管或回退",
                                    context={"holds": [item["id"] for item in holds]})
        reason_text = str(data.get("reason") or "").strip()
        if kind == "return" and not reason_text:
            raise ValidationError("回退时必须填写原因")
        movement_type = "领用" if kind == "pick" else "归还"
        signed = -quantity if kind == "pick" else quantity
        movement_key = f"res{'pick' if kind == 'pick' else 'ret'}-{data['idempotency_key']}"
        movement_cursor = self.connection.execute(
            "INSERT INTO lot_movements(lot_id,movement_type,quantity_grams,idempotency_key,actor,reason,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                lot["id"], movement_type, signed, movement_key, actor,
                reason_text or f"预约 {reservation['id']} {'拣货出库' if kind == 'pick' else '回退入库'}", timestamp,
            ),
        )
        if kind == "pick":
            new_available = round(float(lot["available_weight_grams"]) - quantity, 6)
        else:
            new_available = round(float(lot["available_weight_grams"]) + quantity, 6)
        self.connection.execute(
            "UPDATE seed_lots SET available_weight_grams=?,version=version+1,updated_at=? WHERE id=?",
            (new_available, timestamp, lot["id"]),
        )
        try:
            cursor = self.connection.execute(
                "INSERT INTO reservation_fulfillments(reservation_line_id,lot_id,movement_id,kind,quantity_grams,"
                "shipment_no,consignee,actor,reason,created_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    line_id, lot["id"], movement_cursor.lastrowid, kind, quantity,
                    data.get("shipment_no"), data.get("consignee", ""), actor, reason_text, timestamp,
                    data["idempotency_key"],
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("拣货幂等键冲突或重量超出预约边界") from exc
        net_picked = float(self.connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN kind='pick' THEN quantity_grams ELSE -quantity_grams END),0) "
            "FROM reservation_fulfillments WHERE reservation_line_id=?", (line_id,)
        ).fetchone()[0])
        if kind == "pick":
            line_status = "fulfilled" if net_picked + WEIGHT_EPS >= float(line["quantity_grams"]) else "picking"
            picked_at = timestamp
        else:
            line_status = "picking"
            picked_at = timestamp if net_picked > WEIGHT_EPS else None
        self.connection.execute(
            "UPDATE reservation_lines SET picked_grams=?,status=?,picked_at=? WHERE id=?",
            (round(net_picked, 6), line_status, picked_at, line_id),
        )
        self._event(reservation["id"], "picked" if kind == "pick" else "returned", actor, {
            "line_id": line_id, "lot_id": lot["id"], "quantity_grams": quantity,
            "shipment_no": data.get("shipment_no"), "consignee": data.get("consignee", ""),
        }, line_id=line_id)
        if line_status == "fulfilled":
            self._maybe_complete(reservation["id"], actor, timestamp)
        return self._fulfillment_payload(int(cursor.lastrowid))

    def takeover(self, reservation_id: int, actor: str) -> dict[str, Any]:
        reservation = self.require_reservation(reservation_id)
        if reservation["status"] != "picking":
            raise ConflictError("只有拣货中的预约可以接管")
        timestamp = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE reservations SET taken_over_by=?,taken_over_at=?,updated_at=? WHERE id=?",
            (actor, timestamp, timestamp, reservation_id),
        )
        self._event(reservation_id, "taken_over", actor, {"previous": reservation["taken_over_by"]})
        return self.reservation_detail(reservation_id)

    def rollback(self, reservation_id: int, actor: str, reason: str) -> dict[str, Any]:
        reservation = self.require_reservation(reservation_id)
        if reservation["status"] != "picking":
            raise ConflictError("只有拣货中的预约可以整单回退")
        if not reason.strip():
            raise ValidationError("整单回退必须填写原因")
        timestamp = to_storage(self.clock.now())
        index = 0
        for line in self.connection.execute(
            "SELECT * FROM reservation_lines WHERE reservation_id=? AND status IN ('picking','fulfilled') ORDER BY sequence_no",
            (reservation_id,),
        ).fetchall():
            net_picked = float(self.connection.execute(
                "SELECT COALESCE(SUM(CASE WHEN kind='pick' THEN quantity_grams ELSE -quantity_grams END),0) "
                "FROM reservation_fulfillments WHERE reservation_line_id=?", (line["id"],)
            ).fetchone()[0])
            if net_picked > WEIGHT_EPS:
                index += 1
                self._fulfill(int(line["id"]), {
                    "quantity_grams": round(net_picked, 6),
                    "reason": f"整单回退：{reason}",
                    "idempotency_key": f"rollback-{reservation_id}-{line['id']}-{index}",
                    "actor": actor,
                    "_can_manage": True,
                }, kind="return")
            self.connection.execute(
                "UPDATE reservation_lines SET status='released',release_reason='manual',released_at=?,released_by=? "
                "WHERE id=? AND status='picking'",
                (timestamp, actor, line["id"]),
            )
        self.connection.execute(
            "UPDATE reservations SET status='released',release_reason='manual',released_at=?,released_by=?,updated_at=? "
            "WHERE id=?",
            (timestamp, actor, timestamp, reservation_id),
        )
        self.connection.execute(
            "UPDATE distribution_items SET allocated_lot_id=NULL,status='requested' WHERE request_id=?",
            (reservation["request_id"],),
        )
        self.connection.execute(
            "UPDATE distribution_requests SET status='submitted',version=version+1 WHERE id=?",
            (reservation["request_id"],),
        )
        self._event(reservation_id, "rolled_back", actor, {"reason": reason})
        return self.reservation_detail(reservation_id)

    def _maybe_complete(self, reservation_id: int, actor: str, timestamp: str) -> None:
        row = self.connection.execute(
            "SELECT COUNT(*) FILTER (WHERE status!='fulfilled') FROM reservation_lines WHERE reservation_id=?",
            (reservation_id,),
        ).fetchone()[0]
        if int(row) != 0:
            return
        self.connection.execute(
            "UPDATE reservations SET status='fulfilled',updated_at=? WHERE id=? AND status='picking'",
            (timestamp, reservation_id),
        )
        reservation = self.require_reservation(reservation_id)
        self.connection.execute(
            "UPDATE distribution_items SET status='fulfilled' WHERE request_id=?", (reservation["request_id"],)
        )
        self.connection.execute(
            "UPDATE distribution_requests SET status='fulfilled',version=version+1 WHERE id=?",
            (reservation["request_id"],),
        )
        self._event(reservation_id, "fulfilled", actor, {})

    # ------------------------------------------------------------------ 查询

    def require_reservation(self, reservation_id: int) -> dict[str, Any]:
        item = record(self.connection.execute(
            "SELECT * FROM reservations WHERE id=?", (reservation_id,)
        ).fetchone())
        if item is None:
            raise NotFoundError("库存预约不存在")
        return item

    def require_line(self, line_id: int) -> dict[str, Any]:
        item = record(self.connection.execute(
            "SELECT * FROM reservation_lines WHERE id=?", (line_id,)
        ).fetchone())
        if item is None:
            raise NotFoundError("预约明细不存在")
        return item

    def reservation_detail(self, reservation_id: int) -> dict[str, Any]:
        reservation = self.require_reservation(reservation_id)
        request = self.repository.require_distribution(int(reservation["request_id"]))
        reservation["request_no"] = request["request_no"]
        lines = records(self.connection.execute(
            "SELECT ln.*,l.lot_no,a.accession_no,a.crop_name FROM reservation_lines ln "
            "JOIN seed_lots l ON l.id=ln.lot_id "
            "JOIN distribution_items di ON di.id=ln.request_item_id "
            "JOIN accessions a ON a.id=di.accession_id "
            "WHERE ln.reservation_id=? ORDER BY ln.sequence_no", (reservation_id,)
        ).fetchall())
        for line in lines:
            line["rule"] = _loads(line.pop("rule_json", "{}"))
            line["fulfillments"] = records(self.connection.execute(
                "SELECT * FROM reservation_fulfillments WHERE reservation_line_id=? ORDER BY id", (line["id"],)
            ).fetchall())
            line["outstanding_grams"] = round(float(line["quantity_grams"]) - float(line["picked_grams"]), 6)
        reservation["lines"] = lines
        reservation["events"] = [
            {**event, "detail": _loads(event.pop("detail_json", "{}"))}
            for event in records(self.connection.execute(
                "SELECT * FROM reservation_events WHERE reservation_id=? ORDER BY id", (reservation_id,)
            ).fetchall())
        ]
        totals = self.connection.execute(
            "SELECT COALESCE(SUM(quantity_grams),0),COALESCE(SUM(picked_grams),0) "
            "FROM reservation_lines WHERE reservation_id=? AND status IN ('active','picking','fulfilled')",
            (reservation_id,),
        ).fetchone()
        reservation["reserved_grams"] = round(float(totals[0]), 6)
        reservation["picked_grams"] = round(float(totals[1]), 6)
        return reservation

    def lot_balance(self, lot_id: int) -> dict[str, Any]:
        """批次视角：实物、已预约（净锁定）、可再预约、已出库数量。"""
        lot = self.repository.require_lot(lot_id)
        locked = float(self.connection.execute(
            "SELECT COALESCE(SUM(quantity_grams - picked_grams),0) FROM reservation_lines "
            "WHERE lot_id=? AND status IN ('active','picking')", (lot_id,)
        ).fetchone()[0])
        shipped = float(self.connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN kind='pick' THEN quantity_grams ELSE -quantity_grams END),0) "
            "FROM reservation_fulfillments WHERE lot_id=?", (lot_id,)
        ).fetchone()[0])
        on_hand = float(lot["available_weight_grams"])
        return {
            "lot_id": lot_id,
            "on_hand_grams": round(on_hand, 6),
            "reserved_grams": round(locked, 6),
            "available_for_reservation_grams": round(on_hand - locked, 6),
            "distributed_grams": round(shipped, 6),
        }

    def lot_allocations(self, lot_id: int) -> list[dict[str, Any]]:
        """批次被哪些申请、以什么顺序、最终去向占用——出库人员的分配证据。"""
        rows = records(self.connection.execute(
            "SELECT ln.id AS line_id,ln.reservation_id,ln.sequence_no,ln.quantity_grams,ln.picked_grams,"
            "ln.status,ln.release_reason,ln.rule_json,dr.request_no,dr.requester,"
            "a.accession_no,a.crop_name FROM reservation_lines ln "
            "JOIN reservations r ON r.id=ln.reservation_id "
            "JOIN distribution_requests dr ON dr.id=r.request_id "
            "JOIN distribution_items di ON di.id=ln.request_item_id "
            "JOIN accessions a ON a.id=di.accession_id "
            "WHERE ln.lot_id=? ORDER BY ln.reservation_id,ln.sequence_no", (lot_id,)
        ).fetchall())
        for row in rows:
            row["rule"] = _loads(row.pop("rule_json", "{}"))
        return rows

    def open_reservation_of_request(self, request_id: int) -> dict[str, Any] | None:
        row = self._open_reservation_row(request_id)
        return record(row)

    def latest_reservation_of_request(self, request_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM reservations WHERE request_id=? ORDER BY id DESC LIMIT 1", (request_id,)
        ).fetchone()
        return record(row)

    def _open_reservation_row(self, request_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM reservations WHERE request_id=? AND status IN ('active','picking') ORDER BY id DESC LIMIT 1",
            (request_id,),
        ).fetchone()

    def _fulfillment_payload(self, fulfillment_id: int) -> dict[str, Any]:
        row = record(self.connection.execute(
            "SELECT * FROM reservation_fulfillments WHERE id=?", (fulfillment_id,)
        ).fetchone()) or {}
        line = self.require_line(int(row["reservation_line_id"]))
        return {
            "fulfillment": row,
            "line": self.require_line(int(row["reservation_line_id"])),
            "reservation": self.reservation_detail(int(line["reservation_id"])),
            "replayed": False,
        }

    def _event(
        self,
        reservation_id: int,
        action: str,
        actor: str,
        detail: dict[str, Any],
        *,
        line_id: int | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO reservation_events(reservation_id,reservation_line_id,action,actor,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                reservation_id, line_id, action, actor,
                json.dumps(detail, ensure_ascii=False, sort_keys=True), to_storage(self.clock.now()),
            ),
        )

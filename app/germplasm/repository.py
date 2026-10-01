from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from app.core.errors import NotFoundError


JSON_COLUMNS = {
    "passport_json": "passport",
    "restrictions_json": "restrictions",
    "detail_json": "detail",
    "payload_json": "payload",
}


def record(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for column, target in JSON_COLUMNS.items():
        if column in data:
            raw = data.pop(column)
            try:
                data[target] = json.loads(raw or "{}")
            except json.JSONDecodeError:
                data[target] = {}
    return data


def records(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [record(row) or {} for row in rows]


class GermplasmRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def require_source(self, source_id: int) -> dict[str, Any]:
        item = record(self.connection.execute("SELECT * FROM collection_sources WHERE id=?", (source_id,)).fetchone())
        if item is None:
            raise NotFoundError("来源记录不存在")
        return item

    def require_accession(self, accession_id: int) -> dict[str, Any]:
        item = record(self.connection.execute("SELECT * FROM accessions WHERE id=?", (accession_id,)).fetchone())
        if item is None:
            raise NotFoundError("种质资源不存在")
        return item

    def accession_by_number(self, accession_no: str) -> dict[str, Any] | None:
        return record(self.connection.execute("SELECT * FROM accessions WHERE accession_no=?", (accession_no,)).fetchone())

    def accession_detail(self, accession_id: int) -> dict[str, Any]:
        item = self.require_accession(accession_id)
        if item.get("source_id"):
            item["source"] = self.require_source(int(item["source_id"]))
        item["lots"] = records(self.connection.execute(
            "SELECT * FROM seed_lots WHERE accession_id=? ORDER BY created_at,lot_no", (accession_id,)
        ).fetchall())
        item["events"] = records(self.connection.execute(
            "SELECT * FROM accession_events WHERE accession_id=? ORDER BY id", (accession_id,)
        ).fetchall())
        return item

    def list_accessions(self, *, status: str | None, crop: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status=?")
            params.append(status)
        if crop:
            where.append("crop_name LIKE ?")
            params.append(f"%{crop.strip()}%")
        clause = " WHERE " + " AND ".join(where) if where else ""
        total = int(self.connection.execute(f"SELECT COUNT(*) FROM accessions{clause}", params).fetchone()[0])
        params.extend([limit, offset])
        rows = self.connection.execute(
            f"SELECT * FROM accessions{clause} ORDER BY received_on DESC,accession_no LIMIT ? OFFSET ?", params
        ).fetchall()
        return records(rows), total

    def require_location(self, location_id: int) -> dict[str, Any]:
        item = record(self.connection.execute("SELECT * FROM storage_locations WHERE id=?", (location_id,)).fetchone())
        if item is None:
            raise NotFoundError("库位不存在")
        return item

    def location_usage(self, location_id: int) -> float:
        row = self.connection.execute(
            "SELECT COALESCE(SUM(weight_grams),0) FROM lot_placements WHERE location_id=? AND removed_at IS NULL",
            (location_id,),
        ).fetchone()
        return float(row[0])

    def location_detail(self, location_id: int) -> dict[str, Any]:
        item = self.require_location(location_id)
        item["used_grams"] = self.location_usage(location_id)
        item["available_grams"] = round(float(item["capacity_grams"]) - item["used_grams"], 6)
        item["placements"] = records(self.connection.execute(
            "SELECT p.*,l.lot_no FROM lot_placements p JOIN seed_lots l ON l.id=p.lot_id "
            "WHERE p.location_id=? AND p.removed_at IS NULL ORDER BY p.container_code", (location_id,)
        ).fetchall())
        return item

    def require_lot(self, lot_id: int) -> dict[str, Any]:
        item = record(self.connection.execute("SELECT * FROM seed_lots WHERE id=?", (lot_id,)).fetchone())
        if item is None:
            raise NotFoundError("种子批次不存在")
        return item

    def active_holds(self, lot_id: int) -> list[dict[str, Any]]:
        return records(self.connection.execute(
            "SELECT * FROM lot_holds WHERE lot_id=? AND released_at IS NULL ORDER BY id", (lot_id,)
        ).fetchall())

    def lot_detail(self, lot_id: int) -> dict[str, Any]:
        item = self.require_lot(lot_id)
        item["accession"] = self.require_accession(int(item["accession_id"]))
        item["placements"] = records(self.connection.execute(
            "SELECT p.*,s.location_code FROM lot_placements p JOIN storage_locations s ON s.id=p.location_id "
            "WHERE p.lot_id=? ORDER BY p.id", (lot_id,)
        ).fetchall())
        item["movements"] = records(self.connection.execute(
            "SELECT * FROM lot_movements WHERE lot_id=? ORDER BY id", (lot_id,)
        ).fetchall())
        item["holds"] = records(self.connection.execute(
            "SELECT * FROM lot_holds WHERE lot_id=? ORDER BY id", (lot_id,)
        ).fetchall())
        item["latest_viability"] = record(self.connection.execute(
            "SELECT * FROM viability_tests WHERE lot_id=? AND status='completed' ORDER BY completed_at DESC,id DESC LIMIT 1",
            (lot_id,),
        ).fetchone())
        return item

    def require_placement(self, placement_id: int) -> dict[str, Any]:
        item = record(self.connection.execute("SELECT * FROM lot_placements WHERE id=?", (placement_id,)).fetchone())
        if item is None:
            raise NotFoundError("容器摆放记录不存在")
        return item

    def movement_by_key(self, key: str) -> dict[str, Any] | None:
        return record(self.connection.execute("SELECT * FROM lot_movements WHERE idempotency_key=?", (key,)).fetchone())

    def require_protocol(self, protocol_id: int) -> dict[str, Any]:
        item = record(self.connection.execute("SELECT * FROM viability_protocols WHERE id=?", (protocol_id,)).fetchone())
        if item is None:
            raise NotFoundError("活力检测规程不存在")
        return item

    def protocol_latest(self, code: str) -> dict[str, Any] | None:
        return record(self.connection.execute(
            "SELECT * FROM viability_protocols WHERE protocol_code=? ORDER BY version DESC LIMIT 1", (code,)
        ).fetchone())

    def require_test(self, test_id: int) -> dict[str, Any]:
        item = record(self.connection.execute("SELECT * FROM viability_tests WHERE id=?", (test_id,)).fetchone())
        if item is None:
            raise NotFoundError("活力检测任务不存在")
        return item

    def test_detail(self, test_id: int) -> dict[str, Any]:
        item = self.require_test(test_id)
        item["lot"] = self.require_lot(int(item["lot_id"]))
        item["protocol"] = self.require_protocol(int(item["protocol_id"]))
        item["counts"] = records(self.connection.execute(
            "SELECT * FROM viability_counts WHERE test_id=? ORDER BY replicate_no,observation_day", (test_id,)
        ).fetchall())
        return item

    def require_policy(self, policy_id: int) -> dict[str, Any]:
        item = record(self.connection.execute("SELECT * FROM retest_policies WHERE id=?", (policy_id,)).fetchone())
        if item is None:
            raise NotFoundError("复检策略不存在")
        return item

    def applicable_policy(self, crop_name: str, risk_level: str, on_date: str) -> dict[str, Any] | None:
        return record(self.connection.execute(
            "SELECT * FROM retest_policies WHERE crop_name=? AND risk_level=? AND effective_from<=? "
            "AND (effective_to IS NULL OR effective_to>=?) ORDER BY version DESC LIMIT 1",
            (crop_name, risk_level, on_date, on_date),
        ).fetchone())

    def require_alert(self, alert_id: int) -> dict[str, Any]:
        item = record(self.connection.execute("SELECT * FROM quality_alerts WHERE id=?", (alert_id,)).fetchone())
        if item is None:
            raise NotFoundError("质量告警不存在")
        return item

    def require_distribution(self, request_id: int) -> dict[str, Any]:
        item = record(self.connection.execute("SELECT * FROM distribution_requests WHERE id=?", (request_id,)).fetchone())
        if item is None:
            raise NotFoundError("发放申请不存在")
        return item

    def distribution_detail(self, request_id: int) -> dict[str, Any]:
        item = self.require_distribution(request_id)
        rows = records(self.connection.execute(
            "SELECT i.*,a.accession_no,a.crop_name,"
                        "(SELECT MIN(ln.lot_id) FROM reservation_lines ln WHERE ln.request_item_id=i.id "
            "AND ln.status IN ('active','picking','fulfilled') "
            "HAVING COUNT(DISTINCT ln.lot_id)=1) AS resolved_lot_id "
            "FROM distribution_items i JOIN accessions a ON a.id=i.accession_id "
            "WHERE i.request_id=? ORDER BY i.id", (request_id,)
        ).fetchall())
        for row in rows:
            # 分配已迁移到 reservation_lines（可跨批次）；单一批次时回填旧字段保持兼容。
            row["allocated_lot_id"] = row.get("allocated_lot_id") or row.pop("resolved_lot_id", None)
        item["items"] = rows
        return item

    def count_table(self, table: str) -> int:
        allowed = {
            "accessions", "seed_lots", "storage_locations", "viability_tests",
            "retest_schedules", "quality_alerts", "distribution_requests",
        }
        if table not in allowed:
            raise ValueError("不允许统计该数据表")
        return int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

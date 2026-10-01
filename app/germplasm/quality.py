from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, ValidationError
from app.germplasm.repository import GermplasmRepository, record, records


class QualityService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = GermplasmRepository(connection)

    def add_reading(self, data: dict[str, Any]) -> dict[str, Any]:
        location = self.repository.require_location(int(data["location_id"]))
        observed_at = data["observed_at"]
        timestamp = to_storage(self.clock.now())
        try:
            cursor = self.connection.execute(
                "INSERT INTO environment_readings(location_id,observed_at,temperature_c,humidity_percent,source_key,imported_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    location["id"], to_storage(observed_at), data["temperature_c"], data["humidity_percent"],
                    data["source_key"], timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            existing = record(self.connection.execute(
                "SELECT * FROM environment_readings WHERE location_id=? AND source_key=?",
                (location["id"], data["source_key"]),
            ).fetchone())
            if existing and (
                float(existing["temperature_c"]) != float(data["temperature_c"])
                or float(existing["humidity_percent"]) != float(data["humidity_percent"])
            ):
                raise ConflictError("同一来源读数键对应了不同数据")
            return {"reading": existing, "replayed": True, "alerts": []}
        reading = record(self.connection.execute("SELECT * FROM environment_readings WHERE id=?", (cursor.lastrowid,)).fetchone())
        alerts = self._evaluate_reading(location, reading or {}, timestamp)
        return {"reading": reading, "replayed": False, "alerts": alerts}

    def import_readings(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for index, item in enumerate(rows, start=1):
            marker = f"reading_import_{index}"
            self.connection.execute(f"SAVEPOINT {marker}")
            try:
                result = self.add_reading(item)
                self.connection.execute(f"RELEASE SAVEPOINT {marker}")
                accepted.append({"row": index, "id": result["reading"]["id"], "replayed": result["replayed"]})
            except Exception as exc:
                self.connection.execute(f"ROLLBACK TO SAVEPOINT {marker}")
                self.connection.execute(f"RELEASE SAVEPOINT {marker}")
                rejected.append({"row": index, "reason": str(exc)})
        return {"accepted": accepted, "rejected": rejected, "accepted_count": len(accepted), "rejected_count": len(rejected)}

    def decide_alert(self, alert_id: int, data: dict[str, Any]) -> dict[str, Any]:
        alert = self.repository.require_alert(alert_id)
        action = data["action"]
        timestamp = to_storage(self.clock.now())
        if action == "acknowledge":
            if alert["status"] != "open":
                raise ConflictError("只有未处理告警可以确认")
            self.connection.execute(
                "UPDATE quality_alerts SET status='acknowledged',acknowledged_by=?,acknowledged_at=?,updated_at=? WHERE id=?",
                (data["actor"], timestamp, timestamp, alert_id),
            )
        elif action == "resolve":
            if alert["status"] not in {"open", "acknowledged"}:
                raise ConflictError("当前告警状态不能关闭")
            self.connection.execute(
                "UPDATE quality_alerts SET status='resolved',resolved_at=?,updated_at=? WHERE id=?",
                (timestamp, timestamp, alert_id),
            )
        else:
            if alert["status"] != "open":
                raise ConflictError("只有未处理告警可以忽略")
            if not data.get("reason"):
                raise ValidationError("忽略告警时必须填写原因")
            details = alert.get("detail", {})
            details["dismiss_reason"] = data["reason"]
            details["dismissed_by"] = data["actor"]
            self.connection.execute(
                "UPDATE quality_alerts SET status='dismissed',detail_json=?,updated_at=? WHERE id=?",
                (json.dumps(details, ensure_ascii=False), timestamp, alert_id),
            )
        return self.repository.require_alert(alert_id)

    def open_alerts(self, severity: str | None = None) -> list[dict[str, Any]]:
        if severity:
            rows = self.connection.execute(
                "SELECT * FROM quality_alerts WHERE status IN ('open','acknowledged') AND severity=? "
                "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,created_at",
                (severity,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM quality_alerts WHERE status IN ('open','acknowledged') "
                "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,created_at"
            ).fetchall()
        return records(rows)

    def location_summary(self, location_id: int, hours: int = 24) -> dict[str, Any]:
        location = self.repository.location_detail(location_id)
        cutoff = to_storage(self.clock.now() - timedelta(hours=hours))
        row = self.connection.execute(
            "SELECT COUNT(*) AS samples,MIN(temperature_c) AS min_temperature,MAX(temperature_c) AS max_temperature,"
            "AVG(temperature_c) AS avg_temperature,MIN(humidity_percent) AS min_humidity,"
            "MAX(humidity_percent) AS max_humidity,AVG(humidity_percent) AS avg_humidity "
            "FROM environment_readings WHERE location_id=? AND observed_at>=?",
            (location_id, cutoff),
        ).fetchone()
        alerts = self.connection.execute(
            "SELECT COUNT(*) FROM quality_alerts WHERE location_id=? AND status IN ('open','acknowledged')",
            (location_id,),
        ).fetchone()[0]
        return {"location": location, "window_hours": hours, "readings": dict(row), "open_alerts": int(alerts)}

    def _evaluate_reading(self, location: dict[str, Any], reading: dict[str, Any], timestamp: str) -> list[dict[str, Any]]:
        deviations: list[tuple[str, float, float]] = []
        temperature_delta = abs(float(reading["temperature_c"]) - float(location["temperature_c"]))
        humidity_delta = abs(float(reading["humidity_percent"]) - float(location["humidity_percent"]))
        if temperature_delta > 5:
            deviations.append(("temperature", float(reading["temperature_c"]), float(location["temperature_c"])))
        if humidity_delta > 15:
            deviations.append(("humidity", float(reading["humidity_percent"]), float(location["humidity_percent"])))
        alerts: list[dict[str, Any]] = []
        for metric, observed, target in deviations:
            digest = hashlib.sha256(f"{location['id']}:{reading['id']}:{metric}".encode()).hexdigest()[:20]
            severity = "critical" if abs(observed - target) > (10 if metric == "temperature" else 30) else "warning"
            message = f"库位 {location['location_code']} 的{('温度' if metric == 'temperature' else '湿度')}偏离设定值"
            detail = {"reading_id": reading["id"], "metric": metric, "observed": observed, "target": target}
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO quality_alerts(alert_key,alert_type,severity,location_id,message,detail_json,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (f"environment-{digest}", "environment_excursion", severity, location["id"], message, json.dumps(detail), timestamp, timestamp),
            )
            if cursor.rowcount:
                alerts.append(self.repository.require_alert(int(cursor.lastrowid)))
        return alerts


class DistributionService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = GermplasmRepository(connection)

    def create_request(self, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = to_storage(self.clock.now())
        seen: set[int] = set()
        normalized: list[tuple[int, float]] = []
        for raw in data["items"]:
            accession_id = int(raw.get("accession_id", 0))
            quantity = float(raw.get("quantity_grams", 0))
            if accession_id <= 0 or quantity <= 0:
                raise ValidationError("发放明细必须包含有效资源和正数重量")
            if accession_id in seen:
                raise ValidationError("同一资源不能在申请中重复出现")
            seen.add(accession_id)
            accession = self.repository.require_accession(accession_id)
            if accession["status"] != "accepted":
                raise ConflictError("仅正式接收的资源可以申请发放", context={"accession_id": accession_id})
            normalized.append((accession_id, quantity))
        try:
            cursor = self.connection.execute(
                "INSERT INTO distribution_requests(request_no,requester,purpose,status,requested_at) VALUES(?,?,?,'draft',?)",
                (data["request_no"], data["requester"], data["purpose"], timestamp),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("发放申请编号已经存在") from exc
        request_id = int(cursor.lastrowid)
        for accession_id, quantity in normalized:
            self.connection.execute(
                "INSERT INTO distribution_items(request_id,accession_id,quantity_grams) VALUES(?,?,?)",
                (request_id, accession_id, quantity),
            )
        return self.repository.distribution_detail(request_id)

    def submit(self, request_id: int, expected_version: int) -> dict[str, Any]:
        request = self.repository.require_distribution(request_id)
        if request["status"] != "draft":
            raise ConflictError("只有草稿申请可以提交")
        if int(request["version"]) != expected_version:
            raise ConflictError("发放申请版本冲突", context={"current_version": request["version"]})
        self.connection.execute(
            "UPDATE distribution_requests SET status='submitted',version=version+1 WHERE id=? AND version=?",
            (request_id, expected_version),
        )
        return self.repository.distribution_detail(request_id)

    def decide(self, request_id: int, data: dict[str, Any]) -> dict[str, Any]:
        request = self.repository.require_distribution(request_id)
        if request["status"] not in {"submitted", "approved", "picking"}:
            if request["status"] == "draft":
                raise ConflictError("只有已提交申请可以审批")
            raise ConflictError("当前申请状态不能审批")
        if not data["approve"]:
            if request["status"] != "submitted":
                raise ConflictError("只有待审批申请可以拒绝")
            if int(request["version"]) != int(data["expected_version"]):
                raise ConflictError("发放申请版本冲突", context={"current_version": request["version"]})
            if not data.get("reason"):
                raise ValidationError("拒绝申请时必须填写原因")
            timestamp = to_storage(self.clock.now())
            self.connection.execute(
                "UPDATE distribution_requests SET status='rejected',reviewed_by=?,reviewed_at=?,decision_reason=?,"
                "version=version+1 WHERE id=? AND version=?",
                (data["actor"], timestamp, data["reason"], request_id, data["expected_version"]),
            )
            return self.repository.distribution_detail(request_id)
        # 批准：在同一事务内选批并锁定重量；预约已存在时（审批重放）原额返回，不重复占用。
        from app.germplasm.reservations import ReservationService

        reservation = ReservationService(self.connection, self.clock).reserve_for_decision(request_id, data)
        result = self.repository.distribution_detail(request_id)
        result["reservation"] = reservation
        return result

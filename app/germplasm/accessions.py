from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, ValidationError
from app.germplasm.repository import GermplasmRepository, record


ALLOWED_TRANSITIONS = {
    "draft": {"quarantine", "accepted", "retired"},
    "quarantine": {"accepted", "restricted", "retired"},
    "accepted": {"restricted", "retired"},
    "restricted": {"accepted", "retired"},
    "retired": set(),
}


class AccessionService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = GermplasmRepository(connection)

    def create_source(self, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = to_storage(self.clock.now())
        try:
            cursor = self.connection.execute(
                "INSERT INTO collection_sources(source_code,provider_name,country_code,locality,collected_on,"
                "permit_reference,restrictions_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    data["source_code"], data["provider_name"], data["country_code"], data.get("locality", ""),
                    data.get("collected_on"), data.get("permit_reference"),
                    json.dumps(data.get("restrictions", {}), ensure_ascii=False, sort_keys=True), timestamp, timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("来源编码已经存在") from exc
        return self.repository.require_source(int(cursor.lastrowid))

    def update_source_restrictions(self, source_id: int, restrictions: dict[str, Any]) -> dict[str, Any]:
        before = self.repository.require_source(source_id)
        timestamp = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE collection_sources SET restrictions_json=?,updated_at=? WHERE id=?",
            (json.dumps(restrictions, ensure_ascii=False, sort_keys=True), timestamp, source_id),
        )
        after = self.repository.require_source(source_id)
        self._outbox(
            f"source-restrictions-{source_id}-{timestamp}", "source.restrictions.changed", "source", source_id,
            {"before": before.get("restrictions", {}), "after": after.get("restrictions", {})}, timestamp,
        )
        return after

    def create_accession(self, data: dict[str, Any]) -> dict[str, Any]:
        if data.get("source_id"):
            self.repository.require_source(int(data["source_id"]))
        if self.repository.accession_by_number(data["accession_no"]):
            raise ConflictError("种质资源编号已经存在")
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "INSERT INTO accessions(accession_no,scientific_name,crop_name,cultivar_name,source_id,acquisition_type,"
            "received_on,status,passport_json,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'draft',?,?,?,?)",
            (
                data["accession_no"], data["scientific_name"], data["crop_name"], data.get("cultivar_name", ""),
                data.get("source_id"), data["acquisition_type"], data["received_on"],
                json.dumps(data.get("passport", {}), ensure_ascii=False, sort_keys=True), data["created_by"],
                timestamp, timestamp,
            ),
        )
        accession_id = int(cursor.lastrowid)
        self._event(accession_id, "created", data["created_by"], None, "draft", {"number": data["accession_no"]})
        self._outbox(
            f"accession-created-{accession_id}", "accession.created", "accession", accession_id,
            {"accession_no": data["accession_no"], "crop_name": data["crop_name"]}, timestamp,
        )
        return self.repository.accession_detail(accession_id)

    def update_accession(self, accession_id: int, data: dict[str, Any]) -> dict[str, Any]:
        before = self.repository.require_accession(accession_id)
        if int(before["version"]) != int(data["expected_version"]):
            raise ConflictError("种质资源已被其他人修改", context={"current_version": before["version"]})
        if before["status"] == "retired":
            raise ConflictError("已退出保存的资源不能修改")
        allowed = {key: value for key, value in data.items() if key in {
            "scientific_name", "crop_name", "cultivar_name", "source_id", "passport"
        } and value is not None}
        if not allowed:
            raise ValidationError("没有可更新的资源字段")
        if "source_id" in allowed:
            self.repository.require_source(int(allowed["source_id"]))
        columns: list[str] = []
        params: list[Any] = []
        for key, value in allowed.items():
            if key == "passport":
                columns.append("passport_json=?")
                params.append(json.dumps(value, ensure_ascii=False, sort_keys=True))
            else:
                columns.append(f"{key}=?")
                params.append(value)
        timestamp = to_storage(self.clock.now())
        params.extend([timestamp, accession_id, data["expected_version"]])
        cursor = self.connection.execute(
            f"UPDATE accessions SET {','.join(columns)},version=version+1,updated_at=? WHERE id=? AND version=?",
            params,
        )
        if cursor.rowcount != 1:
            raise ConflictError("种质资源版本冲突")
        after = self.repository.require_accession(accession_id)
        self._event(accession_id, "updated", data["actor"], before["status"], after["status"], {
            "changed_fields": sorted(allowed), "before_version": before["version"], "after_version": after["version"]
        })
        return self.repository.accession_detail(accession_id)

    def transition(self, accession_id: int, data: dict[str, Any]) -> dict[str, Any]:
        before = self.repository.require_accession(accession_id)
        current = str(before["status"])
        target = str(data["target_status"])
        if int(before["version"]) != int(data["expected_version"]):
            raise ConflictError("种质资源状态版本冲突", context={"current_version": before["version"]})
        if target not in ALLOWED_TRANSITIONS.get(current, set()):
            raise ConflictError("不允许执行该资源状态转换", context={"from": current, "to": target})
        reason = data.get("reason", "").strip()
        if target in {"quarantine", "restricted", "retired"} and not reason:
            raise ValidationError("隔离、限制或退出保存时必须填写原因")
        if target == "accepted" and before.get("source_id") is None:
            raise ValidationError("正式接收前必须登记来源信息")
        if target == "accepted" and not before.get("scientific_name"):
            raise ValidationError("正式接收前必须登记学名")
        timestamp = to_storage(self.clock.now())
        quarantine_reason = reason if target in {"quarantine", "restricted"} else ""
        cursor = self.connection.execute(
            "UPDATE accessions SET status=?,quarantine_reason=?,version=version+1,updated_at=? WHERE id=? AND version=?",
            (target, quarantine_reason, timestamp, accession_id, data["expected_version"]),
        )
        if cursor.rowcount != 1:
            raise ConflictError("种质资源状态版本冲突")
        self._event(accession_id, "status_changed", data["actor"], current, target, {"reason": reason})
        if target in {"restricted", "retired"}:
            from app.germplasm.reservations import ReservationService

            ReservationService(self.connection, self.clock).release_accession_for_restriction(
                accession_id, f"资源状态转为 {target}：{reason}"
            )
        self._outbox(
            f"accession-status-{accession_id}-{int(before['version']) + 1}", "accession.status.changed",
            "accession", accession_id, {"from": current, "to": target, "reason": reason}, timestamp,
        )
        return self.repository.accession_detail(accession_id)

    def restrictions_for(self, accession_id: int) -> dict[str, Any]:
        accession = self.repository.require_accession(accession_id)
        source_rules: dict[str, Any] = {}
        if accession.get("source_id"):
            source_rules = self.repository.require_source(int(accession["source_id"])).get("restrictions", {})
        passport_rules = accession.get("passport", {}).get("restrictions", {})
        return {
            "accession_id": accession_id,
            "status": accession["status"],
            "source": source_rules,
            "passport": passport_rules,
            "distribution_allowed": (
                accession["status"] == "accepted"
                and not source_rules.get("no_distribution", False)
                and not passport_rules.get("no_distribution", False)
            ),
        }

    def _event(
        self,
        accession_id: int,
        event_type: str,
        actor: str,
        from_status: str | None,
        to_status: str | None,
        detail: dict[str, Any],
    ) -> None:
        self.connection.execute(
            "INSERT INTO accession_events(accession_id,event_type,actor,from_status,to_status,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                accession_id, event_type, actor, from_status, to_status,
                json.dumps(detail, ensure_ascii=False, sort_keys=True), to_storage(self.clock.now()),
            ),
        )

    def _outbox(
        self,
        event_key: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: int,
        payload: dict[str, Any],
        timestamp: str,
    ) -> None:
        self.connection.execute(
            "INSERT INTO outbox_events(event_key,event_type,aggregate_type,aggregate_id,payload_json,available_at,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (event_key, event_type, aggregate_type, str(aggregate_id), json.dumps(payload, ensure_ascii=False), timestamp, timestamp),
        )

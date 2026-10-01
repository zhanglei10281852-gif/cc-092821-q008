from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.database import database_path, get_connection, init_db, transaction
from app.germplasm.service import GermplasmService


def init_command() -> dict:
    init_db()
    return {"database": str(database_path()), "initialized": True}


def check_command() -> dict:
    init_db()
    connection = get_connection()
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    required = {
        "accessions", "seed_lots", "storage_locations", "viability_tests",
        "retest_schedules", "quality_alerts", "outbox_events",
        "lot_reservations", "reservation_events", "outbound_records",
    }
    actual = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    return {
        "database": str(database_path()),
        "integrity": integrity,
        "foreign_key_errors": len(foreign_keys),
        "required_tables_present": required.issubset(actual),
        "table_count": len(actual),
    }


def smoke_command() -> dict:
    from app.main import app

    init_db()
    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
    if root.status_code != 200 or health.status_code != 200:
        raise RuntimeError("HTTP 冒烟检查失败")
    return {"root": root.json(), "health": health.json(), "status": "ok"}


def demo_command() -> dict:
    init_db()
    service = GermplasmService(get_connection())
    suffix = get_connection().execute("SELECT COUNT(*) FROM accessions").fetchone()[0] + 1
    with transaction(immediate=True):
        source = service.accessions.create_source({
            "source_code": f"DEMO-{suffix:04d}",
            "provider_name": "示范采集队",
            "country_code": "CN",
            "locality": "示范农场",
            "collected_on": "2026-09-01",
            "permit_reference": None,
            "restrictions": {},
        })
        accession = service.accessions.create_accession({
            "accession_no": f"ACC-DEMO-{suffix:04d}",
            "scientific_name": "Oryza sativa",
            "crop_name": "水稻",
            "cultivar_name": "示范材料",
            "source_id": source["id"],
            "acquisition_type": "采集",
            "received_on": "2026-09-20",
            "passport": {"origin": "示范农场"},
            "created_by": "cli",
        })
        accepted = service.accessions.transition(accession["id"], {
            "target_status": "accepted", "reason": "资料齐全", "expected_version": 1, "actor": "cli"
        })
        location = service.inventory.create_location({
            "location_code": f"DEMO-COLD-{suffix:04d}", "facility": "示范库", "room": "低温间",
            "rack": "R1", "shelf": "S1", "capacity_grams": 5000,
            "temperature_c": -18, "humidity_percent": 30,
        })
        lot = service.inventory.create_lot({
            "lot_no": f"LOT-DEMO-{suffix:04d}", "accession_id": accepted["id"], "parent_lot_id": None,
            "harvest_year": 2026, "initial_weight_grams": 500, "moisture_percent": 7.2,
            "treatment": "清选干燥", "sealed_on": "2026-09-21", "created_by": "cli",
        })
        placement = service.inventory.place_lot({
            "lot_id": lot["id"], "location_id": location["id"], "weight_grams": 500,
            "container_code": f"BOX-DEMO-{suffix:04d}", "idempotency_key": f"demo-place-{suffix:08d}", "actor": "cli",
        })
    return {
        "accession_no": accepted["accession_no"],
        "lot_no": lot["lot_no"],
        "location": location["location_code"],
        "placement_id": placement["placement"]["id"],
        "dashboard": service.dashboard(),
    }


def export_command(path: str) -> dict:
    init_db()
    service = GermplasmService(get_connection())
    items, total = service.repository.list_accessions(status=None, crop=None, limit=10_000, offset=0)
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(target), "count": total}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="种质资源库运维命令")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行 HTTP 冒烟检查")
    subparsers.add_parser("demo", help="写入一组示范入库数据")
    export = subparsers.add_parser("export-accessions", help="导出资源档案")
    export.add_argument("path")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "init-db":
            result = init_command()
        elif args.command == "check-db":
            result = check_command()
        elif args.command == "smoke":
            result = smoke_command()
        elif args.command == "demo":
            result = demo_command()
        else:
            result = export_command(args.path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (sqlite3.Error, RuntimeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.core.clock import to_storage, utc_now

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "germplasm.db"
_local = threading.local()

SCHEMA = r'''
CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    manager TEXT NOT NULL,
    phone TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    department_id INTEGER REFERENCES departments(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled','locked')),
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    password_changed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    is_system INTEGER NOT NULL DEFAULT 0 CHECK(is_system IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS permissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    resource TEXT NOT NULL,
    action TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS role_permissions (
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_id INTEGER NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
    granted_at TEXT NOT NULL,
    PRIMARY KEY(role_id,permission_id)
);
CREATE TABLE IF NOT EXISTS user_roles (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    assigned_by INTEGER REFERENCES users(id),
    assigned_at TEXT NOT NULL,
    PRIMARY KEY(user_id,role_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_digest TEXT NOT NULL UNIQUE,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    client_label TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER REFERENCES users(id),
    actor_name TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('success','denied','failure')),
    before_json TEXT,
    after_json TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    correlation_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_resource ON audit_events(resource_type,resource_id);
CREATE TABLE IF NOT EXISTS idempotency_records (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope,idempotency_key)
);
CREATE TABLE IF NOT EXISTS department_memberships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    department_id INTEGER NOT NULL REFERENCES departments(id),
    title TEXT NOT NULL DEFAULT '',
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0,1)),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(user_id,department_id,starts_at)
);
CREATE TABLE IF NOT EXISTS background_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    deduplication_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','completed','failed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    locked_at TEXT,
    locked_by TEXT,
    result_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON background_jobs(status,available_at);

CREATE TABLE IF NOT EXISTS collection_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_code TEXT NOT NULL UNIQUE,
    provider_name TEXT NOT NULL,
    country_code TEXT NOT NULL,
    locality TEXT NOT NULL DEFAULT '',
    collected_on TEXT,
    permit_reference TEXT,
    restrictions_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    accession_no TEXT NOT NULL UNIQUE,
    scientific_name TEXT NOT NULL,
    crop_name TEXT NOT NULL,
    cultivar_name TEXT NOT NULL DEFAULT '',
    source_id INTEGER REFERENCES collection_sources(id),
    acquisition_type TEXT NOT NULL CHECK(acquisition_type IN ('采集','引进','交换','捐赠','育种')),
    received_on TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','quarantine','accepted','restricted','retired')),
    quarantine_reason TEXT NOT NULL DEFAULT '',
    passport_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_accessions_crop ON accessions(crop_name,status);
CREATE INDEX IF NOT EXISTS idx_accessions_source ON accessions(source_id);
CREATE TABLE IF NOT EXISTS accession_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    accession_id INTEGER NOT NULL REFERENCES accessions(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_accession_events ON accession_events(accession_id,id);

CREATE TABLE IF NOT EXISTS storage_locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_code TEXT NOT NULL UNIQUE,
    facility TEXT NOT NULL,
    room TEXT NOT NULL,
    rack TEXT NOT NULL,
    shelf TEXT NOT NULL,
    capacity_grams REAL NOT NULL CHECK(capacity_grams > 0),
    temperature_c REAL NOT NULL,
    humidity_percent REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','maintenance','closed')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seed_lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_no TEXT NOT NULL UNIQUE,
    accession_id INTEGER NOT NULL REFERENCES accessions(id) ON DELETE RESTRICT,
    parent_lot_id INTEGER REFERENCES seed_lots(id),
    harvest_year INTEGER NOT NULL CHECK(harvest_year BETWEEN 1800 AND 2200),
    initial_weight_grams REAL NOT NULL CHECK(initial_weight_grams > 0),
    available_weight_grams REAL NOT NULL CHECK(available_weight_grams >= 0),
    moisture_percent REAL CHECK(moisture_percent >= 0 AND moisture_percent <= 100),
    treatment TEXT NOT NULL DEFAULT '',
    sealed_on TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','stored','held','depleted','disposed')),
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lots_accession ON seed_lots(accession_id,status);
CREATE TABLE IF NOT EXISTS lot_placements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER NOT NULL REFERENCES seed_lots(id) ON DELETE CASCADE,
    location_id INTEGER NOT NULL REFERENCES storage_locations(id) ON DELETE RESTRICT,
    weight_grams REAL NOT NULL CHECK(weight_grams > 0),
    container_code TEXT NOT NULL,
    placed_at TEXT NOT NULL,
    removed_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(container_code,placed_at)
);
CREATE INDEX IF NOT EXISTS idx_placements_active ON lot_placements(location_id,removed_at);
CREATE TABLE IF NOT EXISTS lot_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER NOT NULL REFERENCES seed_lots(id) ON DELETE CASCADE,
    placement_id INTEGER REFERENCES lot_placements(id),
    movement_type TEXT NOT NULL CHECK(movement_type IN ('入库','移库','取样','领用','归还','报废','盘点调整')),
    quantity_grams REAL NOT NULL,
    from_location_id INTEGER REFERENCES storage_locations(id),
    to_location_id INTEGER REFERENCES storage_locations(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_movements_lot ON lot_movements(lot_id,id);
CREATE TABLE IF NOT EXISTS lot_holds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER NOT NULL REFERENCES seed_lots(id) ON DELETE CASCADE,
    hold_type TEXT NOT NULL CHECK(hold_type IN ('检疫','质量','权限','争议')),
    reason TEXT NOT NULL,
    imposed_by TEXT NOT NULL,
    imposed_at TEXT NOT NULL,
    released_by TEXT,
    released_at TEXT,
    release_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_holds_active ON lot_holds(lot_id,released_at);

CREATE TABLE IF NOT EXISTS viability_protocols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    protocol_code TEXT NOT NULL,
    version INTEGER NOT NULL,
    crop_name TEXT NOT NULL,
    sample_size INTEGER NOT NULL CHECK(sample_size > 0),
    replicate_count INTEGER NOT NULL CHECK(replicate_count > 0),
    temperature_c REAL NOT NULL,
    duration_days INTEGER NOT NULL CHECK(duration_days > 0),
    normal_seedling_rule TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(protocol_code,version)
);
CREATE TABLE IF NOT EXISTS viability_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    test_no TEXT NOT NULL UNIQUE,
    lot_id INTEGER NOT NULL REFERENCES seed_lots(id) ON DELETE RESTRICT,
    protocol_id INTEGER NOT NULL REFERENCES viability_protocols(id) ON DELETE RESTRICT,
    test_type TEXT NOT NULL CHECK(test_type IN ('入库初检','周期复检','异常复核')),
    sampled_grams REAL NOT NULL CHECK(sampled_grams > 0),
    scheduled_for TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN ('scheduled','running','completed','invalidated','cancelled')),
    germination_percent REAL,
    vigor_index REAL,
    invalid_reason TEXT,
    requested_by TEXT NOT NULL,
    performed_by TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tests_due ON viability_tests(status,scheduled_for);
CREATE INDEX IF NOT EXISTS idx_tests_lot ON viability_tests(lot_id,created_at);
CREATE TABLE IF NOT EXISTS viability_counts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id INTEGER NOT NULL REFERENCES viability_tests(id) ON DELETE CASCADE,
    replicate_no INTEGER NOT NULL,
    seeds_tested INTEGER NOT NULL CHECK(seeds_tested > 0),
    normal_count INTEGER NOT NULL CHECK(normal_count >= 0),
    abnormal_count INTEGER NOT NULL CHECK(abnormal_count >= 0),
    dead_count INTEGER NOT NULL CHECK(dead_count >= 0),
    fresh_count INTEGER NOT NULL DEFAULT 0 CHECK(fresh_count >= 0),
    observation_day INTEGER NOT NULL CHECK(observation_day > 0),
    observed_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(test_id,replicate_no,observation_day)
);
CREATE TABLE IF NOT EXISTS retest_policies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crop_name TEXT NOT NULL,
    risk_level TEXT NOT NULL CHECK(risk_level IN ('low','medium','high')),
    interval_months INTEGER NOT NULL CHECK(interval_months > 0),
    warning_days INTEGER NOT NULL CHECK(warning_days >= 0),
    minimum_germination_percent REAL NOT NULL CHECK(minimum_germination_percent BETWEEN 0 AND 100),
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    version INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(crop_name,risk_level,version)
);
CREATE TABLE IF NOT EXISTS retest_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER NOT NULL REFERENCES seed_lots(id) ON DELETE CASCADE,
    source_test_id INTEGER REFERENCES viability_tests(id),
    policy_id INTEGER NOT NULL REFERENCES retest_policies(id) ON DELETE RESTRICT,
    due_on TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','notified','scheduled','superseded','waived')),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(lot_id,due_on)
);
CREATE INDEX IF NOT EXISTS idx_schedules_due ON retest_schedules(status,due_on);

CREATE TABLE IF NOT EXISTS environment_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL REFERENCES storage_locations(id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    temperature_c REAL NOT NULL,
    humidity_percent REAL NOT NULL,
    source_key TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE(location_id,source_key)
);
CREATE INDEX IF NOT EXISTS idx_readings_time ON environment_readings(location_id,observed_at);
CREATE TABLE IF NOT EXISTS quality_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_key TEXT NOT NULL UNIQUE,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('info','warning','critical')),
    lot_id INTEGER REFERENCES seed_lots(id),
    location_id INTEGER REFERENCES storage_locations(id),
    message TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','acknowledged','resolved','dismissed')),
    acknowledged_by TEXT,
    acknowledged_at TEXT,
    resolved_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_open ON quality_alerts(status,severity,created_at);

CREATE TABLE IF NOT EXISTS distribution_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_no TEXT NOT NULL UNIQUE,
    requester TEXT NOT NULL,
    purpose TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','submitted','approved','picking','rejected','fulfilled','cancelled')),
    requested_at TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    decision_reason TEXT,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS distribution_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES distribution_requests(id) ON DELETE CASCADE,
    accession_id INTEGER NOT NULL REFERENCES accessions(id) ON DELETE RESTRICT,
    quantity_grams REAL NOT NULL CHECK(quantity_grams > 0),
    allocated_lot_id INTEGER REFERENCES seed_lots(id),
    status TEXT NOT NULL DEFAULT 'requested' CHECK(status IN ('requested','allocated','fulfilled','unavailable')),
    UNIQUE(request_id,accession_id)
);
CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES distribution_requests(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK(status IN ('active','picking','fulfilled','released')),
    release_reason TEXT CHECK(release_reason IS NULL OR release_reason IN ('expired','cancelled','quality_hold','restriction','manual')),
    expires_at TEXT NOT NULL,
    allocation_rule TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    taken_over_by TEXT,
    taken_over_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    released_at TEXT,
    released_by TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reservations_request_open
ON reservations(request_id) WHERE status IN ('active','picking');
CREATE INDEX IF NOT EXISTS idx_reservations_expiry ON reservations(status,expires_at);
CREATE TABLE IF NOT EXISTS reservation_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER NOT NULL REFERENCES reservations(id) ON DELETE CASCADE,
    request_item_id INTEGER NOT NULL REFERENCES distribution_items(id) ON DELETE CASCADE,
    lot_id INTEGER NOT NULL REFERENCES seed_lots(id) ON DELETE RESTRICT,
    sequence_no INTEGER NOT NULL CHECK(sequence_no > 0),
    quantity_grams REAL NOT NULL CHECK(quantity_grams > 0),
    picked_grams REAL NOT NULL DEFAULT 0 CHECK(picked_grams >= 0),
    status TEXT NOT NULL CHECK(status IN ('active','picking','fulfilled','released')),
    release_reason TEXT,
    rule_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    picked_at TEXT,
    released_at TEXT,
    released_by TEXT,
    UNIQUE(reservation_id,request_item_id,sequence_no)
);
CREATE INDEX IF NOT EXISTS idx_rlines_lot ON reservation_lines(lot_id,status);
CREATE INDEX IF NOT EXISTS idx_rlines_item ON reservation_lines(request_item_id);
CREATE TRIGGER IF NOT EXISTS trg_rlines_no_oversell_insert
AFTER INSERT ON reservation_lines
WHEN NEW.status IN ('active','picking')
BEGIN
    SELECT CASE WHEN (
        SELECT COALESCE(SUM(quantity_grams - picked_grams),0) FROM reservation_lines
        WHERE lot_id=NEW.lot_id AND status IN ('active','picking')
    ) > (SELECT available_weight_grams FROM seed_lots WHERE id=NEW.lot_id) + 0.000001
    THEN RAISE(ABORT,'批次可预约重量不足，禁止超卖') END;
END;
CREATE TRIGGER IF NOT EXISTS trg_rlines_no_oversell_update
AFTER UPDATE OF quantity_grams,picked_grams,status,lot_id ON reservation_lines
WHEN NEW.status IN ('active','picking')
BEGIN
    SELECT CASE WHEN (
        SELECT COALESCE(SUM(quantity_grams - picked_grams),0) FROM reservation_lines
        WHERE lot_id=NEW.lot_id AND status IN ('active','picking')
    ) > (SELECT available_weight_grams FROM seed_lots WHERE id=NEW.lot_id) + 0.000001
    THEN RAISE(ABORT,'批次可预约重量不足，禁止超卖') END;
END;
CREATE TABLE IF NOT EXISTS reservation_fulfillments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_line_id INTEGER NOT NULL REFERENCES reservation_lines(id) ON DELETE RESTRICT,
    lot_id INTEGER NOT NULL REFERENCES seed_lots(id) ON DELETE RESTRICT,
    movement_id INTEGER REFERENCES lot_movements(id),
    kind TEXT NOT NULL CHECK(kind IN ('pick','return')),
    quantity_grams REAL NOT NULL CHECK(quantity_grams > 0),
    shipment_no TEXT,
    consignee TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rf_line ON reservation_fulfillments(reservation_line_id);
CREATE INDEX IF NOT EXISTS idx_rf_lot ON reservation_fulfillments(lot_id);
CREATE TRIGGER IF NOT EXISTS trg_rf_bounds
AFTER INSERT ON reservation_fulfillments
BEGIN
    SELECT CASE WHEN (
        SELECT COALESCE(SUM(CASE WHEN kind='pick' THEN quantity_grams ELSE -quantity_grams END),0)
        FROM reservation_fulfillments WHERE reservation_line_id=NEW.reservation_line_id
    ) < -0.000001
    THEN RAISE(ABORT,'回退重量不能超过该预约明细已拣货重量') END;
    SELECT CASE WHEN (
        SELECT COALESCE(SUM(CASE WHEN kind='pick' THEN quantity_grams ELSE -quantity_grams END),0)
        FROM reservation_fulfillments WHERE reservation_line_id=NEW.reservation_line_id
    ) > (SELECT quantity_grams FROM reservation_lines WHERE id=NEW.reservation_line_id) + 0.000001
    THEN RAISE(ABORT,'拣货重量不能超过预约锁定重量') END;
END;
CREATE TABLE IF NOT EXISTS reservation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER NOT NULL REFERENCES reservations(id) ON DELETE CASCADE,
    reservation_line_id INTEGER REFERENCES reservation_lines(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revents_reservation ON reservation_events(reservation_id,id);
CREATE TABLE IF NOT EXISTS outbox_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processing','published','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    locked_by TEXT,
    locked_at TEXT,
    published_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox_events(status,available_at,id);
'''

PERMISSIONS = [
    ("users.read", "查看用户", "users", "read"),
    ("users.write", "维护用户", "users", "write"),
    ("roles.read", "查看角色", "roles", "read"),
    ("roles.write", "维护角色", "roles", "write"),
    ("departments.read", "查看部门", "departments", "read"),
    ("departments.write", "维护部门", "departments", "write"),
    ("audit.read", "查看审计", "audit", "read"),
    ("jobs.run", "执行后台任务", "jobs", "run"),
    ("accessions.read", "查看种质材料", "accessions", "read"),
    ("accessions.write", "维护种质材料", "accessions", "write"),
    ("inventory.read", "查看库存", "inventory", "read"),
    ("inventory.write", "维护库存", "inventory", "write"),
    ("viability.read", "查看活力检测", "viability", "read"),
    ("viability.write", "执行活力检测", "viability", "write"),
    ("quality.review", "复核质量结果", "quality", "review"),
    ("distribution.approve", "审批种质发放", "distribution", "approve"),
    ("distribution.pick", "拣货出库", "distribution", "pick"),
    ("distribution.manage", "接管与回退预约", "distribution", "manage"),
]


def database_path() -> Path:
    raw = os.getenv("GERMPLASM_DATABASE_PATH", str(DEFAULT_DB_PATH))
    return Path(raw).expanduser().resolve()


def _create_connection() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=15000")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def get_connection() -> sqlite3.Connection:
    connection = getattr(_local, "connection", None)
    if connection is None:
        connection = _create_connection()
        _local.connection = connection
    return connection


def close_connection() -> None:
    connection = getattr(_local, "connection", None)
    if connection is not None:
        connection.close()
        _local.connection = None


@contextmanager
def transaction(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    connection = get_connection()
    if connection.in_transaction:
        marker = f"nested_{id(object())}"
        connection.execute(f"SAVEPOINT {marker}")
        try:
            yield connection
            connection.execute(f"RELEASE SAVEPOINT {marker}")
        except Exception:
            connection.execute(f"ROLLBACK TO SAVEPOINT {marker}")
            connection.execute(f"RELEASE SAVEPOINT {marker}")
            raise
        return
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def init_db() -> None:
    timestamp = to_storage(utc_now())
    with transaction(immediate=True) as connection:
        connection.executescript(SCHEMA)
        for code, name, resource, action in PERMISSIONS:
            connection.execute(
                "INSERT OR IGNORE INTO permissions(code,name,resource,action) VALUES(?,?,?,?)",
                (code, name, resource, action),
            )
        roles = [
            ("administrator", "系统管理员", "拥有全部系统权限"),
            ("registrar", "入库登记员", "登记材料并维护库存"),
            ("technician", "活力检测员", "执行取样与活力检测"),
            ("curator", "资源审核员", "复核种质质量与发放"),
            ("auditor", "审计查看员", "只读查看业务和审计记录"),
        ]
        for code, name, description in roles:
            connection.execute(
                "INSERT OR IGNORE INTO roles(code,name,description,is_system,created_at,updated_at) VALUES(?,?,?,1,?,?)",
                (code, name, description, timestamp, timestamp),
            )
        administrator = connection.execute("SELECT id FROM roles WHERE code='administrator'").fetchone()[0]
        connection.execute(
            "INSERT OR IGNORE INTO role_permissions(role_id,permission_id,granted_at) SELECT ?,id,? FROM permissions",
            (administrator, timestamp),
        )
        role_permissions = {
            "registrar": ["accessions.read", "accessions.write", "inventory.read", "inventory.write", "distribution.pick"],
            "technician": ["accessions.read", "inventory.read", "viability.read", "viability.write"],
            "curator": ["accessions.read", "inventory.read", "viability.read", "quality.review", "distribution.approve", "distribution.manage"],
            "auditor": ["accessions.read", "inventory.read", "viability.read", "audit.read"],
        }
        for role_code, codes in role_permissions.items():
            role_id = connection.execute("SELECT id FROM roles WHERE code=?", (role_code,)).fetchone()[0]
            for permission_code in codes:
                permission_id = connection.execute("SELECT id FROM permissions WHERE code=?", (permission_code,)).fetchone()[0]
                connection.execute(
                    "INSERT OR IGNORE INTO role_permissions(role_id,permission_id,granted_at) VALUES(?,?,?)",
                    (role_id, permission_id, timestamp),
                )


def migrate_db() -> None:
    init_db()

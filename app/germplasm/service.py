from __future__ import annotations

import sqlite3

from app.core.clock import Clock
from app.germplasm.accessions import AccessionService
from app.germplasm.inventory import InventoryService
from app.germplasm.quality import DistributionService, QualityService
from app.germplasm.repository import GermplasmRepository
from app.germplasm.reservations import ReservationService
from app.germplasm.viability import ViabilityService


class GermplasmService:
    """把共享事务连接交给各业务边界，便于 API 与 CLI 原子调用。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.repository = GermplasmRepository(connection)
        self.accessions = AccessionService(connection, clock)
        self.inventory = InventoryService(connection, clock)
        self.viability = ViabilityService(connection, clock)
        self.quality = QualityService(connection, clock)
        self.distribution = DistributionService(connection, clock)
        self.reservations = ReservationService(connection, clock)

    def dashboard(self) -> dict:
        return {
            "accessions": self.repository.count_table("accessions"),
            "seed_lots": self.repository.count_table("seed_lots"),
            "storage_locations": self.repository.count_table("storage_locations"),
            "viability_tests": self.repository.count_table("viability_tests"),
            "retest_schedules": self.repository.count_table("retest_schedules"),
            "quality_alerts": self.repository.count_table("quality_alerts"),
            "distribution_requests": self.repository.count_table("distribution_requests"),
            "active_reservations": int(self.connection.execute(
                "SELECT COUNT(*) FROM lot_reservations WHERE status IN ('active','picking')"
            ).fetchone()[0]),
        }

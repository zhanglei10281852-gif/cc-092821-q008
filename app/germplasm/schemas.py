from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class AcquisitionType(str, Enum):
    collection = "采集"
    introduction = "引进"
    exchange = "交换"
    donation = "捐赠"
    breeding = "育种"


class AccessionStatus(str, Enum):
    draft = "draft"
    quarantine = "quarantine"
    accepted = "accepted"
    restricted = "restricted"
    retired = "retired"


class LotStatus(str, Enum):
    pending = "pending"
    stored = "stored"
    held = "held"
    depleted = "depleted"
    disposed = "disposed"


class TestType(str, Enum):
    intake = "入库初检"
    periodic = "周期复检"
    review = "异常复核"


class SourceCreate(BaseModel):
    source_code: str = Field(min_length=2, max_length=40)
    provider_name: str = Field(min_length=1, max_length=200)
    country_code: str = Field(min_length=2, max_length=2)
    locality: str = Field(default="", max_length=300)
    collected_on: date | None = None
    permit_reference: str | None = Field(default=None, max_length=100)
    restrictions: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_code")
    @classmethod
    def normalize_source_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized.replace("-", "").replace("_", "").isalnum():
            raise ValueError("来源编码只能包含字母、数字、连字符和下划线")
        return normalized

    @field_validator("country_code")
    @classmethod
    def normalize_country(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized.isalpha():
            raise ValueError("国家代码必须为两个字母")
        return normalized


class AccessionCreate(BaseModel):
    accession_no: str = Field(min_length=3, max_length=50)
    scientific_name: str = Field(min_length=2, max_length=200)
    crop_name: str = Field(min_length=1, max_length=100)
    cultivar_name: str = Field(default="", max_length=150)
    source_id: int | None = Field(default=None, gt=0)
    acquisition_type: AcquisitionType
    received_on: date
    passport: dict[str, Any] = Field(default_factory=dict)
    created_by: str = Field(min_length=1, max_length=100)

    @field_validator("accession_no")
    @classmethod
    def normalize_accession_no(cls, value: str) -> str:
        normalized = value.strip().upper()
        if " " in normalized:
            raise ValueError("资源编号不能包含空格")
        return normalized

    @field_validator("scientific_name", "crop_name", "cultivar_name", "created_by")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class AccessionPatch(BaseModel):
    scientific_name: str | None = Field(default=None, min_length=2, max_length=200)
    crop_name: str | None = Field(default=None, min_length=1, max_length=100)
    cultivar_name: str | None = Field(default=None, max_length=150)
    source_id: int | None = Field(default=None, gt=0)
    passport: dict[str, Any] | None = None
    expected_version: int = Field(gt=0)
    actor: str = Field(min_length=1, max_length=100)


class AccessionTransition(BaseModel):
    target_status: AccessionStatus
    reason: str = Field(default="", max_length=500)
    expected_version: int = Field(gt=0)
    actor: str = Field(min_length=1, max_length=100)


class LocationCreate(BaseModel):
    location_code: str = Field(min_length=2, max_length=60)
    facility: str = Field(min_length=1, max_length=100)
    room: str = Field(min_length=1, max_length=100)
    rack: str = Field(min_length=1, max_length=60)
    shelf: str = Field(min_length=1, max_length=60)
    capacity_grams: float = Field(gt=0, le=10_000_000)
    temperature_c: float = Field(ge=-196, le=50)
    humidity_percent: float = Field(ge=0, le=100)

    @field_validator("location_code")
    @classmethod
    def normalize_location_code(cls, value: str) -> str:
        return value.strip().upper()


class LotCreate(BaseModel):
    lot_no: str = Field(min_length=3, max_length=60)
    accession_id: int = Field(gt=0)
    parent_lot_id: int | None = Field(default=None, gt=0)
    harvest_year: int = Field(ge=1800, le=2200)
    initial_weight_grams: float = Field(gt=0, le=10_000_000)
    moisture_percent: float | None = Field(default=None, ge=0, le=100)
    treatment: str = Field(default="", max_length=500)
    sealed_on: date | None = None
    created_by: str = Field(min_length=1, max_length=100)

    @field_validator("lot_no")
    @classmethod
    def normalize_lot_no(cls, value: str) -> str:
        return value.strip().upper()


class PlacementCreate(BaseModel):
    lot_id: int = Field(gt=0)
    location_id: int = Field(gt=0)
    weight_grams: float = Field(gt=0)
    container_code: str = Field(min_length=2, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=100)
    actor: str = Field(min_length=1, max_length=100)

    @field_validator("container_code")
    @classmethod
    def normalize_container_code(cls, value: str) -> str:
        return value.strip().upper()


class MovePlacement(BaseModel):
    target_location_id: int = Field(gt=0)
    expected_version: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=100)
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=2, max_length=300)


class WithdrawalCreate(BaseModel):
    lot_id: int = Field(gt=0)
    quantity_grams: float = Field(gt=0)
    movement_type: str = Field(pattern="^(取样|领用|报废)$")
    idempotency_key: str = Field(min_length=8, max_length=100)
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=2, max_length=300)


class HoldCreate(BaseModel):
    lot_id: int = Field(gt=0)
    hold_type: str = Field(pattern="^(检疫|质量|权限|争议)$")
    reason: str = Field(min_length=2, max_length=500)
    actor: str = Field(min_length=1, max_length=100)


class HoldRelease(BaseModel):
    reason: str = Field(min_length=2, max_length=500)
    actor: str = Field(min_length=1, max_length=100)


class ProtocolCreate(BaseModel):
    protocol_code: str = Field(min_length=2, max_length=40)
    crop_name: str = Field(min_length=1, max_length=100)
    sample_size: int = Field(gt=0, le=100_000)
    replicate_count: int = Field(gt=0, le=100)
    temperature_c: float = Field(ge=-20, le=60)
    duration_days: int = Field(gt=0, le=365)
    normal_seedling_rule: str = Field(min_length=5, max_length=1000)
    created_by: str = Field(min_length=1, max_length=100)

    @field_validator("protocol_code")
    @classmethod
    def normalize_protocol_code(cls, value: str) -> str:
        return value.strip().upper()


class TestCreate(BaseModel):
    test_no: str = Field(min_length=3, max_length=60)
    lot_id: int = Field(gt=0)
    protocol_id: int = Field(gt=0)
    test_type: TestType
    sampled_grams: float = Field(gt=0)
    scheduled_for: date
    requested_by: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=100)

    @field_validator("test_no")
    @classmethod
    def normalize_test_no(cls, value: str) -> str:
        return value.strip().upper()


class TestStart(BaseModel):
    performed_by: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(gt=0)


class CountCreate(BaseModel):
    replicate_no: int = Field(gt=0, le=100)
    seeds_tested: int = Field(gt=0, le=100_000)
    normal_count: int = Field(ge=0)
    abnormal_count: int = Field(ge=0)
    dead_count: int = Field(ge=0)
    fresh_count: int = Field(default=0, ge=0)
    observation_day: int = Field(gt=0, le=365)
    observed_by: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_total(self) -> "CountCreate":
        actual = self.normal_count + self.abnormal_count + self.dead_count + self.fresh_count
        if actual != self.seeds_tested:
            raise ValueError("正常、异常、死亡和新鲜未发芽计数之和必须等于检测粒数")
        return self


class TestComplete(BaseModel):
    expected_version: int = Field(gt=0)
    performed_by: str = Field(min_length=1, max_length=100)


class TestInvalidate(BaseModel):
    expected_version: int = Field(gt=0)
    reason: str = Field(min_length=3, max_length=500)
    actor: str = Field(min_length=1, max_length=100)


class PolicyCreate(BaseModel):
    crop_name: str = Field(min_length=1, max_length=100)
    risk_level: str = Field(pattern="^(low|medium|high)$")
    interval_months: int = Field(gt=0, le=240)
    warning_days: int = Field(ge=0, le=365)
    minimum_germination_percent: float = Field(ge=0, le=100)
    effective_from: date
    effective_to: date | None = None
    created_by: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_period(self) -> "PolicyCreate":
        if self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("策略失效日期不能早于生效日期")
        return self


class ReadingCreate(BaseModel):
    location_id: int = Field(gt=0)
    observed_at: datetime
    temperature_c: float = Field(ge=-196, le=80)
    humidity_percent: float = Field(ge=0, le=100)
    source_key: str = Field(min_length=3, max_length=100)


class AlertDecision(BaseModel):
    action: str = Field(pattern="^(acknowledge|resolve|dismiss)$")
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(default="", max_length=500)


class DistributionCreate(BaseModel):
    request_no: str = Field(min_length=3, max_length=60)
    requester: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=3, max_length=1000)
    items: list[dict[str, Any]] = Field(min_length=1, max_length=200)

    @field_validator("request_no")
    @classmethod
    def normalize_request_no(cls, value: str) -> str:
        return value.strip().upper()


class DistributionDecision(BaseModel):
    approve: bool
    expected_version: int = Field(gt=0)
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(default="", max_length=500)
    reservation_days: int = Field(default=7, ge=1, le=90)


class DistributionCancel(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=2, max_length=500)


class ReservationActor(BaseModel):
    actor: str = Field(min_length=1, max_length=100)


class ReservationRollback(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=2, max_length=500)


class ReservationPick(BaseModel):
    quantity_grams: float = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=100)
    actor: str = Field(min_length=1, max_length=100)
    shipment_no: str | None = Field(default=None, max_length=100)
    consignee: str = Field(default="", max_length=200)
    reason: str = Field(default="", max_length=500)


class ReservationReturn(BaseModel):
    quantity_grams: float = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=100)
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=2, max_length=500)


class Page(BaseModel):
    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int

"""批次放行平台在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OperationReceipt:
    """描述一次幂等写入的稳定结果，并附带关联资源编号。"""

    request_id: str
    resource_type: str
    resource_id: str
    replayed: bool
    related_ids: list[str]


@dataclass(frozen=True)
class BrandStandard:
    """品牌标准的一个不可变版本，放行决定会钉住具体版本。"""

    standard_id: str
    version: str
    brand: str
    spec: dict[str, Any]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class Lot:
    """原料或包装批号。"""

    lot_id: str
    site_id: str
    lot_kind: str
    name: str
    supplier: str | None
    payload: dict[str, Any]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class Calibration:
    """设备校准记录，有效期必须覆盖生产时段。"""

    calibration_id: str
    site_id: str
    equipment_id: str
    calibrated_at: str
    valid_from: str
    valid_until: str
    status: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class Batch:
    """生产批次及其当前粗粒度状态。"""

    batch_id: str
    site_id: str
    brand: str
    standard_id: str
    standard_version: str
    production_start: str
    production_end: str
    quantity: float
    unit: str
    status: str
    import_key: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class LabResult:
    """实验室结果，按批次钉住的品牌标准版本判定是否合格。"""

    result_id: str
    batch_id: str
    sample_code: str
    sampled_at: str
    tests: dict[str, Any]
    conforms: bool
    evaluation: dict[str, Any]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class LineageLink:
    """批次谱系边：拆分、合并或返工。"""

    link_id: str
    parent_batch_id: str
    child_batch_id: str
    relation: str
    quantity: float | None
    detail: dict[str, Any]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class Deviation:
    """偏差，范围可以只覆盖部分包装或销售区域。"""

    deviation_id: str
    batch_id: str
    scope: dict[str, Any]
    description: str
    severity: str
    status: str
    opened_by: str
    opened_at: str
    disposition_summary: str | None
    disposition_evidence: list[str]
    dispositioned_by: str | None
    dispositioned_at: str | None
    closed_by: str | None
    closed_at: str | None


@dataclass(frozen=True)
class Restriction:
    """隔离/冻结限制，解除需另一名授权者引用处置证据。"""

    restriction_id: str
    batch_id: str
    deviation_id: str | None
    scope: dict[str, Any]
    reason: str
    status: str
    created_by: str
    created_at: str
    released_by: str | None
    released_at: str | None
    release_note: str | None
    release_evidence: list[str]


@dataclass(frozen=True)
class BatchDecision:
    """追加写入、永不修改的放行/冻结/召回决定。"""

    decision_id: str
    batch_id: str
    decision: str
    scope: dict[str, Any]
    standard_id: str
    standard_version: str
    evidence: dict[str, Any]
    rationale: str
    decided_by: str
    decided_at: str
    shipment_id: str | None


@dataclass(frozen=True)
class ReviewTask:
    """未结复核任务，持久化后可在服务重启后继续。"""

    task_id: str
    batch_id: str
    kind: str
    deviation_id: str | None
    status: str
    note: str | None
    opened_by: str
    opened_at: str
    completed_by: str | None
    completed_at: str | None
    decision_id: str | None


@dataclass(frozen=True)
class Shipment:
    """出库记录；已出库的放行决定视为历史锁定。"""

    shipment_id: str
    batch_id: str
    decision_id: str
    quantity: float
    scope: dict[str, Any]
    shipped_at: str
    created_by: str
    created_at: str

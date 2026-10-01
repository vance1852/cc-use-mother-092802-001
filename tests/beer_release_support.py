"""为放行平台测试准备统一的组织/人员/场所/标准/批号/校准环境。"""

from __future__ import annotations

from datetime import datetime, timezone

from beverage_ops_foundation.clock import FixedClock

from beer_release.service import BatchReleaseService
from beer_release.storage import ReleaseDatabase

SPEC_V1 = {"limits": {"alcohol": {"min": 4.5, "max": 5.5}, "ph": {"min": 4.0, "max": 4.6}}}

WINDOW_START = "2026-10-01T00:00:00Z"
WINDOW_END = "2026-10-01T08:00:00Z"
CAL_FROM = "2026-01-01T00:00:00Z"
CAL_TO = "2027-01-01T00:00:00Z"


def build_service(path: str = ":memory:", fixed_time: datetime | None = None) -> BatchReleaseService:
    """构造一个已完成建档的服务：两厂、各自的复核人/操作员与基础证据。"""

    clock = FixedClock(fixed_time or datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc))
    service = BatchReleaseService(ReleaseDatabase(path), clock)
    service.foundation.register_organization(request_id="org", actor_id="bootstrap",
                                              organization_id="o1", name="高端啤酒品质团队")
    service.foundation.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin",
                                      display_name="管理员", role="admin", organization_id="o1")
    for actor_id, name, role in (
            ("rv-gz", "广州复核人", "reviewer"),
            ("rv-js", "嘉善复核人", "reviewer"),
            ("op-gz", "广州操作员", "operator"),
            ("op-js", "嘉善操作员", "operator")):
        service.foundation.register_actor(request_id=f"a-{actor_id}", actor_id="admin",
                                          new_actor_id=actor_id, display_name=name, role=role,
                                          organization_id="o1")
    for site_id, name in (("gz", "广州工厂"), ("js", "嘉善工厂")):
        service.foundation.register_site(request_id=f"s-{site_id}", actor_id="admin",
                                         site_id=site_id, organization_id="o1", name=name,
                                         timezone_name="Asia/Shanghai")
    service.register_brand_standard(request_id="std", actor_id="rv-gz", standard_id="std",
                                    version="v1", brand="醇品", spec=SPEC_V1)
    return service


def seed_factory(service: BatchReleaseService, site: str) -> None:
    """登记某厂的原料/包装批号与一条覆盖生产窗口的校准。"""

    actor = f"op-{site}"
    service.register_lot(request_id=f"m-{site}", actor_id=actor, lot_id=f"malt-{site}", site_id=site,
                         lot_kind="material", name="麦芽", supplier="华北粮贸")
    service.register_lot(request_id=f"p-{site}", actor_id=actor, lot_id=f"can-{site}", site_id=site,
                         lot_kind="packaging", name="易拉罐", supplier="南方包材")
    service.register_calibration(request_id=f"c-{site}", actor_id=actor, calibration_id=f"filler-{site}",
                                 site_id=site, equipment_id="filler-1", calibrated_at=CAL_FROM,
                                 valid_from=CAL_FROM, valid_until=CAL_TO)


def import_ready_batch(service: BatchReleaseService, batch_id: str, *, site: str = "gz",
                       lab: dict[str, float] | None = None) -> str:
    """导入批次并登记合格实验室结果，返回批次号（尚未放行）。"""

    seed_factory(service, site)
    service.import_batch(request_id=f"imp-{batch_id}", actor_id=f"op-{site}", site_id=site,
                         import_key=batch_id.upper(), brand="醇品", standard_id="std", standard_version="v1",
                         production_start=WINDOW_START, production_end=WINDOW_END, quantity=1000.0,
                         unit="L", material_lot_ids=[f"malt-{site}"], packaging_lot_ids=[f"can-{site}"],
                         calibration_ids=[f"filler-{site}"], batch_id=batch_id)
    service.record_lab_result(request_id=f"lab-{batch_id}", actor_id=f"op-{site}", batch_id=batch_id,
                              sample_code=f"{batch_id}-S1", sampled_at="2026-10-01T09:00:00Z",
                              tests=lab or {"alcohol": 5.0, "ph": 4.3})
    return batch_id


def release_via_review(service: BatchReleaseService, batch_id: str, *, site: str = "gz",
                       opened_by: str | None = None, decided_by: str | None = None,
                       prefix: str | None = None, scope: dict | None = None) -> str:
    """走双人复核流程放行，返回放行决定号。"""

    prefix = prefix or batch_id
    opened_by = opened_by or f"op-{site}"
    decided_by = decided_by or f"rv-{site}"
    service.open_review(request_id=f"rev-{prefix}", actor_id=opened_by, batch_id=batch_id,
                        kind="release_review")
    task = service.list_reviews("open")[-1]["task_id"]
    receipt = service.complete_review(request_id=f"done-{prefix}", actor_id=decided_by, task_id=task,
                                      decision="release", rationale="证据齐全", scope=scope)
    return receipt.resource_id

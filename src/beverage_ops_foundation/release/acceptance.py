"""跨工厂批次放行平台的离线端到端验收。

在临时 SQLite 数据库中演练广州/嘉善/厦门三厂的完整放行链：
品牌标准版本、物料隔离与重复导入、设备校准、批次谱系（返工）、
实验室结果、三段复核、范围化偏差与四眼解除、放行/冻结/召回决定、
出库快照、服务重启后继续未结复核，以及全链 explain 还原。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..audit import verify_chain
from ..clock import FixedClock
from ..errors import ConflictError, PermissionDenied
from ..service import DomainService
from ..release.service import ReleaseService
from ..storage import Database

T0 = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)


def _bootstrap(database, clock):
    base = DomainService(database, clock)
    release = ReleaseService(database, clock)
    base.register_organization(request_id="req-org", actor_id="bootstrap",
                               organization_id="org-brew", name="高端啤酒事业群")
    base.register_actor(request_id="req-adm", actor_id="bootstrap", new_actor_id="adm1",
                        display_name="系统管理员", role="admin", organization_id="org-brew")
    actors = [
        ("op-gz", "广州产线操作员", "operator"),
        ("op-js", "嘉善产线操作员", "operator"),
        ("op-xm", "厦门产线操作员", "operator"),
        ("rv1", "实验室复核员", "reviewer"),
        ("ql1", "质量负责人甲", "quality_lead"),
        ("ql2", "质量负责人乙", "quality_lead"),
    ]
    for index, (actor_id, name, role) in enumerate(actors):
        base.register_actor(request_id=f"req-actor-{index}", actor_id="adm1",
                            new_actor_id=actor_id, display_name=name, role=role,
                            organization_id="org-brew")
    sites = [("site-gz", "广州工厂", "Asia/Shanghai"),
             ("site-js", "嘉善工厂", "Asia/Shanghai"),
             ("site-xm", "厦门工厂", "Asia/Shanghai")]
    for index, (site_id, name, tzname) in enumerate(sites):
        base.register_site(request_id=f"req-site-{index}", actor_id="adm1", site_id=site_id,
                           organization_id="org-brew", name=name, timezone_name=tzname)
    return base, release


def run() -> dict[str, object]:
    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "release_acceptance.sqlite3"
        database = Database(db_path)
        clock = FixedClock(T0)
        _, svc = _bootstrap(database, clock)

        # 1) 品牌标准 v1，随后登记 v2；版本必须顺序递增。
        standard_v1 = {
            "products": ["P-ALE"],
            "tests": {
                "alcohol": {"method_code": "M-ALC", "min": 4.5, "max": 5.5, "unit": "%vol"},
                "turbidity": {"method_code": "M-TUR", "max": 0.5, "unit": "EBC"},
            },
            "equipment": [
                {"site_id": "site-gz", "equipment_code": "filler-gz"},
                {"site_id": "site-js", "equipment_code": "filler-js"},
                {"site_id": "site-xm", "equipment_code": "filler-xm"},
            ],
        }
        svc.register_brand_standard(request_id="req-std-1", actor_id="ql1",
                                    standard_id="std-premium", version=1, payload=standard_v1)
        standard_v2 = dict(standard_v1, tests={
            "alcohol": {"method_code": "M-ALC", "min": 4.6, "max": 5.4, "unit": "%vol"},
            "turbidity": {"method_code": "M-TUR", "max": 0.45, "unit": "EBC"},
        })
        svc.register_brand_standard(request_id="req-std-2", actor_id="ql1",
                                    standard_id="std-premium", version=2, payload=standard_v2)
        try:
            svc.register_brand_standard(request_id="req-std-bad", actor_id="ql1",
                                        standard_id="std-premium", version=1,
                                        payload=standard_v1)
            checks["standard_version_no_backfill"] = False
        except ConflictError:
            checks["standard_version_no_backfill"] = True

        # 2) 产线、校准与物料批号。
        for site_id, line_id, name in [
                ("site-gz", "line-gz-1", "广州1号线"),
                ("site-js", "line-js-1", "嘉善1号线"),
                ("site-xm", "line-xm-1", "厦门1号线")]:
            svc.register_line(request_id=f"req-line-{line_id}", actor_id="adm1",
                              line_id=line_id, site_id=site_id, name=name)
        for site_id, equipment in [("site-gz", "filler-gz"), ("site-js", "filler-js"),
                                   ("site-xm", "filler-xm")]:
            svc.register_calibration(
                request_id=f"req-cal-{equipment}", actor_id="op-gz",
                calibration_id=f"cal-{equipment}", site_id=site_id, equipment_code=equipment,
                valid_from="2026-08-01T00:00:00Z", valid_until="2026-10-31T23:59:59Z")

        svc.register_material_lot(request_id="req-malt", actor_id="op-gz",
                                  material_lot_id="lot-malt-1", site_id="site-gz",
                                  kind="raw_material", material_code="MALT-PILSNER",
                                  attributes={"origin": "澳洲"}, quarantined=False)
        svc.register_material_lot(request_id="req-bottle-1", actor_id="op-gz",
                                  material_lot_id="lot-bottle-1", site_id="site-gz",
                                  kind="packaging", material_code="BTL-330",
                                  attributes={"supplier": "包装供应商甲"},
                                  quarantined=True, import_key="import-20260901")
        # 重复导入：即使导入方声称未隔离，也不得改变既有隔离状态。
        svc.register_material_lot(request_id="req-bottle-1-dup", actor_id="op-gz",
                                  material_lot_id="lot-bottle-1", site_id="site-gz",
                                  kind="packaging", material_code="BTL-330",
                                  attributes={"supplier": "包装供应商甲"},
                                  quarantined=False, import_key="import-20260902")
        checks["duplicate_import_keeps_quarantine"] = \
            svc.get_material_lot("lot-bottle-1")["quarantined"] is True
        # 质量负责人凭校准证据解除物料隔离。
        svc.set_material_quarantine(request_id="req-bottle-release", actor_id="ql1",
                                    material_lot_id="lot-bottle-1", quarantined=False,
                                    reason="供应商复检合格", evidence_ref="calibration:cal-filler-gz")
        checks["quarantine_lifted_by_lead"] = \
            svc.get_material_lot("lot-bottle-1")["quarantined"] is False

        # 3) 广州批次建档；复核未完成前不能放行。
        batch_args = dict(site_id="site-gz", line_id="line-gz-1", product_code="P-ALE",
                          standard_id="std-premium", standard_version=1,
                          production_start="2026-09-10T00:00:00Z",
                          production_end="2026-09-10T08:00:00Z",
                          package_codes=["BTL-330", "CAN-500"],
                          region_codes=["CN-SOUTH", "CN-EAST"], quantity=12000,
                          material_lot_ids=["lot-malt-1", "lot-bottle-1"])
        svc.register_batch(request_id="req-gz-b1", actor_id="op-gz", batch_id="GZ-B1",
                           relation="original", **batch_args)
        try:
            svc.create_decision(request_id="req-release-too-early", actor_id="ql1",
                                batch_id="GZ-B1", decision="release",
                                reason="尝试提前放行", evidence_ref="calibration:cal-filler-gz")
            checks["release_blocked_before_review"] = False
        except ConflictError as exc:
            checks["release_blocked_before_review"] = "review:production_open" in str(exc)

        # 4) 实验室结果；重复导入回放，且不能把同一检验改成相反结论。
        alcohol = svc.record_lab_result(request_id="req-lab-alc", actor_id="rv1",
                                        batch_id="GZ-B1", test_code="alcohol", outcome="pass",
                                        method_code="M-ALC", measured_value="4.8",
                                        tested_at="2026-09-10T09:00:00Z", tested_by="lab-gz",
                                        import_key="lab-import-1")
        alc_id = alcohol["resource_id"]
        replay = svc.record_lab_result(request_id="req-lab-alc-again", actor_id="rv1",
                                       batch_id="GZ-B1", test_code="alcohol", outcome="pass",
                                       method_code="M-ALC", measured_value="4.8",
                                       tested_at="2026-09-10T09:00:00Z", tested_by="lab-gz",
                                       import_key="lab-import-2")
        checks["lab_duplicate_import_replays"] = replay.get("replayed_existing") is True
        try:
            svc.record_lab_result(request_id="req-lab-alc-tamper", actor_id="rv1",
                                  batch_id="GZ-B1", test_code="alcohol", outcome="fail",
                                  method_code="M-ALC", measured_value="4.8",
                                  tested_at="2026-09-10T09:00:00Z", tested_by="lab-gz")
            checks["lab_result_not_mutable"] = False
        except Exception:
            checks["lab_result_not_mutable"] = True
        try:  # 实测值与结论矛盾必须拒绝。
            svc.record_lab_result(request_id="req-lab-turb-bad", actor_id="rv1",
                                  batch_id="GZ-B1", test_code="turbidity", outcome="pass",
                                  method_code="M-TUR", measured_value="0.9",
                                  tested_at="2026-09-10T09:30:00Z", tested_by="lab-gz")
            checks["lab_value_outcome_consistency"] = False
        except Exception:
            checks["lab_value_outcome_consistency"] = True
        svc.record_lab_result(request_id="req-lab-turb", actor_id="rv1", batch_id="GZ-B1",
                              test_code="turbidity", outcome="pass", method_code="M-TUR",
                              measured_value="0.3", tested_at="2026-09-10T09:30:00Z",
                              tested_by="lab-gz")

        # 5) 完成生产与实验室复核，质量负责人放行并出库。
        svc.complete_review(request_id="req-rev-prod-gz", actor_id="op-gz",
                            batch_id="GZ-B1", stage="production", notes="产线记录齐全")
        svc.complete_review(request_id="req-rev-lab-gz", actor_id="rv1",
                            batch_id="GZ-B1", stage="laboratory", notes="两项检验合格")
        svc.create_decision(request_id="req-dec-release-gz", actor_id="ql1",
                            batch_id="GZ-B1", decision="release",
                            reason="全部证据满足 std-premium v1",
                            evidence_ref=f"lab_result:{alc_id}")
        ship1 = svc.register_shipment(request_id="req-ship-1", actor_id="op-gz",
                                      shipment_id="SHIP-1", batch_id="GZ-B1",
                                      package_code="BTL-330", region_code="CN-SOUTH",
                                      quantity=2000, shipped_at="2026-09-12T08:00:00Z")
        checks["first_shipment_uses_release"] = ship1["resource_id"] == "SHIP-1"

        # 6) 华东区域偏差：四眼挂接/解除，期间出库必须被拦截。
        svc.raise_restriction(request_id="req-restr-east", actor_id="ql1", batch_id="GZ-B1",
                              scope_type="region", scope_values=["CN-EAST"],
                              reason="华东标签备案复核中", evidence_ref=f"lab_result:{alc_id}")
        try:
            svc.register_shipment(request_id="req-ship-blocked", actor_id="op-gz",
                                  shipment_id="SHIP-BLOCKED", batch_id="GZ-B1",
                                  package_code="CAN-500", region_code="CN-EAST",
                                  quantity=100, shipped_at="2026-09-12T10:00:00Z")
            checks["shipment_blocked_under_restriction"] = False
        except ConflictError:
            checks["shipment_blocked_under_restriction"] = True
        try:  # 挂接人本人不能解除。
            svc.lift_restriction(request_id="req-lift-self", actor_id="ql1",
                                 restriction_id=svc.list_restrictions("GZ-B1", True)[0]["restriction_id"],
                                 evidence_ref=f"lab_result:{alc_id}")
            checks["lift_requires_second_authorized_actor"] = False
        except PermissionDenied:
            checks["lift_requires_second_authorized_actor"] = True
        release_review = next(r for r in svc.list_reviews("GZ-B1") if r["stage"] == "release")
        svc.lift_restriction(request_id="req-lift-ql2", actor_id="ql2",
                             restriction_id=svc.list_restrictions("GZ-B1", True)[0]["restriction_id"],
                             evidence_ref="calibration:cal-filler-gz",
                             review_id=release_review["review_id"], notes="备案完成，准予解除")
        ship2 = svc.register_shipment(request_id="req-ship-2", actor_id="op-gz",
                                      shipment_id="SHIP-2", batch_id="GZ-B1",
                                      package_code="CAN-500", region_code="CN-EAST",
                                      quantity=300, shipped_at="2026-09-13T08:00:00Z")
        checks["shipment_after_lift"] = ship2["resource_id"] == "SHIP-2"

        # 7) 返工批次保留谱系，且必须重新检验、重新复核后才能放行。
        svc.register_material_lot(request_id="req-can", actor_id="op-gz",
                                  material_lot_id="lot-can-1", site_id="site-gz",
                                  kind="packaging", material_code="CAN-500",
                                  attributes={"supplier": "制罐厂乙"}, quarantined=False)
        svc.register_batch(request_id="req-gz-b2", actor_id="op-gz", batch_id="GZ-B2",
                           relation="rework", parent_batch_ids=["GZ-B1"], **{
                               **batch_args,
                               "material_lot_ids": ["lot-can-1"],
                               "production_start": "2026-09-14T00:00:00Z",
                               "production_end": "2026-09-14T06:00:00Z",
                               "package_codes": ["CAN-500"],
                               "region_codes": ["CN-SOUTH"],
                               "quantity": 4000})
        lineage = svc.lineage("GZ-B2")
        checks["rework_lineage_kept"] = [a["related_batch_id"] for a in lineage["ancestors"]] == ["GZ-B1"]
        checks["parent_lineage_sees_child"] = \
            [d["related_batch_id"] for d in svc.lineage("GZ-B1")["descendants"]] == ["GZ-B2"]
        try:
            svc.create_decision(request_id="req-release-b2-early", actor_id="ql1",
                                batch_id="GZ-B2", decision="release", reason="尝试跳过复检",
                                evidence_ref=f"lab_result:{alc_id}")
            checks["rework_requires_retest"] = False
        except ConflictError as exc:
            checks["rework_requires_retest"] = "lab_missing:alcohol" in str(exc)
        b2_alc = svc.record_lab_result(request_id="req-lab-b2-alc", actor_id="rv1",
                                       batch_id="GZ-B2", test_code="alcohol", outcome="pass",
                                       method_code="M-ALC", measured_value="4.9",
                                       tested_at="2026-09-14T08:00:00Z", tested_by="lab-gz")
        svc.record_lab_result(request_id="req-lab-b2-turb", actor_id="rv1", batch_id="GZ-B2",
                              test_code="turbidity", outcome="pass", method_code="M-TUR",
                              measured_value="0.28", tested_at="2026-09-14T08:30:00Z",
                              tested_by="lab-gz")
        svc.complete_review(request_id="req-rev-prod-b2", actor_id="op-gz",
                            batch_id="GZ-B2", stage="production")
        svc.complete_review(request_id="req-rev-lab-b2", actor_id="rv1",
                            batch_id="GZ-B2", stage="laboratory")
        svc.create_decision(request_id="req-dec-release-b2", actor_id="ql2",
                            batch_id="GZ-B2", decision="release", reason="返工复检合格",
                            evidence_ref=f"lab_result:{b2_alc['resource_id']}")
        checks["rework_released_after_retest"] = \
            all(c["state"] == "release" for c in svc.effective_status("GZ-B2")["cells"])

        # 8) 嘉善批次浊度不合格：实验室复核不能收尾，只能冻结，不能放行。
        svc.register_material_lot(request_id="req-malt-js", actor_id="op-js",
                                  material_lot_id="lot-malt-js-1", site_id="site-js",
                                  kind="raw_material", material_code="MALT-PILSNER",
                                  attributes={"origin": "江苏"}, quarantined=False)
        svc.register_batch(request_id="req-js-b1", actor_id="op-js", batch_id="JS-B1",
                           relation="original",
                           site_id="site-js", line_id="line-js-1", product_code="P-ALE",
                           standard_id="std-premium", standard_version=2,
                           production_start="2026-09-15T00:00:00Z",
                           production_end="2026-09-15T08:00:00Z",
                           package_codes=["BTL-330"], region_codes=["CN-EAST"],
                           quantity=8000, material_lot_ids=["lot-malt-js-1", "lot-bottle-1"])
        svc.record_lab_result(request_id="req-lab-js-alc", actor_id="rv1", batch_id="JS-B1",
                              test_code="alcohol", outcome="pass", method_code="M-ALC",
                              measured_value="4.9", tested_at="2026-09-15T09:00:00Z",
                              tested_by="lab-js")
        svc.record_lab_result(request_id="req-lab-js-turb", actor_id="rv1", batch_id="JS-B1",
                              test_code="turbidity", outcome="fail", method_code="M-TUR",
                              measured_value="0.9", tested_at="2026-09-15T09:30:00Z",
                              tested_by="lab-js")
        svc.complete_review(request_id="req-rev-prod-js", actor_id="op-js",
                            batch_id="JS-B1", stage="production")
        try:
            svc.complete_review(request_id="req-rev-lab-js", actor_id="rv1",
                                batch_id="JS-B1", stage="laboratory")
            checks["lab_review_blocked_on_fail"] = False
        except ConflictError:
            checks["lab_review_blocked_on_fail"] = True
        svc.create_decision(request_id="req-dec-freeze-js", actor_id="ql1",
                            batch_id="JS-B1", decision="freeze",
                            reason="浊度超标，等待偏差处置",
                            evidence_ref="calibration:cal-filler-js")
        try:
            svc.create_decision(request_id="req-dec-release-js", actor_id="ql1",
                                batch_id="JS-B1", decision="release", reason="强行放行",
                                evidence_ref="calibration:cal-filler-js")
            checks["failed_batch_not_releaseable"] = False
        except ConflictError:
            checks["failed_batch_not_releaseable"] = True
        checks["frozen_batch_not_saleable"] = \
            all(not c["saleable"] and c["state"] == "freeze"
                for c in svc.effective_status("JS-B1")["cells"])

        # 9) 厦门批次放行出库后召回：历史决定保留，新决定追加，状态矩阵变为召回。
        svc.register_material_lot(request_id="req-malt-xm", actor_id="op-xm",
                                  material_lot_id="lot-malt-xm-1", site_id="site-xm",
                                  kind="raw_material", material_code="MALT-PILSNER",
                                  attributes={"origin": "福建"}, quarantined=False)
        svc.register_batch(request_id="req-xm-b1", actor_id="op-xm", batch_id="XM-B1",
                           relation="original",
                           site_id="site-xm", line_id="line-xm-1", product_code="P-ALE",
                           standard_id="std-premium", standard_version=2,
                           production_start="2026-09-16T00:00:00Z",
                           production_end="2026-09-16T08:00:00Z",
                           package_codes=["BTL-330"], region_codes=["CN-SOUTH"],
                           quantity=6000, material_lot_ids=["lot-malt-xm-1", "lot-bottle-1"])
        for req, code, value, mid in [
                ("req-lab-xm-alc", "alcohol", "4.7", "M-ALC"),
                ("req-lab-xm-turb", "turbidity", "0.4", "M-TUR")]:
            svc.record_lab_result(request_id=req, actor_id="rv1", batch_id="XM-B1",
                                  test_code=code, outcome="pass", method_code=mid,
                                  measured_value=value, tested_at="2026-09-16T09:00:00Z",
                                  tested_by="lab-xm")
        svc.complete_review(request_id="req-rev-prod-xm", actor_id="op-xm",
                            batch_id="XM-B1", stage="production")
        svc.complete_review(request_id="req-rev-lab-xm", actor_id="rv1",
                            batch_id="XM-B1", stage="laboratory")
        svc.create_decision(request_id="req-dec-release-xm", actor_id="ql1",
                            batch_id="XM-B1", decision="release", reason="检验合格",
                            evidence_ref="calibration:cal-filler-xm")
        svc.register_shipment(request_id="req-ship-xm", actor_id="op-xm",
                              shipment_id="SHIP-XM-1", batch_id="XM-B1",
                              package_code="BTL-330", region_code="CN-SOUTH",
                              quantity=1000, shipped_at="2026-09-17T08:00:00Z")
        svc.create_decision(request_id="req-dec-recall-xm", actor_id="ql2",
                            batch_id="XM-B1", decision="recall",
                            reason="客诉风味异常，启动召回",
                            evidence_ref="calibration:cal-filler-xm")
        checks["recalled_batch_not_saleable"] = \
            all(c["state"] == "recall" for c in svc.effective_status("XM-B1")["cells"])
        xm_decisions = svc.list_decisions("XM-B1")
        checks["recall_appends_history"] = [d["decision"] for d in xm_decisions] == ["release", "recall"]

        # 10) 重启前：历史放行决定固化 v1 快照，不被后续 v2 规则倒改。
        explain_gz = svc.explain("GZ-B1")
        gz_release = next(d for d in explain_gz["decisions"] if d["decision"] == "release")
        snapshot = explain_gz["decision_snapshots"][gz_release["decision_id"]]
        checks["historical_decision_keeps_v1_snapshot"] = \
            snapshot["standard_snapshot"]["tests"]["turbidity"]["max"] == 0.5
        shipped_snapshot = explain_gz["shipments"][0]["decision_snapshot"]
        checks["shipment_pins_release_decision"] = \
            shipped_snapshot["standard_version"] == 1 and shipped_snapshot["decided_by"] == "ql1"

        pending_before = {item["batch_id"]: item["open_stages"]
                          for item in svc.pending_batches()}
        checks["pending_includes_frozen_batch"] = \
            pending_before.get("JS-B1") == ["laboratory", "release"]
        valid_before, events_before = verify_chain(database.connection)
        database.close()

        # 11) 模拟服务重启：重新打开同一数据库，未结复核仍可继续，全链可还原。
        restarted = Database(db_path)
        svc2 = ReleaseService(restarted, FixedClock(T1))
        pending_after = {item["batch_id"]: item["open_stages"]
                         for item in svc2.pending_batches()}
        checks["review_state_survives_restart"] = pending_after == pending_before
        # 重启后嘉善批次改判（复检合格）需要新检验证据后才能放行，继续未结复核。
        svc2.record_lab_result(request_id="req-lab-js-turb-2", actor_id="rv1", batch_id="JS-B1",
                               test_code="turbidity", outcome="pass", method_code="M-TUR",
                               measured_value="0.4", tested_at="2026-09-21T02:00:00Z",
                               tested_by="lab-js", import_key="lab-import-js-2")
        svc2.complete_review(request_id="req-rev-lab-js-2", actor_id="rv1",
                             batch_id="JS-B1", stage="laboratory", notes="复检合格")
        svc2.create_decision(request_id="req-dec-release-js-2", actor_id="ql2",
                             batch_id="JS-B1", decision="release", reason="复检合格，解除冻结",
                             evidence_ref="calibration:cal-filler-js")
        checks["review_resumed_after_restart"] = \
            all(c["state"] == "release" for c in svc2.effective_status("JS-B1")["cells"])
        explain_after = svc2.explain("GZ-B1")
        checks["explain_reconstructs_chain"] = (
            explain_after["brand_standard"]["version"] == 1
            and {d["decision"] for d in explain_after["decisions"]} == {"release"}
            and len(explain_after["shipments"]) == 2
            and {r["status"] for r in explain_after["reviews"]} == {"completed"}
        )
        # 重启期间追加的事件与之前的审计链首尾相接，且整体可验。
        valid_after, events_after = verify_chain(restarted.connection)
        checks["audit_chain_across_restart"] = valid_after and events_after > events_before
        restarted.close()

    return {"status": "ok" if all(checks.values()) else "failed",
            "checks": checks, "audit_valid": valid_after,
            "audit_events": events_after}


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

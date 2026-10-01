"""跨工厂批次放行平台的离线端到端验收。

场景覆盖：

* 广州/嘉善/厦门三个工厂共用品牌标准版本、批号与校准证据；
* 批次钉住标准版本，实验室结果按钉住版本评价，新版本不倒改历史；
* 拆分、合并、返工保留谱系，祖先限制会阻止子批次放行；
* 偏差只影响部分销售区域，投影出"部分冻结、其余可售"的混合状态；
* 处置与解除限制由不同授权者引用处置证据完成（四眼原则）；
* 重复导入不改隔离状态，出库后的历史放行决定锁定，召回只追加；
* 服务重启后继续未结复核，审计哈希链持续可校验。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from beverage_ops_foundation.errors import ConflictError, PermissionDenied

from .service import BatchReleaseService
from .storage import ReleaseDatabase

SPEC_V1 = {"limits": {"alcohol": {"min": 4.5, "max": 5.5}, "ph": {"min": 4.0, "max": 4.6}}}
SPEC_V2 = {"limits": {"alcohol": {"min": 4.5, "max": 5.2}, "ph": {"min": 4.0, "max": 4.6}}}

WINDOW_START = "2026-10-01T00:00:00Z"
WINDOW_END = "2026-10-01T08:00:00Z"
CAL_FROM = "2026-01-01T00:00:00Z"
CAL_TO = "2027-01-01T00:00:00Z"


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _bootstrap(service: BatchReleaseService) -> None:
    service.foundation.register_organization(request_id="req-org", actor_id="bootstrap",
                                              organization_id="o1", name="高端啤酒品质团队")
    service.foundation.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin",
                                      display_name="系统管理员", role="admin", organization_id="o1")
    for actor_id, name, role in (
            ("rv-gz", "广州质量负责人", "reviewer"),
            ("rv-js", "嘉善质量负责人", "reviewer"),
            ("rv-xm", "厦门质量负责人", "reviewer"),
            ("op-gz", "广州操作员", "operator"),
            ("op-js", "嘉善操作员", "operator"),
            ("op-xm", "厦门操作员", "operator")):
        service.foundation.register_actor(
            request_id=f"req-{actor_id}", actor_id="admin", new_actor_id=actor_id,
            display_name=name, role=role, organization_id="o1")
    for site_id, name in (("site-gz", "广州工厂"), ("site-js", "嘉善工厂"), ("site-xm", "厦门工厂")):
        service.foundation.register_site(
            request_id=f"req-{site_id}", actor_id="admin", site_id=site_id,
            organization_id="o1", name=name, timezone_name="Asia/Shanghai")


def _release_evidence(service: BatchReleaseService, batch_id: str, *,
                      opened_by: str, decided_by: str, req_prefix: str) -> str:
    service.open_review(request_id=f"{req_prefix}-review", actor_id=opened_by, batch_id=batch_id,
                        kind="release_review", note="等待实验室复核")
    task = service.list_reviews("open")[-1]["task_id"]
    receipt = service.complete_review(request_id=f"{req_prefix}-complete", actor_id=decided_by,
                                      task_id=task, decision="release", rationale="证据齐全，同意放行")
    return receipt.resource_id


def run() -> dict[str, object]:
    """执行完整跨工厂放行链并返回校验结果。"""

    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "beer_release.sqlite3"
        service = BatchReleaseService(ReleaseDatabase(db_path))
        _bootstrap(service)

        # 品牌标准 v1 在三厂通用。
        service.register_brand_standard(request_id="std-v1", actor_id="rv-gz", standard_id="std-premium",
                                        version="v1", brand="醇品", spec=SPEC_V1)

        # 每个工厂登记自己的原料/包装批号与设备校准。
        for site in ("gz", "js", "xm"):
            service.register_lot(request_id=f"lot-m-{site}", actor_id=f"op-{site}", lot_id=f"malt-{site}",
                                 site_id=f"site-{site}", lot_kind="material", name="麦芽", supplier="华北粮贸")
            service.register_lot(request_id=f"lot-p-{site}", actor_id=f"op-{site}", lot_id=f"can-{site}",
                                 site_id=f"site-{site}", lot_kind="packaging", name="易拉罐", supplier="南方包材")
            service.register_calibration(request_id=f"cal-{site}", actor_id=f"op-{site}",
                                         calibration_id=f"filler-{site}", site_id=f"site-{site}",
                                         equipment_id="灌装机-1", calibrated_at=CAL_FROM,
                                         valid_from=CAL_FROM, valid_until=CAL_TO)

        # ---------------- 广州：放行 → 出库 → 历史锁定 → 召回只追加
        service.import_batch(request_id="imp-gz", actor_id="op-gz", site_id="site-gz", import_key="GZ-2026-0001",
                             brand="醇品", standard_id="std-premium", standard_version="v1",
                             production_start=WINDOW_START, production_end=WINDOW_END, quantity=1000.0,
                             unit="L", material_lot_ids=["malt-gz"], packaging_lot_ids=["can-gz"],
                             calibration_ids=["filler-gz"], batch_id="b-gz")
        service.record_lab_result(request_id="lab-gz", actor_id="op-gz", batch_id="b-gz", sample_code="GZ-S1",
                                  sampled_at="2026-10-01T09:00:00Z", tests={"alcohol": 5.0, "ph": 4.3})
        gz_release = _release_evidence(service, "b-gz", opened_by="op-gz", decided_by="rv-gz", req_prefix="gz")
        service.record_shipment(request_id="ship-gz", actor_id="op-gz", batch_id="b-gz",
                                decision_id=gz_release, quantity=600.0, shipped_at="2026-10-02T01:00:00Z")

        # 标准升级到 v2，但广州批次钉住 v1，历史评价不被倒改。
        service.register_brand_standard(request_id="std-v2", actor_id="rv-gz", standard_id="std-premium",
                                        version="v2", brand="醇品", spec=SPEC_V2)
        gz_explain_before = service.explain_batch("b-gz")
        _check(gz_explain_before["brand_standard"]["version"] == "v1", "批次必须钉住导入时的标准版本")
        _check(gz_explain_before["lab_results"][0]["evaluation"]["spec_hash"]
               == gz_explain_before["brand_standard"]["spec_hash"], "实验室评价必须引用钉住版本")

        # 重复导入：同内容原样重放，不改隔离状态；不同内容被拒绝。
        replay = service.import_batch(request_id="imp-gz", actor_id="op-gz", site_id="site-gz",
                                      import_key="GZ-2026-0001", brand="醇品", standard_id="std-premium",
                                      standard_version="v1", production_start=WINDOW_START,
                                      production_end=WINDOW_END, quantity=1000.0, unit="L",
                                      material_lot_ids=["malt-gz"], packaging_lot_ids=["can-gz"],
                                      calibration_ids=["filler-gz"], batch_id="b-gz")
        _check(replay.replayed, "重复导入必须返回重放回执")
        try:
            service.import_batch(request_id="imp-gz-2", actor_id="op-gz", site_id="site-gz",
                                 import_key="GZ-2026-0001", brand="醇品", standard_id="std-premium",
                                 standard_version="v1", production_start=WINDOW_START,
                                 production_end=WINDOW_END, quantity=900.0, unit="L",
                                 material_lot_ids=["malt-gz"], packaging_lot_ids=["can-gz"],
                                 calibration_ids=["filler-gz"], batch_id="b-gz")
            raise AssertionError("同导入键不同内容必须冲突")
        except ConflictError:
            pass
        gz_explain_replay = service.explain_batch("b-gz")
        _check(len(gz_explain_replay["decisions"]) == 1, "重复导入不得新增决定或改变隔离状态")

        # 召回：必须存在历史放行；历史放行决定保持锁定、出库记录不被倒改。
        recall = service.decide(request_id="dec-gz-recall", actor_id="rv-gz", batch_id="b-gz",
                                decision="recall", rationale="市售反馈异味，启动召回")
        gz_final = service.explain_batch("b-gz")
        _check(gz_final["batch"]["status"] == "recalled", "召回后批次粗粒度状态应为 recalled")
        release_decision = next(d for d in gz_final["decisions"] if d["decision"] == "release")
        _check(release_decision["historical_locked"] is True, "出库后的历史放行决定必须锁定")
        _check(len(release_decision["shipments"]) == 1, "历史出库记录必须保留")
        _check(gz_final["decisions"][-1]["decision_id"] == recall.resource_id, "召回必须以追加决定表达")

        # ---------------- 嘉善：部分区域偏差 → 混合状态 → 四眼解除
        service.import_batch(request_id="imp-js", actor_id="op-js", site_id="site-js", import_key="JS-2026-0007",
                             brand="醇品", standard_id="std-premium", standard_version="v1",
                             production_start=WINDOW_START, production_end=WINDOW_END, quantity=800.0,
                             unit="L", material_lot_ids=["malt-js"], packaging_lot_ids=["can-js"],
                             calibration_ids=["filler-js"], batch_id="b-js")
        service.record_lab_result(request_id="lab-js", actor_id="op-js", batch_id="b-js", sample_code="JS-S1",
                                  sampled_at="2026-10-01T09:30:00Z", tests={"alcohol": 4.9, "ph": 4.4})
        _release_evidence(service, "b-js", opened_by="op-js", decided_by="rv-js", req_prefix="js")

        east_scope = {"regions": ["华东"]}
        service.open_deviation(request_id="dev-js", actor_id="op-js", batch_id="b-js", scope=east_scope,
                               description="华东渠道标签喷码偏移", severity="medium", deviation_id="d-js")
        service.decide(request_id="dec-js-freeze", actor_id="rv-js", batch_id="b-js",
                       decision="freeze", rationale="标签问题隔离华东渠道", scope=east_scope)
        js_frozen = service.explain_batch("b-js")
        _check(js_frozen["effective_state"]["overall"] == "mixed", "只冻结华东必须呈现混合状态")
        cell_states = {(c["packaging"], c["region"]): c["state"] for c in js_frozen["effective_state"]["cells"]}
        _check(cell_states[(None, "华东")] == "frozen", "华东单元必须冻结")
        _check(cell_states[(None, None)] == "released", "其余渠道必须保持可售")

        # 处置由嘉善质量负责人完成。
        service.disposition_deviation(request_id="disp-js", actor_id="rv-js", deviation_id="d-js",
                                      disposition_summary="重新贴标并复检通过",
                                      evidence=["CAPA-JS-2026-0031", "lab://JS-S1/recheck"])
        # 处置人本人不能解除限制（四眼）。
        try:
            service.release_restriction(request_id="rel-js-self", actor_id="rv-js",
                                        restriction_id=_freeze_restriction(js_frozen),
                                        evidence=["CAPA-JS-2026-0031"], note="尝试自行解除")
            raise AssertionError("处置人不得自行解除限制")
        except PermissionDenied:
            pass
        # 广州质量负责人作为独立授权者引用处置证据解除。
        service.release_restriction(request_id="rel-js", actor_id="rv-gz",
                                    restriction_id=_freeze_restriction(js_frozen),
                                    evidence=["CAPA-JS-2026-0031"], note="复核复检报告，同意解除华东隔离")
        service.close_deviation(request_id="close-js", actor_id="rv-gz", deviation_id="d-js")
        service.decide(request_id="dec-js-rerelease", actor_id="rv-js", batch_id="b-js",
                       decision="release", rationale="华东贴标整改完成，恢复放行", scope=east_scope)
        js_done = service.explain_batch("b-js")
        _check(js_done["effective_state"]["overall"] == "released", "解除并重新放行后应全部可售")

        # ---------------- 厦门：拆分/合并/返工谱系与祖先限制继承
        service.import_batch(request_id="imp-xm", actor_id="op-xm", site_id="site-xm", import_key="XM-2026-0002",
                             brand="醇品", standard_id="std-premium", standard_version="v1",
                             production_start=WINDOW_START, production_end=WINDOW_END, quantity=1000.0,
                             unit="L", material_lot_ids=["malt-xm"], packaging_lot_ids=["can-xm"],
                             calibration_ids=["filler-xm"], batch_id="b-xm")
        service.record_lab_result(request_id="lab-xm", actor_id="op-xm", batch_id="b-xm", sample_code="XM-S1",
                                  sampled_at="2026-10-01T10:00:00Z", tests={"alcohol": 4.8, "ph": 4.2})
        _release_evidence(service, "b-xm", opened_by="op-xm", decided_by="rv-xm", req_prefix="xm")
        service.split_batch(request_id="split-xm", actor_id="op-xm", batch_id="b-xm", children=[
            {"batch_id": "c-xm-1", "quantity": 500.0, "scope": {"packaging": ["330ml罐"]}},
            {"batch_id": "c-xm-2", "quantity": 500.0, "scope": {"packaging": ["500ml罐"]}},
        ])
        service.record_lab_result(request_id="lab-c1", actor_id="op-xm", batch_id="c-xm-1", sample_code="C1-S1",
                                  sampled_at="2026-10-01T11:00:00Z", tests={"alcohol": 4.8, "ph": 4.2})

        # 母批次被追加全范围限制（供应商追溯），子批次放行必须被祖先限制阻止。
        service.open_deviation(request_id="dev-xm", actor_id="op-xm", batch_id="b-xm", scope={},
                               description="麦芽供应商追溯预警", severity="high", deviation_id="d-xm")
        service.impose_restriction(request_id="res-xm", actor_id="op-xm", batch_id="b-xm",
                                   reason="供应商追溯冻结", deviation_id="d-xm")
        blocked = service.explain_batch("c-xm-1")
        _check(any(r["inherited"] and r["restriction_id"] for r in blocked["restrictions"]),
               "子批次必须可见继承自祖先的限制")
        try:
            service.decide(request_id="dec-c1-blocked", actor_id="rv-xm", batch_id="c-xm-1",
                           decision="release", rationale="尝试放行拆分批次")
            raise AssertionError("祖先活动限制必须阻止子批次放行")
        except ConflictError as exc:
            _check("祖先" in str(exc), "阻断原因必须指明继承限制")

        # 另一厂质量负责人处置，厦门负责人解除，子批次随后放行。
        service.disposition_deviation(request_id="disp-xm", actor_id="rv-js", deviation_id="d-xm",
                                      disposition_summary="供应商批次复检合格", evidence=["COA-MALT-7788"])
        xm_restriction = blocked["restrictions"][0]["restriction_id"]
        service.release_restriction(request_id="rel-xm", actor_id="rv-xm", restriction_id=xm_restriction,
                                    evidence=["COA-MALT-7788"], note="复检合格，解除追溯冻结")
        service.close_deviation(request_id="close-xm", actor_id="rv-xm", deviation_id="d-xm")
        service.decide(request_id="dec-c1", actor_id="rv-xm", batch_id="c-xm-1",
                       decision="release", rationale="祖先限制解除，拆分批次放行")

        # 合并：仅同品牌同钉住版本允许；谱系可回溯两个母批次。
        service.merge_batch(request_id="merge-xm", actor_id="op-xm",
                            parent_batch_ids=["c-xm-1", "c-xm-2"], quantity=1000.0,
                            child_batch_id="m-xm")
        lineage = service.get_lineage("m-xm")
        ancestor_ids = {a["parent_batch_id"] for a in lineage["ancestors"]}
        _check({"c-xm-1", "c-xm-2", "b-xm"} <= ancestor_ids, "合并批次必须保留完整祖先谱系")
        merged_explain = service.explain_batch("m-xm")
        _check(any(i["lot_id"] == "malt-xm" for i in merged_explain["inputs"]), "合并批次必须继承批号证据")

        # 返工：不合格来源批次返工后得到新批次，谱系与证据都保留。
        service.import_batch(request_id="imp-rw", actor_id="op-xm", site_id="site-xm", import_key="XM-2026-0009",
                             brand="醇品", standard_id="std-premium", standard_version="v1",
                             production_start=WINDOW_START, production_end=WINDOW_END, quantity=300.0,
                             unit="L", material_lot_ids=["malt-xm"], packaging_lot_ids=["can-xm"],
                             calibration_ids=["filler-xm"], batch_id="b-rw-src")
        service.record_lab_result(request_id="lab-rw-bad", actor_id="op-xm", batch_id="b-rw-src",
                                  sample_code="RW-S1", sampled_at="2026-10-01T12:00:00Z",
                                  tests={"alcohol": 4.2, "ph": 4.3})
        service.rework_batch(request_id="rework-xm", actor_id="op-xm", source_batch_id="b-rw-src",
                             quantity=300.0, derived_batch_id="b-rw-new", note="浊度不合格返工")
        service.record_lab_result(request_id="lab-rw-good", actor_id="op-xm", batch_id="b-rw-new",
                                  sample_code="RW-S2", sampled_at="2026-10-01T15:00:00Z",
                                  tests={"alcohol": 4.7, "ph": 4.3})
        service.decide(request_id="dec-rw", actor_id="rv-xm", batch_id="b-rw-new",
                       decision="release", rationale="返工后复检合格")
        _check(service.get_lineage("b-rw-new")["ancestors"][0]["relation"] == "rework", "返工谱系必须保留")

        # ---------------- 重启续办：留一个未结复核，关闭后用新实例继续
        service.import_batch(request_id="imp-restart", actor_id="op-gz", site_id="site-gz",
                             import_key="GZ-2026-0010", brand="醇品", standard_id="std-premium",
                             standard_version="v1", production_start=WINDOW_START, production_end=WINDOW_END,
                             quantity=200.0, unit="L", material_lot_ids=["malt-gz"],
                             packaging_lot_ids=["can-gz"], calibration_ids=["filler-gz"], batch_id="b-restart")
        service.record_lab_result(request_id="lab-restart", actor_id="op-gz", batch_id="b-restart",
                                  sample_code="RS-S1", sampled_at="2026-10-01T16:00:00Z",
                                  tests={"alcohol": 5.1, "ph": 4.4})
        service.open_review(request_id="review-restart", actor_id="op-gz", batch_id="b-restart",
                            kind="release_review", note="等待重启后继续", task_id="task-restart")
        service.database.close()

        restarted = BatchReleaseService(ReleaseDatabase(db_path))
        open_tasks = restarted.list_reviews("open")
        _check(any(t["task_id"] == "task-restart" for t in open_tasks), "重启后必须能找回未结复核")
        restarted.complete_review(request_id="complete-restart", actor_id="rv-gz",
                                  task_id="task-restart", decision="release", rationale="重启后续办放行")
        restart_explain = restarted.explain_batch("b-restart")
        _check(restart_explain["batch"]["status"] == "released", "重启后续办放行必须生效")

        # ---------------- 全量审计链与跨厂决定可还原性
        valid, event_count = restarted.verify_audit()
        _check(valid, "审计哈希链必须完整可校验")
        for batch_id in ("b-gz", "b-js", "b-xm", "b-rw-new", "b-restart"):
            explanation = restarted.explain_batch(batch_id)
            _check(explanation["audit"]["valid"], f"{batch_id} 的证据链必须可还原")
        restarted.database.close()

        return {"status": "ok", "audit_valid": valid, "audit_events": event_count,
                "factories": ["广州", "嘉善", "厦门"],
                "checks": ["标准版本钉住", "重复导入不改隔离", "部分区域混合状态", "四眼解除限制",
                           "谱系继承阻断", "返工谱系", "历史决定锁定", "重启续办", "召回只追加"]}


def _freeze_restriction(explanation: dict[str, object]) -> str:
    for restriction in explanation["restrictions"]:
        if restriction["reason"].startswith("freeze_decision:") and restriction["status"] == "active":
            return restriction["restriction_id"]
    raise AssertionError("未找到冻结决定创建的活动限制")


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""跨工厂批次放行域的服务规则测试。"""

import unittest
from datetime import datetime, timezone

from beverage_ops_foundation.errors import (
    ConflictError, NotFoundError, PermissionDenied, ValidationError,
)
from beverage_ops_foundation.release.service import ReleaseService
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database

STANDARD = {
    "products": ["P1"],
    "tests": {"t1": {"method_code": "M1", "min": 1.0, "max": 9.0}},
    "equipment": ["EQ1"],
}


class ReleaseServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = datetime(2026, 9, 20, tzinfo=timezone.utc)
        from beverage_ops_foundation.clock import FixedClock

        self.clock = FixedClock(clock)
        self.base = DomainService(self.database, self.clock)
        self.svc = ReleaseService(self.database, self.clock)
        self.base.register_organization(request_id="org", actor_id="bootstrap",
                                        organization_id="o1", name="啤酒集团")
        self.base.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="a1",
                                 display_name="管理员", role="admin", organization_id="o1")
        for key, actor_id, name, role in [
                ("op", "op1", "操作员", "operator"),
                ("rv", "rv1", "复核员", "reviewer"),
                ("q1", "q1", "质量负责人甲", "quality_lead"),
                ("q2", "q2", "质量负责人乙", "quality_lead"),
                ("au", "au1", "审计员", "auditor")]:
            self.base.register_actor(request_id=key, actor_id="a1", new_actor_id=actor_id,
                                     display_name=name, role=role, organization_id="o1")
        self.base.register_site(request_id="site", actor_id="a1", site_id="s1",
                                organization_id="o1", name="广州厂", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def _ready_batch(self, batch_id="B1", standard_version=1, quantity=100):
        self.svc.register_brand_standard(request_id="std", actor_id="q1",
                                         standard_id="S1", version=standard_version,
                                         payload=STANDARD)
        self.svc.register_line(request_id="line", actor_id="a1", line_id="L1",
                               site_id="s1", name="一号线")
        self.svc.register_calibration(request_id="cal", actor_id="op1",
                                      calibration_id="C1", site_id="s1", equipment_code="EQ1",
                                      valid_from="2026-09-01T00:00:00Z",
                                      valid_until="2026-09-30T23:59:59Z")
        self.svc.register_material_lot(request_id="mat", actor_id="op1",
                                       material_lot_id="M1", site_id="s1", kind="raw_material",
                                       material_code="RAW", attributes={}, quarantined=False)
        self.svc.register_batch(request_id="batch", actor_id="op1", batch_id=batch_id,
                                site_id="s1", line_id="L1", product_code="P1",
                                standard_id="S1", standard_version=standard_version,
                                production_start="2026-09-10T00:00:00Z",
                                production_end="2026-09-10T08:00:00Z",
                                package_codes=["P-A", "P-B"], region_codes=["R1", "R2"],
                                quantity=quantity, material_lot_ids=["M1"])
        result = self.svc.record_lab_result(request_id="lab", actor_id="rv1",
                                            batch_id=batch_id, test_code="t1", outcome="pass",
                                            method_code="M1", measured_value="5.0",
                                            tested_at="2026-09-10T09:00:00Z", tested_by="lab")
        self.svc.complete_review(request_id="rev-p", actor_id="op1",
                                 batch_id=batch_id, stage="production")
        self.svc.complete_review(request_id="rev-l", actor_id="rv1",
                                 batch_id=batch_id, stage="laboratory")
        return result["resource_id"]

    def test_brand_standard_versions_must_be_sequential(self):
        self.svc.register_brand_standard(request_id="std1", actor_id="q1",
                                         standard_id="S1", version=1, payload=STANDARD)
        with self.assertRaises(ConflictError):
            self.svc.register_brand_standard(request_id="std3", actor_id="q1",
                                             standard_id="S1", version=3, payload=STANDARD)

    def test_duplicate_material_import_cannot_clear_quarantine(self):
        self.svc.register_material_lot(request_id="m1", actor_id="op1", material_lot_id="M1",
                                       site_id="s1", kind="packaging", material_code="BOX",
                                       attributes={"a": 1}, quarantined=True,
                                       import_key="import-1")
        self.svc.register_material_lot(request_id="m2", actor_id="op1", material_lot_id="M1",
                                       site_id="s1", kind="packaging", material_code="BOX",
                                       attributes={"a": 1}, quarantined=False,
                                       import_key="import-2")
        self.assertTrue(self.svc.get_material_lot("M1")["quarantined"])

    def test_conflicting_material_reimport_is_rejected(self):
        kwargs = dict(actor_id="op1", material_lot_id="M1", site_id="s1", kind="packaging",
                      material_code="BOX", quarantined=False)
        self.svc.register_material_lot(request_id="m1", attributes={"a": 1}, **kwargs)
        with self.assertRaises(ConflictError):
            self.svc.register_material_lot(request_id="m2", attributes={"a": 2}, **kwargs)

    def test_release_requires_full_evidence_chain(self):
        lab_id = self._ready_batch()
        # 删除校准视角不可行；改为新建一条缺少校准的批次。
        self.svc.register_batch(request_id="batch2", actor_id="op1", batch_id="B2",
                                site_id="s1", line_id="L1", product_code="P1",
                                standard_id="S1", standard_version=1,
                                production_start="2026-11-10T00:00:00Z",
                                production_end="2026-11-10T08:00:00Z",
                                package_codes=["P-A"], region_codes=["R1"], quantity=10,
                                material_lot_ids=["M1"])
        self.svc.record_lab_result(request_id="lab2", actor_id="rv1", batch_id="B2",
                                   test_code="t1", outcome="pass", method_code="M1",
                                   measured_value="5", tested_at="2026-11-10T09:00:00Z",
                                   tested_by="lab")
        self.svc.complete_review(request_id="rev2p", actor_id="op1",
                                 batch_id="B2", stage="production")
        self.svc.complete_review(request_id="rev2l", actor_id="rv1",
                                 batch_id="B2", stage="laboratory")
        with self.assertRaises(ConflictError) as caught:
            self.svc.create_decision(request_id="dec2", actor_id="q1", batch_id="B2",
                                     decision="release", reason="x",
                                     evidence_ref=f"lab_result:{lab_id}")
        self.assertIn("calibration_missing:EQ1", str(caught.exception))

    def test_four_eyes_required_to_lift_restriction(self):
        self._ready_batch()
        self.svc.raise_restriction(request_id="rr", actor_id="q1", batch_id="B1",
                                   scope_type="package", scope_values=["P-A"],
                                   reason="包装印刷调查", evidence_ref="calibration:C1")
        with self.assertRaises(PermissionDenied):
            self.svc.lift_restriction(request_id="lift-self", actor_id="q1",
                                     restriction_id=self.svc.list_restrictions("B1", True)[0]["restriction_id"],
                                     evidence_ref="calibration:C1")
        self.svc.lift_restriction(request_id="lift-other", actor_id="q2",
                                 restriction_id=self.svc.list_restrictions("B1", True)[0]["restriction_id"],
                                 evidence_ref="calibration:C1")
        self.assertEqual("lifted", self.svc.list_restrictions("B1")[0]["status"])

    def test_operator_cannot_make_release_decision(self):
        self._ready_batch()
        with self.assertRaises(PermissionDenied):
            self.svc.create_decision(request_id="dec", actor_id="op1", batch_id="B1",
                                     decision="release", reason="x", evidence_ref="calibration:C1")

    def test_partial_scope_freeze_and_release_matrix(self):
        lab_id = self._ready_batch()
        self.svc.create_decision(request_id="rel", actor_id="q1", batch_id="B1",
                                 decision="release", reason="合格",
                                 evidence_ref=f"lab_result:{lab_id}")
        self.svc.create_decision(request_id="frz", actor_id="q1", batch_id="B1",
                                 decision="freeze", scope_type="region",
                                 scope_values=["R2"], reason="区域调查",
                                 evidence_ref="calibration:C1")
        status = self.svc.effective_status("B1")
        by_cell = {f"{c['package_code']}|{c['region_code']}": c["state"]
                   for c in status["cells"]}
        self.assertEqual("release", by_cell["P-A|R1"])
        self.assertEqual("freeze", by_cell["P-A|R2"])
        self.assertEqual("freeze", by_cell["P-B|R2"])
        self.assertTrue(next(c["saleable"] for c in status["cells"]
                             if c["package_code"] == "P-A" and c["region_code"] == "R1"))

    def test_restriction_blocks_saleable_but_release_decision_remains(self):
        lab_id = self._ready_batch()
        self.svc.create_decision(request_id="rel", actor_id="q1", batch_id="B1",
                                 decision="release", reason="合格",
                                 evidence_ref=f"lab_result:{lab_id}")
        self.svc.raise_restriction(request_id="rr", actor_id="q1", batch_id="B1",
                                   scope_type="package", scope_values=["P-B"],
                                   reason="包装调查", evidence_ref="calibration:C1")
        status = self.svc.effective_status("B1")
        cell = next(c for c in status["cells"] if c["package_code"] == "P-B")
        self.assertEqual("release", cell["state"])
        self.assertFalse(cell["saleable"])
        with self.assertRaises(ConflictError):
            self.svc.register_shipment(request_id="sh", actor_id="op1", shipment_id="S1",
                                       batch_id="B1", package_code="P-B", region_code="R1",
                                       quantity=1, shipped_at="2026-09-11T00:00:00Z")

    def test_split_and_merge_preserve_lineage_and_materials(self):
        self._ready_batch()
        self.svc.register_material_lot(request_id="mnew", actor_id="op1",
                                       material_lot_id="M2", site_id="s1", kind="packaging",
                                       material_code="BOX2", attributes={}, quarantined=False)
        self.svc.register_batch(request_id="split", actor_id="op1", batch_id="B2",
                                site_id="s1", line_id="L1", product_code="P1",
                                standard_id="S1", standard_version=1,
                                production_start="2026-09-11T00:00:00Z",
                                production_end="2026-09-11T04:00:00Z",
                                package_codes=["P-A"], region_codes=["R1"], quantity=50,
                                relation="split", parent_batch_ids=["B1"],
                                material_lot_ids=["M2"])
        lineage = self.svc.lineage("B2")
        self.assertEqual(["B1"], [a["related_batch_id"] for a in lineage["ancestors"]])
        self.assertIn("M1", self.svc.get_batch("B2")["material_lot_ids"])
        self.svc.register_batch(request_id="merge", actor_id="op1", batch_id="B3",
                                site_id="s1", line_id="L1", product_code="P1",
                                standard_id="S1", standard_version=1,
                                production_start="2026-09-12T00:00:00Z",
                                production_end="2026-09-12T04:00:00Z",
                                package_codes=["P-A"], region_codes=["R1"], quantity=60,
                                relation="merge", parent_batch_ids=["B1", "B2"])
        self.assertEqual({"B1", "B2"},
                         {a["related_batch_id"] for a in self.svc.lineage("B3")["ancestors"]})

    def test_merge_requires_two_parents(self):
        self._ready_batch()
        with self.assertRaises(ValidationError):
            self.svc.register_batch(request_id="merge", actor_id="op1", batch_id="B9",
                                    site_id="s1", line_id="L1", product_code="P1",
                                    standard_id="S1", standard_version=1,
                                    production_start="2026-09-12T00:00:00Z",
                                    production_end="2026-09-12T04:00:00Z",
                                    package_codes=["P-A"], region_codes=["R1"], quantity=60,
                                    relation="merge", parent_batch_ids=["B1"])

    def test_recall_requires_market_or_prior_release(self):
        self._ready_batch()
        with self.assertRaises(ConflictError):
            self.svc.create_decision(request_id="rec", actor_id="q1", batch_id="B1",
                                     decision="recall", reason="x",
                                     evidence_ref="calibration:C1")

    def test_decisions_are_append_only(self):
        lab_id = self._ready_batch()
        self.svc.create_decision(request_id="rel", actor_id="q1", batch_id="B1",
                                 decision="release", reason="合格",
                                 evidence_ref=f"lab_result:{lab_id}")
        self.svc.create_decision(request_id="frz", actor_id="q2", batch_id="B1",
                                 decision="freeze", reason="市场调查",
                                 evidence_ref="calibration:C1")
        decisions = self.svc.list_decisions("B1")
        self.assertEqual(["release", "freeze"], [d["decision"] for d in decisions])
        self.assertEqual(decisions[0]["decision_id"], decisions[1]["supersedes_decision_id"])

    def test_reviews_persist_across_service_restart(self):
        import tempfile
        from pathlib import Path

        from beverage_ops_foundation.clock import FixedClock
        from beverage_ops_foundation.release.service import ReleaseService

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "restart.sqlite3"
            database = Database(db_path)
            base = DomainService(database, FixedClock(datetime(2026, 9, 20, tzinfo=timezone.utc)))
            svc = ReleaseService(database, FixedClock(datetime(2026, 9, 20, tzinfo=timezone.utc)))
            base.register_organization(request_id="org", actor_id="bootstrap",
                                       organization_id="o1", name="啤酒集团")
            base.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="a1",
                                display_name="管理员", role="admin", organization_id="o1")
            base.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                display_name="操作员", role="operator", organization_id="o1")
            base.register_actor(request_id="q1", actor_id="a1", new_actor_id="q1",
                                display_name="质量负责人", role="quality_lead",
                                organization_id="o1")
            base.register_site(request_id="site", actor_id="a1", site_id="s1",
                               organization_id="o1", name="广州厂", timezone_name="Asia/Shanghai")
            svc.register_brand_standard(request_id="std", actor_id="q1",
                                        standard_id="S1", version=1, payload=STANDARD)
            svc.register_line(request_id="line", actor_id="a1", line_id="L1",
                              site_id="s1", name="一号线")
            svc.register_calibration(request_id="cal", actor_id="op1", calibration_id="C1",
                                     site_id="s1", equipment_code="EQ1",
                                     valid_from="2026-09-01T00:00:00Z",
                                     valid_until="2026-09-30T23:59:59Z")
            svc.register_material_lot(request_id="mat", actor_id="op1", material_lot_id="M1",
                                      site_id="s1", kind="raw_material", material_code="RAW",
                                      attributes={}, quarantined=False)
            svc.register_batch(request_id="batch", actor_id="op1", batch_id="B1",
                               site_id="s1", line_id="L1", product_code="P1",
                               standard_id="S1", standard_version=1,
                               production_start="2026-09-10T00:00:00Z",
                               production_end="2026-09-10T08:00:00Z",
                               package_codes=["P-A"], region_codes=["R1"], quantity=100,
                               material_lot_ids=["M1"])
            database.close()

            restarted = Database(db_path)
            svc2 = ReleaseService(restarted, FixedClock(datetime(2026, 9, 21, tzinfo=timezone.utc)))
            pending = svc2.pending_batches()
            self.assertEqual(["B1"], [item["batch_id"] for item in pending])
            self.assertEqual(["laboratory", "production", "release"], pending[0]["open_stages"])
            restarted.close()

    def test_explain_reconstructs_full_chain(self):
        lab_id = self._ready_batch()
        self.svc.create_decision(request_id="rel", actor_id="q1", batch_id="B1",
                                 decision="release", reason="合格",
                                 evidence_ref=f"lab_result:{lab_id}")
        explanation = self.svc.explain("B1")
        self.assertEqual("S1", explanation["brand_standard"]["standard_id"])
        self.assertEqual(1, len(explanation["lab_results"]))
        self.assertIn("decision_snapshots", explanation)
        self.assertEqual("release", explanation["effective_status"]["cells"][0]["state"])
        actions = {event["action"] for event in explanation["audit_events"]}
        self.assertIn("release.decision_created", actions)

    def test_evidence_reference_must_exist(self):
        self._ready_batch()
        with self.assertRaises(NotFoundError):
            self.svc.create_decision(request_id="rel", actor_id="q1", batch_id="B1",
                                     decision="release", reason="合格",
                                     evidence_ref="lab_result:does-not-exist")

    def test_idempotent_replay_returns_same_decision(self):
        lab_id = self._ready_batch()
        first = self.svc.create_decision(request_id="rel", actor_id="q1", batch_id="B1",
                                         decision="release", reason="合格",
                                         evidence_ref=f"lab_result:{lab_id}")
        second = self.svc.create_decision(request_id="rel", actor_id="q1", batch_id="B1",
                                          decision="release", reason="合格",
                                          evidence_ref=f"lab_result:{lab_id}")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["resource_id"], second["resource_id"])


if __name__ == "__main__":
    unittest.main()

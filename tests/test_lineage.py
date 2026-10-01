"""拆分、合并、返工谱系与祖先限制继承测试。"""

import unittest

from beverage_ops_foundation.errors import ConflictError, ValidationError

from beer_release_support import build_service, import_ready_batch, release_via_review


class LineageTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        import_ready_batch(self.service, "parent")
        release_via_review(self.service, "parent")

    def tearDown(self):
        self.service.database.close()

    def test_split_preserves_lineage_and_inherits_evidence(self):
        receipt = self.service.split_batch(request_id="split", actor_id="op-gz", batch_id="parent",
                                           children=[{"batch_id": "c1", "quantity": 400.0},
                                                     {"batch_id": "c2", "quantity": 600.0}])
        self.assertEqual(["c1", "c2"], receipt.related_ids)
        lineage = self.service.get_lineage("c1")
        self.assertEqual("parent", lineage["ancestors"][0]["parent_batch_id"])
        self.assertEqual("split", lineage["ancestors"][0]["relation"])
        explanation = self.service.explain_batch("c1")
        # 批号与校准证据从母批次继承，缺的只是子批次自己的实验室结果。
        lot_ids = {lot["lot_id"] for lot in explanation["inputs"]}
        self.assertEqual({"malt-gz", "can-gz"}, lot_ids)
        self.assertTrue(explanation["calibrations"])

    def test_split_quantity_cannot_exceed_parent(self):
        with self.assertRaises(ValidationError):
            self.service.split_batch(request_id="split-bad", actor_id="op-gz", batch_id="parent",
                                     children=[{"batch_id": "c9", "quantity": 1200.0}])

    def test_ancestor_restriction_blocks_child_release(self):
        self.service.split_batch(request_id="split", actor_id="op-gz", batch_id="parent",
                                 children=[{"batch_id": "c1", "quantity": 1000.0}])
        self.service.record_lab_result(request_id="lab-c1", actor_id="op-gz", batch_id="c1",
                                       sample_code="C1-S1", sampled_at="2026-10-01T11:00:00Z",
                                       tests={"alcohol": 5.0, "ph": 4.3})
        self.service.open_deviation(request_id="dev", actor_id="op-gz", batch_id="parent", scope={},
                                    description="追溯冻结", severity="high", deviation_id="d1")
        self.service.impose_restriction(request_id="res", actor_id="op-gz", batch_id="parent",
                                        reason="追溯", deviation_id="d1")
        with self.assertRaises(ConflictError) as ctx:
            self.service.decide(request_id="blocked", actor_id="rv-gz", batch_id="c1",
                                decision="release", rationale="子批次尝试放行")
        self.assertIn("祖先", str(ctx.exception))

    def test_merge_requires_same_pinned_standard_version(self):
        from beer_release_support import WINDOW_END, WINDOW_START
        self.service.split_batch(request_id="split", actor_id="op-gz", batch_id="parent",
                                 children=[{"batch_id": "c1", "quantity": 400.0},
                                           {"batch_id": "c2", "quantity": 400.0}])
        # 登记 v2 标准并导入一个钉住 v2 的批次，与 v1 子批次禁止合并。
        self.service.register_brand_standard(
            request_id="std-v2", actor_id="rv-gz", standard_id="std", version="v2", brand="醇品",
            spec={"limits": {"alcohol": {"min": 4.5, "max": 5.2}, "ph": {"min": 4.0, "max": 4.6}}})
        self.service.import_batch(request_id="imp-v2", actor_id="op-gz", site_id="gz",
                                  import_key="V2BATCH", brand="醇品", standard_id="std",
                                  standard_version="v2", production_start=WINDOW_START,
                                  production_end=WINDOW_END, quantity=400.0, unit="L",
                                  material_lot_ids=["malt-gz"], packaging_lot_ids=["can-gz"],
                                  calibration_ids=["filler-gz"], batch_id="v2batch")
        with self.assertRaises(ValidationError):
            self.service.merge_batch(request_id="merge-bad", actor_id="op-gz",
                                     parent_batch_ids=["c1", "v2batch"], quantity=500.0,
                                     child_batch_id="m-bad")

    def test_merge_and_rework_keep_full_history(self):
        self.service.split_batch(request_id="split", actor_id="op-gz", batch_id="parent",
                                 children=[{"batch_id": "c1", "quantity": 400.0},
                                           {"batch_id": "c2", "quantity": 400.0}])
        self.service.merge_batch(request_id="merge", actor_id="op-gz",
                                 parent_batch_ids=["c1", "c2"], quantity=800.0, child_batch_id="m1")
        ancestors = {a["parent_batch_id"] for a in self.service.get_lineage("m1")["ancestors"]}
        self.assertEqual({"c1", "c2", "parent"}, ancestors)

        self.service.rework_batch(request_id="rework", actor_id="op-gz", source_batch_id="m1",
                                  quantity=800.0, derived_batch_id="rw1", note="返工")
        self.assertEqual("rework", self.service.get_lineage("rw1")["ancestors"][0]["relation"])
        ancestors = {a["parent_batch_id"] for a in self.service.get_lineage("rw1")["ancestors"]}
        self.assertIn("m1", ancestors)


if __name__ == "__main__":
    unittest.main()

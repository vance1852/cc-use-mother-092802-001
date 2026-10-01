"""放行门禁与品牌标准版本钉住测试。"""

import unittest

from beverage_ops_foundation.errors import ConflictError, PermissionDenied, ValidationError

from beer_release_support import (
    SPEC_V1,
    WINDOW_END,
    WINDOW_START,
    build_service,
    import_ready_batch,
    release_via_review,
)


class ReleaseGateTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def tearDown(self):
        self.service.database.close()

    def test_full_evidence_chain_allows_release(self):
        import_ready_batch(self.service, "b1")
        decision_id = release_via_review(self.service, "b1")
        explanation = self.service.explain_batch("b1")
        self.assertEqual("released", explanation["batch"]["status"])
        self.assertTrue(explanation["calibrations"][0]["covers_production_window"])
        self.assertTrue(explanation["lab_results"][0]["conforms"])
        self.assertEqual(decision_id, explanation["decisions"][-1]["decision_id"])

    def test_release_blocked_without_lab_result(self):
        from beer_release_support import seed_factory
        seed_factory(self.service, "gz")
        self.service.import_batch(request_id="imp-b2", actor_id="op-gz", site_id="gz", import_key="B2",
                                  brand="醇品", standard_id="std", standard_version="v1",
                                  production_start=WINDOW_START, production_end=WINDOW_END, quantity=10.0,
                                  unit="L", material_lot_ids=["malt-gz"], packaging_lot_ids=["can-gz"],
                                  calibration_ids=["filler-gz"], batch_id="b2")
        with self.assertRaises(ConflictError) as ctx:
            self.service.decide(request_id="d", actor_id="rv-gz", batch_id="b2",
                                decision="release", rationale="无实验室结果尝试放行")
        self.assertIn("实验室结果", str(ctx.exception))

    def test_nonconforming_lab_blocks_release(self):
        import_ready_batch(self.service, "b3", lab={"alcohol": 4.1, "ph": 4.3})
        with self.assertRaises(ConflictError) as ctx:
            self.service.decide(request_id="d", actor_id="rv-gz", batch_id="b3",
                                decision="release", rationale="不合格尝试放行")
        self.assertIn("不合格", str(ctx.exception))

    def test_expired_calibration_blocks_release(self):
        batch_id = import_ready_batch(self.service, "b4")
        # 事后吊销校准只影响今后判定；但尚未放行的批次此时再决定放行应被阻断。
        self.service.revoke_calibration(request_id="rev-cal", actor_id="rv-gz",
                                        calibration_id="filler-gz", reason="设备返厂")
        with self.assertRaises(ConflictError) as ctx:
            self.service.decide(request_id="d", actor_id="rv-gz", batch_id=batch_id,
                                decision="release", rationale="校准失效后尝试放行")
        self.assertIn("校准", str(ctx.exception))

    def test_operator_cannot_release(self):
        import_ready_batch(self.service, "b5")
        with self.assertRaises(PermissionDenied):
            self.service.decide(request_id="d", actor_id="op-gz", batch_id="b5",
                                decision="release", rationale="操作员尝试放行")

    def test_review_release_requires_different_person(self):
        import_ready_batch(self.service, "b6")
        self.service.open_review(request_id="rev-b6", actor_id="op-gz", batch_id="b6",
                                 kind="release_review")
        task = self.service.list_reviews("open")[-1]["task_id"]
        with self.assertRaises(PermissionDenied):
            self.service.complete_review(request_id="done-b6", actor_id="op-gz", task_id=task,
                                         decision="release", rationale="开单人自行放行")

    def test_batch_pins_standard_version_and_new_version_is_not_retroactive(self):
        batch_id = import_ready_batch(self.service, "b7")
        release_via_review(self.service, batch_id)
        new_spec = {"limits": {"alcohol": {"min": 4.5, "max": 4.7}, "ph": {"min": 4.0, "max": 4.6}}}
        self.service.register_brand_standard(request_id="std-v2", actor_id="rv-gz", standard_id="std",
                                             version="v2", brand="醇品", spec=new_spec)
        explanation = self.service.explain_batch(batch_id)
        self.assertEqual("v1", explanation["brand_standard"]["version"])
        self.assertEqual("released", explanation["batch"]["status"])
        # 已登记版本不可变。
        with self.assertRaises(ConflictError):
            self.service.register_brand_standard(request_id="std-v1-tamper", actor_id="rv-gz",
                                                 standard_id="std", version="v1", brand="醇品",
                                                 spec={"limits": {"alcohol": {"min": 1.0, "max": 9.0}}})


if __name__ == "__main__":
    unittest.main()

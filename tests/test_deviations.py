"""部分范围偏差、混合状态投影与四眼解除限制测试。"""

import unittest

from beverage_ops_foundation.errors import ConflictError, PermissionDenied, ValidationError

from beer_release_support import build_service, import_ready_batch, release_via_review
from beer_release.service import normalize_scope, scope_covers_cell, scope_overlaps


class ScopeModelTest(unittest.TestCase):
    def test_normalize_scope_sorts_and_dedupes(self):
        scope = normalize_scope({"packaging": ["b", "a", "a"], "regions": ["华东"]})
        self.assertEqual(["a", "b"], scope["packaging"])
        self.assertEqual(["华东"], scope["regions"])

    def test_empty_scope_means_everything(self):
        scope = normalize_scope({})
        self.assertTrue(scope_covers_cell(scope, "330ml罐", "华东"))
        self.assertTrue(scope_covers_cell(scope, None, None))

    def test_specific_region_does_not_cover_wildcard_cell(self):
        scope = normalize_scope({"regions": ["华东"]})
        self.assertTrue(scope_covers_cell(scope, None, "华东"))
        self.assertFalse(scope_covers_cell(scope, None, None))
        self.assertFalse(scope_covers_cell(scope, None, "华北"))

    def test_overlap_is_symmetric_for_two_specific_scopes(self):
        east = normalize_scope({"regions": ["华东"]})
        packaging_specific = normalize_scope({"packaging": ["330ml罐"], "regions": ["华东"]})
        self.assertTrue(scope_overlaps(east, packaging_specific))


class PartialDeviationTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        import_ready_batch(self.service, "b1")
        release_via_review(self.service, "b1")

    def tearDown(self):
        self.service.database.close()

    def _freeze_east(self) -> str:
        self.service.open_deviation(request_id="dev", actor_id="op-gz", batch_id="b1",
                                    scope={"regions": ["华东"]}, description="标签喷码偏移",
                                    severity="medium", deviation_id="d1")
        self.service.decide(request_id="freeze", actor_id="rv-gz", batch_id="b1", decision="freeze",
                            rationale="隔离华东", scope={"regions": ["华东"]})
        explanation = self.service.explain_batch("b1")
        self.assertEqual("mixed", explanation["effective_state"]["overall"])
        cell_map = {(c["packaging"], c["region"]): c["state"]
                    for c in explanation["effective_state"]["cells"]}
        self.assertEqual("frozen", cell_map[(None, "华东")])
        self.assertEqual("released", cell_map[(None, None)])
        active = [r for r in explanation["restrictions"] if r["status"] == "active"]
        return active[0]["restriction_id"]

    def test_partial_freeze_keeps_other_regions_saleable(self):
        self._freeze_east()

    def test_dispositioner_cannot_release_restriction_themselves(self):
        restriction_id = self._freeze_east()
        self.service.disposition_deviation(request_id="disp", actor_id="rv-gz", deviation_id="d1",
                                           disposition_summary="重新贴标", evidence=["CAPA-1"])
        with self.assertRaises(PermissionDenied):
            self.service.release_restriction(request_id="rel-self", actor_id="rv-gz",
                                             restriction_id=restriction_id, evidence=["CAPA-1"],
                                             note="处置人自行解除")

    def test_restriction_release_requires_disposition_evidence_and_second_authorizer(self):
        restriction_id = self._freeze_east()
        # 未处置前，独立授权者也不能解除。
        with self.assertRaises(ConflictError):
            self.service.release_restriction(request_id="rel-early", actor_id="rv-js",
                                             restriction_id=restriction_id, evidence=["CAPA-1"],
                                             note="偏差尚未处置")
        self.service.disposition_deviation(request_id="disp", actor_id="rv-gz", deviation_id="d1",
                                           disposition_summary="重新贴标并复检", evidence=["CAPA-1"])
        # 证据列表不能为空。
        with self.assertRaises(ValidationError):
            self.service.release_restriction(request_id="rel-empty", actor_id="rv-js",
                                             restriction_id=restriction_id, evidence=[], note="无证据")
        self.service.release_restriction(request_id="rel", actor_id="rv-js",
                                         restriction_id=restriction_id, evidence=["CAPA-1"],
                                         note="复核复检报告，同意解除")
        self.service.close_deviation(request_id="close", actor_id="rv-js", deviation_id="d1")
        self.service.decide(request_id="rerelease", actor_id="rv-gz", batch_id="b1",
                            decision="release", rationale="整改完成恢复华东",
                            scope={"regions": ["华东"]})
        explanation = self.service.explain_batch("b1")
        self.assertEqual("released", explanation["effective_state"]["overall"])
        restriction = next(r for r in explanation["restrictions"] if r["restriction_id"] == restriction_id)
        self.assertEqual("released", restriction["status"])
        self.assertEqual("rv-js", restriction["released_by"])
        self.assertEqual(["CAPA-1"], restriction["release_evidence"])

    def test_released_restriction_cannot_be_released_again(self):
        restriction_id = self._freeze_east()
        self.service.disposition_deviation(request_id="disp", actor_id="rv-gz", deviation_id="d1",
                                           disposition_summary="重新贴标", evidence=["CAPA-1"])
        self.service.release_restriction(request_id="rel", actor_id="rv-js",
                                         restriction_id=restriction_id, evidence=["CAPA-1"], note="解除")
        with self.assertRaises(ConflictError):
            self.service.release_restriction(request_id="rel-again", actor_id="rv-js",
                                             restriction_id=restriction_id, evidence=["CAPA-1"],
                                             note="重复解除")


if __name__ == "__main__":
    unittest.main()

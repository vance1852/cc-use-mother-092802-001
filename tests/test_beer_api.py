"""批次放行平台 HTTP 路由测试。"""

import json
import unittest

from beer_release.api import route
from beer_release_support import build_service, import_ready_batch, release_via_review


def headers(actor: str) -> dict[str, str]:
    return {"X-Actor-Id": actor}


class BeerApiTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def tearDown(self):
        self.service.database.close()

    def test_health_reports_audit(self):
        status, payload = route(self.service, "GET", "/", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])
        self.assertIn("audit_valid", payload)

    def test_unknown_route_404(self):
        status, payload = route(self.service, "GET", "/nope", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_brand_standard_and_batch_lifecycle(self):
        import_ready_batch(self.service, "b1")
        status, payload = route(self.service, "GET", "/batches/b1/explain", None, headers("rv-gz"))
        self.assertEqual(200, status)
        self.assertEqual("std", payload["brand_standard"]["standard_id"])
        self.assertEqual("v1", payload["brand_standard"]["version"])

    def test_operator_cannot_release_via_api(self):
        import_ready_batch(self.service, "b2")
        status, payload = route(self.service, "POST", "/decisions",
                                {"request_id": "d", "batch_id": "b2", "decision": "release",
                                 "rationale": "越权放行"}, headers("op-gz"))
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_full_review_flow_via_api(self):
        import_ready_batch(self.service, "b3")
        status, payload = route(self.service, "POST", "/reviews",
                                {"request_id": "rev", "batch_id": "b3", "kind": "release_review",
                                 "note": "待复核"}, headers("op-gz"))
        self.assertEqual(201, status)
        task_id = payload["resource_id"]
        status, payload = route(self.service, "POST", f"/reviews/{task_id}/complete",
                                {"request_id": "done", "decision": "release", "rationale": "同意"},
                                headers("rv-gz"))
        self.assertEqual(201, status)
        status, payload = route(self.service, "GET", "/reviews?status=completed", None, headers("rv-gz"))
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))

    def test_partial_freeze_and_explain_endpoint(self):
        import_ready_batch(self.service, "b4")
        release_via_review(self.service, "b4")
        route(self.service, "POST", "/deviations",
              {"request_id": "dev", "batch_id": "b4", "scope": {"regions": ["华东"]},
               "description": "标签问题", "severity": "medium", "deviation_id": "d4"}, headers("op-gz"))
        status, payload = route(self.service, "POST", "/decisions",
                                {"request_id": "fz", "batch_id": "b4", "decision": "freeze",
                                 "rationale": "隔离华东", "scope": {"regions": ["华东"]}},
                                headers("rv-gz"))
        self.assertEqual(201, status)
        status, payload = route(self.service, "GET", "/batches/b4/explain", None, headers("rv-gz"))
        self.assertEqual(200, status)
        self.assertEqual("mixed", payload["effective_state"]["overall"])

    def test_shipment_requires_release_decision(self):
        import_ready_batch(self.service, "b5")
        status, payload = route(self.service, "POST", "/shipments",
                                {"request_id": "ship", "batch_id": "b5", "decision_id": "nonexistent",
                                 "quantity": 10.0}, headers("op-gz"))
        self.assertEqual(404, status)

    def test_idempotent_replay_returns_200(self):
        body = {"request_id": "std-dup", "standard_id": "std2", "version": "v1", "brand": "醇品",
                "spec": {"limits": {"alcohol": {"min": 4.0, "max": 6.0}}}}
        status_first, _ = route(self.service, "POST", "/brand-standards", body, headers("rv-gz"))
        status_second, payload = route(self.service, "POST", "/brand-standards", body, headers("rv-gz"))
        self.assertEqual(201, status_first)
        self.assertEqual(200, status_second)
        self.assertTrue(payload["replayed"])


if __name__ == "__main__":
    unittest.main()

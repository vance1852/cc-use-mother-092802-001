"""跨工厂批次放行平台的 HTTP 路由测试。"""

import unittest

from beverage_ops_foundation.api import route
from beverage_ops_foundation.release.service import ReleaseService
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database

STANDARD_BODY = {
    "request_id": "std", "standard_id": "S1", "version": 1,
    "payload": {"tests": {"t1": {"method_code": "M1"}}},
}


class ReleaseApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.base = DomainService(self.database)
        self.release = ReleaseService(self.database)
        self.base.register_organization(request_id="org", actor_id="bootstrap",
                                        organization_id="o1", name="啤酒集团")
        self.base.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="a1",
                                 display_name="管理员", role="admin", organization_id="o1")
        self.base.register_actor(request_id="q1", actor_id="a1", new_actor_id="q1",
                                 display_name="质量负责人", role="quality_lead",
                                 organization_id="o1")
        self.base.register_site(request_id="site", actor_id="a1", site_id="s1",
                                organization_id="o1", name="广州厂", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def _call(self, method, path, body=None, actor="a1"):
        return route(self.base, method, path, body or {}, {"X-Actor-Id": actor},
                     release_service=self.release)

    def test_brand_standard_roundtrip(self):
        status, body = self._call("POST", "/brand-standards", dict(STANDARD_BODY), actor="q1")
        self.assertEqual(201, status)
        status, body = self._call("GET", "/brand-standards?standard_id=S1")
        self.assertEqual(200, status)
        self.assertEqual(1, body["version"])

    def test_unknown_actor_is_rejected(self):
        status, body = self._call("POST", "/brand-standards",
                                  {"request_id": "std2", "standard_id": "S2", "version": 1,
                                   "payload": {"tests": {"t1": {"method_code": "M1"}}}},
                                  actor="nobody")
        self.assertEqual(404, status)
        self.assertEqual("not_found", body["error"])

    def test_pending_batches_route(self):
        status, body = self._call("GET", "/pending-batches")
        self.assertEqual(200, status)
        self.assertEqual([], body["items"])

    def test_explain_unknown_batch_returns_404(self):
        status, body = self._call("GET", "/batches/NOPE/explain")
        self.assertEqual(404, status)
        self.assertEqual("not_found", body["error"])

    def test_idempotent_replay_status_is_200(self):
        first_status, _ = self._call("POST", "/brand-standards", dict(STANDARD_BODY), actor="q1")
        second_status, second = self._call("POST", "/brand-standards", dict(STANDARD_BODY),
                                           actor="q1")
        self.assertEqual(201, first_status)
        self.assertEqual(200, second_status)
        self.assertTrue(second["replayed"])


if __name__ == "__main__":
    unittest.main()

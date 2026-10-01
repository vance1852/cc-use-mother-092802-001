"""幂等重放、重复导入不改隔离与重启后续办测试。"""

import tempfile
import unittest
from pathlib import Path

from beverage_ops_foundation.errors import ConflictError

from beer_release_support import build_service, import_ready_batch, release_via_review
from beer_release.service import BatchReleaseService
from beer_release.storage import ReleaseDatabase


class IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def tearDown(self):
        self.service.database.close()

    def test_same_request_replays_receipt(self):
        import_ready_batch(self.service, "b1")
        first = self.service.impose_restriction(request_id="res", actor_id="op-gz", batch_id="b1",
                                                reason="抽检复核冻结")
        second = self.service.impose_restriction(request_id="res", actor_id="op-gz", batch_id="b1",
                                                 reason="抽检复核冻结")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)

    def test_same_request_id_with_changed_payload_conflicts(self):
        import_ready_batch(self.service, "b2")
        self.service.impose_restriction(request_id="res", actor_id="op-gz", batch_id="b2",
                                        reason="原因一")
        with self.assertRaises(ConflictError):
            self.service.impose_restriction(request_id="res", actor_id="op-gz", batch_id="b2",
                                            reason="原因二")

    def test_duplicate_import_does_not_change_quarantine(self):
        import_ready_batch(self.service, "b3")
        release_via_review(self.service, "b3")
        # 先制造一个活动限制，再重复导入。
        self.service.impose_restriction(request_id="res-b3", actor_id="op-gz", batch_id="b3",
                                        reason="市场投诉冻结")
        args = dict(actor_id="op-gz", site_id="gz", import_key="B3", brand="醇品",
                    standard_id="std", standard_version="v1",
                    production_start=WINDOW_START, production_end=WINDOW_END, quantity=1000.0,
                    unit="L", material_lot_ids=["malt-gz"], packaging_lot_ids=["can-gz"],
                    calibration_ids=["filler-gz"], batch_id="b3")
        replay = self.service.import_batch(request_id="imp-b3", **args)
        self.assertTrue(replay.replayed)
        explanation = self.service.explain_batch("b3")
        active = [r for r in explanation["restrictions"] if r["status"] == "active"]
        self.assertEqual(1, len(active))
        self.assertEqual("frozen", explanation["batch"]["status"])

    def test_replay_lab_import_does_not_change_state(self):
        import_ready_batch(self.service, "b4")
        replay = self.service.record_lab_result(request_id="lab-b4", actor_id="op-gz", batch_id="b4",
                                                sample_code="b4-S1", sampled_at="2026-10-01T09:00:00Z",
                                                tests={"alcohol": 5.0, "ph": 4.3})
        self.assertTrue(replay.replayed)
        explanations = self.service.explain_batch("b4")
        self.assertEqual(1, len(explanations["lab_results"]))

    def test_same_sample_with_changed_result_conflicts(self):
        import_ready_batch(self.service, "b5")
        with self.assertRaises(ConflictError):
            self.service.record_lab_result(request_id="lab-b5-again", actor_id="op-gz", batch_id="b5",
                                           sample_code="b5-S1", sampled_at="2026-10-01T09:00:00Z",
                                           tests={"alcohol": 4.1, "ph": 4.3})


WINDOW_START = "2026-10-01T00:00:00Z"
WINDOW_END = "2026-10-01T08:00:00Z"


class RestartPersistenceTest(unittest.TestCase):
    def test_open_review_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release.sqlite3"
            service = build_service(str(path))
            import_ready_batch(service, "b1")
            service.open_review(request_id="rev", actor_id="op-gz", batch_id="b1",
                                kind="release_review", task_id="task-1")
            service.database.close()

            restarted = BatchReleaseService(ReleaseDatabase(path))
            open_tasks = restarted.list_reviews("open")
            self.assertEqual("task-1", open_tasks[0]["task_id"])
            restarted.complete_review(request_id="done", actor_id="rv-gz", task_id="task-1",
                                      decision="release", rationale="重启后续办放行")
            self.assertEqual("released", restarted.get_batch("b1")["status"])
            valid, _ = restarted.verify_audit()
            self.assertTrue(valid)
            restarted.database.close()

    def test_decisions_and_shipments_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release2.sqlite3"
            service = build_service(str(path))
            import_ready_batch(service, "b2")
            decision_id = release_via_review(service, "b2")
            service.record_shipment(request_id="ship", actor_id="op-gz", batch_id="b2",
                                    decision_id=decision_id, quantity=100.0,
                                    shipped_at="2026-10-02T00:00:00Z")
            service.database.close()

            restarted = BatchReleaseService(ReleaseDatabase(path))
            explanation = restarted.explain_batch("b2")
            self.assertTrue(explanation["decisions"][0]["historical_locked"])
            self.assertEqual(1, len(explanation["decisions"][0]["shipments"]))
            restarted.database.close()


if __name__ == "__main__":
    unittest.main()

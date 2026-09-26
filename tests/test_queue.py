import tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from src.domain import PermissionDenied
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES, priority_score


def make_service():
    tmp = tempfile.TemporaryDirectory()
    repo = Repository(str(Path(tmp.name) / "test.db"))
    return tmp, repo, Service(repo)


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.repo, self.service = make_service()

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def create(self, title, severity, quantity, threshold, ref):
        return self.service.create_item(
            {"title": title, "description": "queue test", "severity": severity,
             "quantity": quantity, "threshold": threshold, "external_ref": ref},
            "creator", "assessor")

    def test_rank_score_and_registration_order(self):
        low = self.create("低分项目", "low", 1, 10, "Q-LOW")
        high = self.create("高分项目", "high", 12, 6, "Q-HIGH")
        mid_a = self.create("同分甲", "medium", 5, 10, "Q-MID-A")
        mid_b = self.create("同分乙", "medium", 5, 10, "Q-MID-B")
        queue = self.service.queue("viewer")
        self.assertEqual([e["id"] for e in queue], [high["id"], mid_a["id"], mid_b["id"], low["id"]])
        self.assertEqual([e["rank"] for e in queue], [1, 2, 3, 4])
        self.assertEqual(queue[0]["score"], priority_score("high", 12, 6, 0))
        self.assertEqual(queue[1]["score"], queue[2]["score"])
        self.assertLess(queue[1]["id"], queue[2]["id"])
        self.assertFalse(any(e["escalated"] for e in queue))
        self.assertTrue(all(e["remaining_hours"] >= 0 for e in queue))
        for entry in queue:
            self.assertIn("priority", entry)
            self.assertIn("deadline_hours", entry)
            self.assertIn("escalation_required", entry)

    def test_open_records_raise_score(self):
        item = self.create("待办项目", "medium", 5, 10, "Q-REC")
        base = self.service.queue("viewer")[0]["score"]
        self.service.add_record(item["id"], {"kind": "evidence", "detail": "未关闭事项",
                                             "status": "open", "external_ref": "Q-REC-1"},
                                "recorder", "assessor")
        after = self.service.queue("viewer")[0]
        self.assertEqual(after["open_records"], 1)
        self.assertEqual(after["score"], priority_score("medium", 5, 10, 1))
        self.assertGreater(after["score"], base)

    def test_overdue_escalates_with_overdue_hours_and_single_audit(self):
        item = self.create("超时项目", "severe", 1, 10, "Q-LATE")
        created = datetime.fromisoformat(item["created_at"])
        now = created + timedelta(hours=10)
        queue = self.service.queue("viewer", now)
        self.assertEqual(len(queue), 1)
        entry = queue[0]
        self.assertTrue(entry["escalated"])
        self.assertEqual(entry["overdue_hours"], 6)
        self.assertEqual(entry["remaining_hours"], 0)
        self.service.queue("viewer", now)
        events = [e for e in self.service.audit("viewer", item["id"]) if e["action"] == "escalate"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["detail"]["overdue_hours"], 6)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_finished_items_leave_queue(self):
        item = self.create("办结项目", "high", 5, 10, "Q-DONE")
        current = item
        for target in ("assessed", "rejected"):
            current = self.service.transition(current["id"], target, current["version"],
                                              "reviewer", TRANSITION_ROLES[target][0])
        self.assertEqual(current["status"], STATES[-1])
        self.assertEqual(self.service.queue("viewer"), [])
        other = self.create("在办项目", "low", 1, 10, "Q-OPEN")
        queue = self.service.queue("viewer")
        self.assertEqual([e["id"] for e in queue], [other["id"]])

    def test_queue_requires_known_role(self):
        with self.assertRaises(PermissionDenied):
            self.service.queue("intruder")


if __name__ == "__main__":
    unittest.main()

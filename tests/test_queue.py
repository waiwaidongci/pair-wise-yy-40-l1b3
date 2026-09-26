import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import PermissionDenied
from src.repository import Repository
from src.service import ESCALATION_ACTION, Service
from src.rules import ENTITY, STATES, TRANSITION_ROLES


def backdate(repo, item_id, hours):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()
    with repo._lock, repo.conn:
        repo.conn.execute("UPDATE items SET created_at=? WHERE id=?", (ts, item_id))


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _create(self, ref, severity, quantity, threshold, open_records=0):
        item = self.service.create_item(
            {"title": ref, "description": "queue case", "severity": severity,
             "quantity": quantity, "threshold": threshold, "external_ref": ref},
            "creator", "assessor")
        for n in range(open_records):
            self.service.add_record(
                item["id"], {"kind": "issue", "detail": f"open-{n}",
                             "status": "open", "external_ref": f"{ref}-R{n}"},
                "recorder", "assessor")
        return self.service.get_item(item["id"], "viewer")

    def test_ranking_by_score_and_registration_tiebreak(self):
        # severe 高分项后登记；两个同分项先登记的排前
        a = self._create("A", "low", 0, 100)
        b = self._create("B", "low", 0, 100)
        c = self._create("C", "severe", 30, 10)
        queue = self.service.today_queue("viewer")
        self.assertEqual([row["id"] for row in queue], [c["id"], a["id"], b["id"]])
        self.assertEqual([row["rank"] for row in queue], [1, 2, 3])
        self.assertEqual(queue[0]["score"], queue[0]["priority"])
        self.assertGreater(queue[0]["score"], queue[1]["score"])

    def test_overdue_escalates_once_and_reports_hours(self):
        item = self._create("D", "high", 0, 1)  # high 时限 8 小时
        backdate(self.repo, item["id"], 11)
        first = self.service.today_queue("viewer", "duty-officer")
        row = first[0]
        self.assertTrue(row["escalated"])
        self.assertEqual(row["escalation_status"], "escalated")
        self.assertGreaterEqual(row["overdue_hours"], 2)
        self.assertLessEqual(row["remaining_hours"], -2)
        events = [e for e in self.repo.list_audit(item["id"])
                  if e["action"] == ESCALATION_ACTION]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], "duty-officer")
        self.assertEqual(events[0]["entity_type"], ENTITY)
        # 再次查看不重复写审计
        self.service.today_queue("viewer", "other-officer")
        events = [e for e in self.repo.list_audit(item["id"])
                  if e["action"] == ESCALATION_ACTION]
        self.assertEqual(len(events), 1)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_within_deadline_not_escalated(self):
        self._create("E", "low", 0, 1)  # low 时限 72 小时
        queue = self.service.today_queue("viewer")
        row = queue[0]
        self.assertFalse(row["escalated"])
        self.assertEqual(row["escalation_status"], "within_deadline")
        self.assertEqual(row["overdue_hours"], 0)
        self.assertGreater(row["remaining_hours"], 0)
        self.assertFalse([
            e for e in self.repo.list_audit(row["id"])
            if e["action"] == ESCALATION_ACTION])

    def test_terminal_items_leave_queue(self):
        item = self._create("F", "severe", 30, 10, open_records=0)
        self.assertEqual(len(self.service.today_queue("viewer")), 1)
        current = item
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        self.assertEqual(self.service.today_queue("viewer"), [])

    def test_queue_requires_view_permission(self):
        with self.assertRaises(PermissionDenied):
            self.service.today_queue("not-a-role")


if __name__ == "__main__":
    unittest.main()

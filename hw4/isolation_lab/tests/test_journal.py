import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from isolation_lab.journal import Journal

REQUIRED = {"scenario", "tx", "step", "sql", "value", "isolation", "t_ms", "thread"}


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "logs" / "run.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_each_line_is_one_event_with_required_fields(self):
        with Journal(self.path) as journal:
            journal.begin("01_dirty_read")
            journal.record("T1", "read", sql="SELECT 1", value=100, isolation="READ UNCOMMITTED")
            journal.record("T2", "update", sql="UPDATE t SET v = 2", isolation="READ UNCOMMITTED")
        lines = self.path.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        events = [json.loads(line) for line in lines]
        for event in events:
            self.assertEqual(set(event), REQUIRED)
        self.assertEqual(events[0]["scenario"], "01_dirty_read")
        self.assertEqual(events[0]["tx"], "T1")
        self.assertEqual(events[0]["value"], 100)
        self.assertEqual(events[0]["sql"], "SELECT 1")
        self.assertEqual(events[0]["isolation"], "READ UNCOMMITTED")
        self.assertIsNone(events[1]["value"])

    def test_timestamps_are_relative_to_scenario_start(self):
        with Journal(self.path) as journal:
            journal.begin("01_a")
            journal.record("T1", "first")
            time.sleep(0.05)
            journal.record("T1", "second")
            journal.begin("02_b")
            journal.record("T1", "first")
        events = Journal.load(self.path)
        self.assertGreaterEqual(events[0]["t_ms"], 0)
        self.assertGreaterEqual(events[1]["t_ms"] - events[0]["t_ms"], 45)
        self.assertLess(events[2]["t_ms"], events[1]["t_ms"])
        self.assertEqual(events[2]["scenario"], "02_b")

    def test_records_name_of_recording_thread(self):
        with Journal(self.path) as journal:
            journal.begin("01_a")
            worker = threading.Thread(target=journal.record, args=("T2", "write"), name="T2-worker")
            worker.start()
            worker.join(5)
        self.assertEqual(Journal.load(self.path)[0]["thread"], "T2-worker")

    def test_concurrent_records_do_not_interleave(self):
        with Journal(self.path) as journal:
            journal.begin("01_a")
            workers = [
                threading.Thread(target=lambda: [journal.record("T1", f"s{i}", value="x" * 500) for i in range(200)])
                for _ in range(4)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(10)
        self.assertEqual(len(Journal.load(self.path)), 800)

    def test_record_before_begin_fails(self):
        with Journal(self.path) as journal:
            with self.assertRaises(RuntimeError):
                journal.record("T1", "read")


if __name__ == "__main__":
    unittest.main()

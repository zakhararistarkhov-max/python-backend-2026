import tempfile
import unittest
from pathlib import Path

from isolation_lab.journal import Journal
from isolation_lab.visualizer import TimelineVisualizer

SCENARIOS = [
    ("01_dirty_read", "READ UNCOMMITTED"),
    ("02_no_dirty_read", "READ COMMITTED"),
    ("03_non_repeatable_read", "READ COMMITTED"),
    ("04_no_non_repeatable_read", "REPEATABLE READ"),
    ("05_phantom_read", "REPEATABLE READ"),
    ("06_no_phantom_read", "SERIALIZABLE"),
]


def write_sample(path):
    with Journal(path) as journal:
        for name, level in SCENARIOS:
            journal.begin(name)
            journal.record("T1", "begin", isolation=level)
            journal.record("T2", "update", sql="UPDATE acc SET bal = 200", isolation=level)
            journal.record("T1", "read", sql="SELECT bal FROM acc", value=200, isolation=level)
            journal.record("T2", "rollback", isolation=level)
            journal.record("LAB", "outcome", value=f"{name} observed", isolation=level)


class VisualizerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.journal = self.root / "run.jsonl"
        write_sample(self.journal)

    def tearDown(self):
        self.tmp.cleanup()

    def test_renders_one_png_per_scenario(self):
        out = self.root / "images"
        paths = TimelineVisualizer.from_file(self.journal).render(out)
        self.assertEqual([p.name for p in paths], [f"{name}.png" for name, _ in SCENARIOS])
        self.assertEqual(sorted(p.name for p in out.iterdir()), [f"{name}.png" for name, _ in SCENARIOS])
        for path in paths:
            data = path.read_bytes()
            self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertGreater(len(data), 5000)

    def test_title_contains_isolation_and_outcome(self):
        visualizer = TimelineVisualizer.from_file(self.journal)
        title = visualizer.title("05_phantom_read")
        self.assertIn("REPEATABLE READ", title)
        self.assertIn("05_phantom_read observed", title)

    def test_rejects_journal_without_scenarios(self):
        empty = self.root / "empty.jsonl"
        empty.write_text("")
        with self.assertRaises(ValueError):
            TimelineVisualizer.from_file(empty).render(self.root / "x")


if __name__ == "__main__":
    unittest.main()

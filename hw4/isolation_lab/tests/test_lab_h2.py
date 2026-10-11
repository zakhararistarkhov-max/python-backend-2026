import os
import tempfile
import time
import unittest
from pathlib import Path

from isolation_lab.journal import Journal

H2_JAR = os.environ.get("H2_JAR", "")
HAS_H2 = bool(H2_JAR) and Path(H2_JAR).is_file()


def reads(events, scenario, tx="T1"):
    return [e["value"] for e in events if e["scenario"] == scenario and e["tx"] == tx and e["step"].startswith("read")]


def outcome(events, scenario):
    return next(e["value"] for e in events if e["scenario"] == scenario and e["step"] == "outcome")


@unittest.skipUnless(HAS_H2, "needs JVM and H2 jar (run inside the container)")
class LabOnH2Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "run.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_six_scenarios_pass_with_observed_values(self):
        from isolation_lab.lab import IsolationLab

        self.assertEqual(IsolationLab(self.path, H2_JAR).run(), 0)
        events = Journal.load(self.path)
        self.assertEqual(
            [e["scenario"] for e in events if e["step"] == "outcome"],
            [
                "01_dirty_read",
                "02_no_dirty_read",
                "03_non_repeatable_read",
                "04_no_non_repeatable_read",
                "05_phantom_read",
                "06_no_phantom_read",
            ],
        )
        self.assertEqual(reads(events, "01_dirty_read"), [100, 200, 100])
        self.assertEqual(reads(events, "02_no_dirty_read"), [100, 100, 100])
        self.assertEqual(reads(events, "03_non_repeatable_read"), [100, 300])
        self.assertEqual(reads(events, "04_no_non_repeatable_read"), [100, 100])
        self.assertEqual(reads(events, "05_phantom_read"), [1, 1])
        self.assertEqual(reads(events, "06_no_phantom_read"), [1, 1])
        self.assertIn("phantom not reproduced on H2 2.5.252", outcome(events, "05_phantom_read"))
        for scenario in ("01_dirty_read", "02_no_dirty_read"):
            final = [e["value"] for e in events if e["scenario"] == scenario and e["step"] == "verify committed"]
            self.assertEqual(final, [100])
        for scenario in ("05_phantom_read", "06_no_phantom_read"):
            final = [e["value"] for e in events if e["scenario"] == scenario and e["step"] == "verify committed"]
            self.assertEqual(final, [2])
        isolation = {e["scenario"]: e["isolation"] for e in events if e["step"] == "begin"}
        self.assertEqual(isolation["01_dirty_read"], "READ UNCOMMITTED")
        self.assertEqual(isolation["06_no_phantom_read"], "SERIALIZABLE")

    def test_missed_signal_times_out_rolls_back_and_fails(self):
        from isolation_lab.lab import IsolationLab
        from isolation_lab.scenarios import DirtyRead

        class Stuck(DirtyRead):
            name = "99_stuck"

            def t2_body(self, tx):
                tx.execute("update", "UPDATE account SET balance = 200 WHERE id = 1")

        lab = IsolationLab(self.path, H2_JAR, scenarios=[Stuck], step_timeout=1.0)
        started = time.monotonic()
        self.assertEqual(lab.run(), 1)
        self.assertLess(time.monotonic() - started, 10)
        events = Journal.load(self.path)
        self.assertTrue(any(e["step"] == "error" and "timeout" in str(e["value"]) for e in events))
        rollbacks = {e["tx"] for e in events if e["step"] == "rollback"}
        self.assertEqual(rollbacks, {"T1", "T2"})
        scenario = lab.executed[-1]
        self.assertTrue(scenario.t1.closed and scenario.t2.closed)


if __name__ == "__main__":
    unittest.main()

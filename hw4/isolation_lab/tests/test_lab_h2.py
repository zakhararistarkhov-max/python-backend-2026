import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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
        engines = [e for e in events if e["step"] == "engine"]
        self.assertEqual(len(engines), 6)
        self.assertTrue(all(e["value"].startswith("H2 2.5.252") for e in engines))
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

    def test_phantom_outcome_uses_loaded_h2_version(self):
        from isolation_lab.lab import IsolationLab
        from isolation_lab.scenarios import PhantomRead

        lab = IsolationLab(self.path, H2_JAR, scenarios=[PhantomRead])
        with mock.patch.object(IsolationLab, "loaded_h2_version", return_value="2.5.252-test (build)"):
            self.assertEqual(lab.run(), 0)
        events = Journal.load(self.path)
        self.assertIn("phantom not reproduced on H2 2.5.252-test", outcome(events, "05_phantom_read"))
        engine = [e["value"] for e in events if e["step"] == "engine"]
        self.assertEqual(engine, ["H2 2.5.252-test (build)"])

    def test_unexpected_h2_version_fails_at_startup(self):
        from isolation_lab.lab import IsolationLab

        lab = IsolationLab(self.path, H2_JAR)
        with mock.patch.object(IsolationLab, "loaded_h2_version", return_value="2.3.232 (2024-08-11)"):
            with self.assertRaisesRegex(RuntimeError, "2.3.232"):
                lab.run()
        self.assertEqual(lab.executed, [])

    def test_stuck_cleanup_fails_the_run(self):
        from isolation_lab.lab import IsolationLab
        from isolation_lab.scenarios import DirtyRead

        class SlowCleanup(DirtyRead):
            name = "98_slow_cleanup"

            def close_all(self):
                super().close_all()
                time.sleep(3)

        lab = IsolationLab(self.path, H2_JAR, scenarios=[SlowCleanup, DirtyRead], step_timeout=1.0)
        self.assertEqual(lab.run(), 1)
        self.assertTrue(lab.stuck)
        self.assertEqual(len(lab.executed), 1)

    def test_failing_statement_is_journaled(self):
        from isolation_lab.lab import IsolationLab
        from isolation_lab.scenarios import DirtyRead

        class BrokenUpdate(DirtyRead):
            name = "97_broken_update"

            def t2_body(self, tx):
                self.wait("t1_read_before")
                tx.execute("update", "UPDATE missing_table SET balance = 1")

        lab = IsolationLab(self.path, H2_JAR, scenarios=[BrokenUpdate], step_timeout=1.0)
        self.assertEqual(lab.run(), 1)
        failed = [e for e in Journal.load(self.path) if e["step"] == "update failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["tx"], "T2")
        self.assertEqual(failed[0]["sql"], "UPDATE missing_table SET balance = 1")
        self.assertIn("MISSING_TABLE", failed[0]["value"].upper())

    def test_missing_committed_row_fails(self):
        from isolation_lab.lab import IsolationLab
        from isolation_lab.scenarios import DirtyRead

        class MissingRow(DirtyRead):
            name = "96_missing_row"

            def check(self):
                self.committed("SELECT balance FROM account WHERE id = 99")

        lab = IsolationLab(self.path, H2_JAR, scenarios=[MissingRow], step_timeout=1.0)
        self.assertEqual(lab.run(), 1)
        errors = [e["value"] for e in Journal.load(self.path) if e["step"] == "error"]
        self.assertTrue(any("no rows" in str(value) for value in errors))


if __name__ == "__main__":
    unittest.main()

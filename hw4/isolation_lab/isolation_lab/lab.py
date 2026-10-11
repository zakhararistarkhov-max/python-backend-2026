import time
from pathlib import Path

import jpype

from isolation_lab.journal import Journal
from isolation_lab.scenarios import SCENARIOS

H2_VERSION = "2.5.252"


class IsolationLab:
    """Runs the isolation scenarios against H2 through JPype and JDBC."""

    def __init__(self, journal_path, jar, scenarios=SCENARIOS, step_timeout=10.0, lock_timeout_ms=3000):
        self.journal_path = Path(journal_path)
        self.jar = Path(jar)
        self.scenarios = scenarios
        self.step_timeout = step_timeout
        self.lock_timeout_ms = lock_timeout_ms
        self.executed = []
        self.stuck = False

    def start_jvm(self):
        if not jpype.isJVMStarted():
            if not self.jar.is_file():
                raise FileNotFoundError(f"H2 jar not found: {self.jar}")
            jpype.startJVM(classpath=[str(self.jar)], convertStrings=True)
        self.h2_version = self.loaded_h2_version()
        if not self.h2_version.startswith(H2_VERSION):
            raise RuntimeError(f"expected H2 {H2_VERSION}, loaded {self.h2_version}")
        self.driver_manager = jpype.JClass("java.sql.DriverManager")

    def loaded_h2_version(self):
        return str(jpype.JClass("org.h2.engine.Constants").FULL_VERSION)

    def connect(self, database):
        url = f"jdbc:h2:mem:{database};LOCK_TIMEOUT={self.lock_timeout_ms}"
        return self.driver_manager.getConnection(url, "sa", "")

    def run(self):
        self.start_jvm()
        print(f"H2 {self.h2_version}, journal {self.journal_path}", flush=True)
        with Journal(self.journal_path) as journal:
            for scenario_class in self.scenarios:
                scenario = scenario_class(journal, self.connect, self.step_timeout, self.h2_version)
                self.executed.append(scenario)
                started = time.monotonic()
                try:
                    outcome = scenario.run()
                except Exception as exc:
                    self.stuck = scenario.stuck
                    print(f"FAIL {scenario.name} [{scenario.isolation}]: {type(exc).__name__}: {exc}", flush=True)
                    return 1
                if scenario.stuck:
                    self.stuck = True
                    print(f"FAIL {scenario.name} [{scenario.isolation}]: cleanup did not finish in time", flush=True)
                    return 1
                elapsed = (time.monotonic() - started) * 1000
                print(f"PASS {scenario.name} [{scenario.isolation}] {outcome} ({elapsed:.0f} ms)", flush=True)
        return 0

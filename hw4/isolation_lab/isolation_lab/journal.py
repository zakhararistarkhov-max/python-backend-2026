import json
import threading
import time
from pathlib import Path


class Journal:
    """Thread-safe JSONL event journal with per-scenario monotonic timestamps."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("w", encoding="utf-8")
        self._lock = threading.Lock()
        self._scenario = None
        self._start = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        with self._lock:
            if not self._file.closed:
                self._file.close()

    def begin(self, scenario):
        with self._lock:
            self._scenario = scenario
            self._start = time.monotonic()

    def record(self, tx, step, sql=None, value=None, isolation=None):
        with self._lock:
            if self._scenario is None:
                raise RuntimeError("journal.begin() must be called before record()")
            event = {
                "scenario": self._scenario,
                "tx": tx,
                "step": step,
                "sql": sql,
                "value": value,
                "isolation": isolation,
                "t_ms": round((time.monotonic() - self._start) * 1000, 3),
                "thread": threading.current_thread().name,
            }
            self._file.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._file.flush()
        return event

    @staticmethod
    def load(path):
        with Path(path).open(encoding="utf-8") as file:
            return [json.loads(line) for line in file if line.strip()]

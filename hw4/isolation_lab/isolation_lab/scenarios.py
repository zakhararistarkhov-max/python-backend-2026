import threading
import time
import uuid

ISOLATION_LEVELS = {
    "READ UNCOMMITTED": 1,
    "READ COMMITTED": 2,
    "REPEATABLE READ": 4,
    "SERIALIZABLE": 8,
}
LEVEL_NAMES = {code: name for name, code in ISOLATION_LEVELS.items()}


class ScenarioFailed(Exception):
    """Raised when a scenario times out, errors or observes unexpected data."""


class Tx:
    """One JDBC connection running a single explicit transaction."""

    def __init__(self, name, connection, isolation, journal):
        self.name = name
        self.connection = connection
        self.isolation = isolation
        self.journal = journal
        self.finished = False
        self.closed = False

    def log(self, step, sql=None, value=None):
        self.journal.record(self.name, step, sql=sql, value=value, isolation=self.isolation)

    def begin(self):
        self.connection.setAutoCommit(False)
        self.connection.setTransactionIsolation(ISOLATION_LEVELS[self.isolation])
        actual = LEVEL_NAMES.get(int(self.connection.getTransactionIsolation()))
        if actual != self.isolation:
            raise ScenarioFailed(f"{self.name}: requested {self.isolation}, driver reports {actual}")
        self.log("begin", value=actual)

    def query_int(self, step, sql):
        statement = self.connection.createStatement()
        try:
            result = statement.executeQuery(sql)
            if not result.next():
                raise ScenarioFailed(f"{self.name}: no rows for {sql}")
            value = int(result.getInt(1))
        finally:
            statement.close()
        self.log(step, sql, value)
        return value

    def execute(self, step, sql):
        statement = self.connection.createStatement()
        try:
            rows = int(statement.executeUpdate(sql))
        finally:
            statement.close()
        self.log(step, sql, f"{rows} row(s)")
        return rows

    def commit(self):
        self.connection.commit()
        self.finished = True
        self.log("commit", "COMMIT")

    def rollback(self):
        self.connection.rollback()
        self.finished = True
        self.log("rollback", "ROLLBACK")

    def close(self):
        if self.closed:
            return
        try:
            if not self.finished:
                self.rollback()
        finally:
            self.connection.close()
            self.closed = True


class Scenario:
    """Two concurrent transactions T1 and T2 coordinated step by step."""

    name = ""
    isolation = ""
    setup_sql = ()
    outcome = ""

    def __init__(self, journal, connect, step_timeout=10.0):
        self.journal = journal
        self.connect = connect
        self.step_timeout = step_timeout
        self.database = f"{self.name}_{uuid.uuid4().hex[:8]}"
        self.observed = {}
        self.errors = []
        self.abort = threading.Event()
        self.flags = {}
        self.flags_lock = threading.Lock()
        self.setup = self.t1 = self.t2 = None
        self.stuck = False

    def flag(self, name):
        with self.flags_lock:
            return self.flags.setdefault(name, threading.Event())

    def signal(self, name):
        self.flag(name).set()

    def wait(self, name):
        flag = self.flag(name)
        deadline = time.monotonic() + self.step_timeout
        while not flag.wait(0.02):
            if self.abort.is_set():
                raise ScenarioFailed(f"aborted while waiting for {name}")
            if time.monotonic() > deadline:
                raise ScenarioFailed(f"timeout after {self.step_timeout}s waiting for {name}")

    def expect(self, label, actual, expected):
        if actual != expected:
            raise ScenarioFailed(f"{label}: expected {expected!r}, observed {actual!r}")

    def lab(self, step, sql=None, value=None, isolation=None):
        self.journal.record("LAB", step, sql=sql, value=value, isolation=isolation)

    def committed(self, sql):
        statement = self.setup.createStatement()
        try:
            result = statement.executeQuery(sql)
            result.next()
            value = int(result.getInt(1))
        finally:
            statement.close()
        self.lab("verify committed", sql, value)
        return value

    def drive(self, body, tx):
        try:
            tx.begin()
            body(tx)
        except BaseException as exc:
            self.errors.append(f"{tx.name}: {type(exc).__name__}: {exc}")
            self.abort.set()

    def run(self):
        self.journal.begin(self.name)
        try:
            self.setup = self.connect(self.database)
            statement = self.setup.createStatement()
            for sql in self.setup_sql:
                statement.executeUpdate(sql)
                self.lab("setup", sql)
            statement.close()
            self.t1 = Tx("T1", self.connect(self.database), self.isolation, self.journal)
            self.t2 = Tx("T2", self.connect(self.database), self.isolation, self.journal)
            threads = [
                threading.Thread(target=self.drive, args=(self.t1_body, self.t1), name=f"T1-{self.name}", daemon=True),
                threading.Thread(target=self.drive, args=(self.t2_body, self.t2), name=f"T2-{self.name}", daemon=True),
            ]
            for thread in threads:
                thread.start()
            deadline = time.monotonic() + self.step_timeout * 4
            for thread in threads:
                thread.join(max(0.0, deadline - time.monotonic()))
                if thread.is_alive():
                    self.stuck = True
                    self.abort.set()
                    self.errors.append(f"{thread.name} did not finish in time")
            if self.errors:
                raise ScenarioFailed("; ".join(self.errors))
            self.check()
            result = self.outcome.format(**self.observed)
            self.lab("outcome", value=result, isolation=self.isolation)
            return result
        except BaseException as exc:
            self.lab("error", value=f"{type(exc).__name__}: {exc}", isolation=self.isolation)
            raise
        finally:
            self.cleanup()

    def cleanup(self):
        worker = threading.Thread(target=self.close_all, name=f"cleanup-{self.name}", daemon=True)
        worker.start()
        worker.join(self.step_timeout)
        if worker.is_alive():
            self.stuck = True

    def close_all(self):
        for tx in (self.t1, self.t2):
            if tx is not None:
                try:
                    tx.close()
                except Exception as exc:
                    self.lab("close failed", value=f"{tx.name}: {exc}")
        if self.setup is not None:
            self.setup.close()

    def t1_body(self, tx):
        raise NotImplementedError

    def t2_body(self, tx):
        raise NotImplementedError

    def check(self):
        raise NotImplementedError


class DirtyReadFlow(Scenario):
    """T1 reads a row while T2 holds an uncommitted update that it later rolls back."""

    setup_sql = (
        "CREATE TABLE account (id INT PRIMARY KEY, balance INT NOT NULL)",
        "INSERT INTO account VALUES (1, 100)",
    )
    read_sql = "SELECT balance FROM account WHERE id = 1"
    during = None

    def t1_body(self, tx):
        self.observed["before"] = tx.query_int("read before", self.read_sql)
        self.signal("t1_read_before")
        self.wait("t2_updated")
        self.observed["during"] = tx.query_int("read during", self.read_sql)
        self.signal("t1_read_during")
        self.wait("t2_rolled_back")
        self.observed["after"] = tx.query_int("read after", self.read_sql)
        tx.commit()

    def t2_body(self, tx):
        self.wait("t1_read_before")
        tx.execute("update (uncommitted)", "UPDATE account SET balance = 200 WHERE id = 1")
        self.signal("t2_updated")
        self.wait("t1_read_during")
        tx.rollback()
        self.signal("t2_rolled_back")

    def check(self):
        self.expect("T1 read before", self.observed["before"], 100)
        self.expect("T1 read during T2 update", self.observed["during"], self.during)
        self.expect("T1 read after T2 rollback", self.observed["after"], 100)
        self.observed["committed"] = self.committed(self.read_sql)
        self.expect("committed balance after rollback", self.observed["committed"], 100)


class DirtyRead(DirtyReadFlow):
    """Dirty read under READ UNCOMMITTED."""

    name = "01_dirty_read"
    isolation = "READ UNCOMMITTED"
    during = 200
    outcome = "dirty read observed: {before} -> {during} (uncommitted) -> {after} after rollback"


class NoDirtyRead(DirtyReadFlow):
    """No dirty read under READ COMMITTED."""

    name = "02_no_dirty_read"
    isolation = "READ COMMITTED"
    during = 100
    outcome = "no dirty read: {before} -> {during} while T2 held uncommitted 200"


class NonRepeatableReadFlow(Scenario):
    """T1 reads a row twice while T2 updates and commits it in between."""

    setup_sql = DirtyReadFlow.setup_sql
    read_sql = DirtyReadFlow.read_sql
    second = None

    def t1_body(self, tx):
        self.observed["first"] = tx.query_int("read first", self.read_sql)
        self.signal("t1_read_first")
        self.wait("t2_committed")
        self.observed["second"] = tx.query_int("read again", self.read_sql)
        tx.commit()

    def t2_body(self, tx):
        self.wait("t1_read_first")
        tx.execute("update", "UPDATE account SET balance = 300 WHERE id = 1")
        tx.commit()
        self.signal("t2_committed")

    def check(self):
        self.expect("T1 first read", self.observed["first"], 100)
        self.expect("T1 second read", self.observed["second"], self.second)
        self.observed["committed"] = self.committed(self.read_sql)
        self.expect("committed balance", self.observed["committed"], 300)


class NonRepeatableRead(NonRepeatableReadFlow):
    """Non-repeatable read under READ COMMITTED."""

    name = "03_non_repeatable_read"
    isolation = "READ COMMITTED"
    second = 300
    outcome = "non-repeatable read observed: {first} -> {second}"


class NoNonRepeatableRead(NonRepeatableReadFlow):
    """No non-repeatable read under REPEATABLE READ."""

    name = "04_no_non_repeatable_read"
    isolation = "REPEATABLE READ"
    second = 100
    outcome = "no non-repeatable read: {first} -> {second} (committed {committed})"


class PhantomFlow(Scenario):
    """T1 counts rows twice while T2 inserts a matching row and commits."""

    setup_sql = (
        "CREATE TABLE item (id INT PRIMARY KEY, category VARCHAR(16) NOT NULL)",
        "INSERT INTO item VALUES (1, 'book')",
    )
    count_sql = "SELECT COUNT(*) FROM item WHERE category = 'book'"

    def t1_body(self, tx):
        self.observed["first"] = tx.query_int("read count", self.count_sql)
        self.signal("t1_counted")
        self.wait("t2_committed")
        self.observed["second"] = tx.query_int("read count again", self.count_sql)
        tx.commit()

    def t2_body(self, tx):
        self.wait("t1_counted")
        tx.execute("insert", "INSERT INTO item VALUES (2, 'book')")
        tx.commit()
        self.signal("t2_committed")

    def check(self):
        self.expect("T1 first count", self.observed["first"], 1)
        self.expect("T1 second count", self.observed["second"], 1)
        self.observed["committed"] = self.committed(self.count_sql)
        self.expect("committed count", self.observed["committed"], 2)


class PhantomRead(PhantomFlow):
    """Phantom read attempt under REPEATABLE READ (H2 does not produce it)."""

    name = "05_phantom_read"
    isolation = "REPEATABLE READ"
    outcome = "phantom not reproduced on H2 2.5.252: count {first} -> {second} (committed {committed})"


class NoPhantomRead(PhantomFlow):
    """No phantom read under SERIALIZABLE."""

    name = "06_no_phantom_read"
    isolation = "SERIALIZABLE"
    outcome = "no phantom read: count {first} -> {second} (committed {committed})"


SCENARIOS = (DirtyRead, NoDirtyRead, NonRepeatableRead, NoNonRepeatableRead, PhantomRead, NoPhantomRead)

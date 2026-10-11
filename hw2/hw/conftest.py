import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry

TEST_DB_DIR = None
if not os.environ.get("DATABASE_URL"):
    TEST_DB_DIR = tempfile.mkdtemp(prefix="shop-tests-")
    os.environ["DATABASE_URL"] = f"sqlite:///{Path(TEST_DB_DIR) / 'shop.db'}"

from shop_api import main  # noqa: E402
from shop_api.database import create_database_engine  # noqa: E402
from shop_api.metrics import ShopMetrics  # noqa: E402
from shop_api.store import ShopStore  # noqa: E402


class SnapshotFailingMetrics(ShopMetrics):
    """Metrics that can fail while a cart snapshot is built inside a transaction."""

    def __init__(self) -> None:
        super().__init__(CollectorRegistry())
        self.failing = False

    def cart_returned(self, cart: object) -> None:
        if self.failing:
            raise RuntimeError("snapshot failed")
        super().cart_returned(cart)


def pytest_unconfigure(config: pytest.Config) -> None:
    main.store.dispose()
    if TEST_DB_DIR is not None:
        shutil.rmtree(TEST_DB_DIR, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def app_schema() -> None:
    main.store.create_schema()


@pytest.fixture()
def make_store(tmp_path: Path) -> Iterator:
    stores: list[ShopStore] = []

    def make(metrics: ShopMetrics | None = None, name: str | None = None) -> ShopStore:
        path = tmp_path / (name or f"store{len(stores)}.db")
        engine = create_database_engine(f"sqlite:///{path}")
        store = ShopStore(engine) if metrics is None else ShopStore(engine, metrics)
        store.create_schema()
        stores.append(store)
        return store

    yield make
    for store in stores:
        store.dispose()


@pytest.fixture()
def store(make_store) -> ShopStore:
    return make_store()


@pytest.fixture()
def snapshot_metrics() -> SnapshotFailingMetrics:
    return SnapshotFailingMetrics()

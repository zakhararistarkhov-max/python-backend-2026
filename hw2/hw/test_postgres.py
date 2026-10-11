import math
import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http import HTTPStatus
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shop_api import main
from shop_api.database import create_database_engine
from shop_api.metrics import ShopMetrics
from shop_api.models import CartItemRow, CartRow, ItemRow
from shop_api.store import PriceOverflowError, ShopStore

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not set")


class SnapshotFailingMetrics(ShopMetrics):
    """Metrics that can fail while a cart snapshot is built inside a transaction."""

    def __init__(self) -> None:
        super().__init__(CollectorRegistry())
        self.failing = False

    def cart_returned(self, cart: object) -> None:
        if self.failing:
            raise RuntimeError("snapshot failed")
        super().cart_returned(cart)


@pytest.fixture()
def engine() -> Iterator[Engine]:
    schema = f"shop_test_{uuid4().hex}"
    admin = create_engine(POSTGRES_URL)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_database_engine(
        POSTGRES_URL,
        connect_args={"options": f"-csearch_path={schema}"},
        pool_size=20,
    )
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture()
def metrics() -> SnapshotFailingMetrics:
    return SnapshotFailingMetrics()


@pytest.fixture()
def store(engine: Engine, metrics: SnapshotFailingMetrics) -> ShopStore:
    store = ShopStore(engine, metrics)
    store.create_schema()
    return store


def count(engine: Engine, model: type) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model))


def test_tables_live_in_isolated_schema(engine: Engine, store: ShopStore) -> None:
    with engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
        tables = set(
            connection.scalars(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema = :s"),
                {"s": schema},
            )
        )

    assert schema.startswith("shop_test_")
    assert tables == {"items", "carts", "cart_items"}


def test_ids_are_generated_by_database(store: ShopStore) -> None:
    items = [store.create_item(f"i{i}", 1.0).id for i in range(3)]
    carts = [store.create_cart() for _ in range(3)]

    assert items == [1, 2, 3]
    assert carts == [1, 2, 3]


def test_failed_add_rolls_back_without_partial_rows(
    engine: Engine, store: ShopStore, metrics: SnapshotFailingMetrics
) -> None:
    cart_id = store.create_cart()
    item_id = store.create_item("x", 1.0).id

    metrics.failing = True
    with pytest.raises(RuntimeError):
        store.add_to_cart(cart_id, item_id)
    metrics.failing = False

    assert count(engine, CartItemRow) == 0
    assert store.get_cart(cart_id).items == []
    assert store.add_to_cart(cart_id, item_id).items[0].quantity == 1


def test_failed_create_leaves_no_rows(engine: Engine, store: ShopStore) -> None:
    with pytest.raises(IntegrityError):
        store.create_item("bad", -1.0)

    assert count(engine, ItemRow) == 0
    assert store.create_item("good", 1.0).id >= 1


def test_concurrent_adds_have_no_lost_updates(store: ShopStore) -> None:
    cart_id = store.create_cart()
    first = store.create_item("a", 1.0).id
    second = store.create_item("b", 2.0).id
    workers, per_worker = 8, 50

    def add_many(worker: int) -> None:
        for _ in range(per_worker):
            store.add_to_cart(cart_id, first if worker % 2 else second)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(add_many, range(workers)))

    cart = store.get_cart(cart_id)
    quantities = {line.id: line.quantity for line in cart.items}
    assert quantities == {first: workers * per_worker // 2, second: workers * per_worker // 2}
    assert cart.price == 200 * 1.0 + 200 * 2.0


def race(actions: list[Callable[[], object]]) -> list[bool]:
    barrier = Barrier(len(actions))

    def run(action: Callable[[], object]) -> bool:
        barrier.wait()
        try:
            action()
        except PriceOverflowError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        return list(pool.map(run, actions))


def test_concurrent_overflow_attempts_keep_cart_finite(store: ShopStore) -> None:
    huge = store.create_item("huge", 1e308).id

    for _ in range(20):
        cart_id = store.create_cart()

        accepted = race([partial(store.add_to_cart, cart_id, huge)] * 8)

        cart = store.get_cart(cart_id)
        assert accepted.count(True) == 1
        assert cart.items[0].quantity == 1
        assert cart.price == 1e308


@pytest.mark.parametrize("raised_in_cart", [True, False])
def test_concurrent_price_updates_and_adds_keep_carts_finite(
    store: ShopStore, raised_in_cart: bool
) -> None:
    for _ in range(20):
        cart_id = store.create_cart()
        raised = store.create_item("raised", 1.0).id
        huge = store.create_item("huge", 1e308).id
        store.add_to_cart(cart_id, raised if raised_in_cart else huge)
        added = huge if raised_in_cart else raised

        race(
            [
                partial(store.update_item, raised, price=1e308),
                partial(store.add_to_cart, cart_id, added),
            ]
            * 4
        )

        assert math.isfinite(store.get_cart(cart_id).price)


def test_check_constraints_reject_invalid_rows(engine: Engine, store: ShopStore) -> None:
    cart_id = store.create_cart()
    item_id = store.create_item("x", 1.0).id

    with pytest.raises(IntegrityError, match="items_price_non_negative"):
        with Session(engine) as session, session.begin():
            session.add(ItemRow(name="bad", price=-0.5))
    with pytest.raises(IntegrityError, match="cart_items_quantity_positive"):
        with Session(engine) as session, session.begin():
            session.add(CartItemRow(cart_id=cart_id, item_id=item_id, quantity=0))
    with pytest.raises(IntegrityError):
        with Session(engine) as session, session.begin():
            session.add(CartItemRow(cart_id=cart_id + 100, item_id=item_id, quantity=1))

    assert count(engine, ItemRow) == 1
    assert count(engine, CartItemRow) == 0


def test_deleted_items_are_unavailable_and_excluded_from_price(
    engine: Engine, store: ShopStore
) -> None:
    cart_id = store.create_cart()
    kept = store.create_item("kept", 1.25).id
    removed = store.create_item("removed", 100.0).id
    for item_id in (kept, kept, removed):
        store.add_to_cart(cart_id, item_id)

    store.delete_item(removed)
    cart = store.get_cart(cart_id)

    assert cart.price == 2.5
    assert [(line.id, line.quantity, line.available) for line in cart.items] == [
        (kept, 2, True),
        (removed, 1, False),
    ]
    with Session(engine) as session:
        assert session.get(ItemRow, removed).deleted is True
        assert session.get(CartRow, cart_id) is not None


def test_http_contract_on_postgres(monkeypatch: pytest.MonkeyPatch, store: ShopStore) -> None:
    monkeypatch.setattr(main, "store", store)
    client = TestClient(main.app)

    created = client.post("/item", json={"name": "huge", "price": 1e308})
    item_id = created.json()["id"]
    cart = client.post("/cart")
    cart_id = cart.json()["id"]

    assert created.headers["location"] == f"/item/{item_id}"
    assert cart.headers["location"] == f"/cart/{cart_id}"
    assert client.post(f"/cart/{cart_id}/add/{item_id}").status_code == HTTPStatus.OK
    assert client.post(f"/cart/{cart_id}/add/{item_id}").status_code == HTTPStatus.CONFLICT
    assert client.get("/cart", params={"min_quantity": 1}).json()[0]["price"] == 1e308
    assert client.delete(f"/item/{item_id}").status_code == HTTPStatus.OK
    assert client.put(f"/item/{item_id}", json={"name": "y", "price": 1.0}).status_code == (
        HTTPStatus.NOT_MODIFIED
    )
    assert client.get(f"/item/{2**70}").status_code == HTTPStatus.NOT_FOUND
    assert client.get(f"/cart/{cart_id}").json() == {
        "id": cart_id,
        "items": [{"id": item_id, "name": "huge", "quantity": 1, "available": False}],
        "price": 0.0,
    }

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shop_api import main
from shop_api.chat import ChatRoomManager
from shop_api.database import create_database_engine
from shop_api.metrics import ShopMetrics
from shop_api.models import CartItemRow
from shop_api.store import ShopStore


def test_lifespan_creates_schema_and_disposes_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'lifecycle.db'}")
    store = ShopStore(engine, ShopMetrics(CollectorRegistry()))
    dispose = Mock(wraps=engine.dispose)
    monkeypatch.setattr(engine, "dispose", dispose)
    monkeypatch.setattr(main, "store", store)
    assert inspect(engine).get_table_names() == []

    try:
        with TestClient(main.app) as client:
            assert set(inspect(engine).get_table_names()) == {"items", "carts", "cart_items"}
            response = client.post("/item", json={"name": "persisted", "price": 2.5})
            assert response.status_code == 201
            item_id = response.json()["id"]
            dispose.assert_not_called()

        dispose.assert_called_once_with()
        with TestClient(main.app) as client:
            assert client.get(f"/item/{item_id}").json() == {
                "id": item_id,
                "name": "persisted",
                "price": 2.5,
                "deleted": False,
            }
        assert dispose.call_count == 2
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [WebSocketDisconnect(code=1001), RuntimeError("receive failed")]
)
async def test_chat_cleans_up_after_receive_error(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    rooms = ChatRoomManager()
    monkeypatch.setattr(main, "chat_rooms", rooms)
    socket = AsyncMock(spec=WebSocket)
    socket.receive.side_effect = failure

    if isinstance(failure, WebSocketDisconnect):
        await main.chat(socket, "room")
    else:
        with pytest.raises(RuntimeError, match="receive failed"):
            await main.chat(socket, "room")

    socket.accept.assert_awaited_once_with()
    assert "room" not in rooms._rooms
    socket.send_text.assert_not_awaited()


def test_unsupported_database_dialect() -> None:
    engine = Mock(spec=Engine)
    engine.dialect = Mock()
    engine.dialect.name = "mysql"

    with pytest.raises(ValueError, match="Unsupported database dialect: mysql"):
        ShopStore(engine)

    engine.connect.assert_not_called()


@pytest.mark.parametrize("method", ["cart_add_rejected", "stage"])
def test_unknown_metric_label_is_rejected(method: str) -> None:
    registry = CollectorRegistry()
    metrics = ShopMetrics(registry)

    with pytest.raises(ValueError, match="unknown"):
        getattr(metrics, method)("unknown")

    for family in registry.collect():
        for sample in family.samples:
            assert "unknown" not in sample.labels.values()


def test_sqlite_connection_options_and_foreign_keys(tmp_path: Path) -> None:
    engine = create_database_engine(
        f"sqlite:///{tmp_path / 'options.db'}", connect_args={"timeout": 1.25}
    )
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 1250
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("missing", ["cart", "item"])
def test_sqlite_rejects_orphan_cart_lines(store: ShopStore, missing: str) -> None:
    cart_id = store.create_cart()
    item_id = store.create_item("valid", 1.0).id
    with pytest.raises(IntegrityError):
        with Session(store._engine) as session, session.begin():
            session.add(
                CartItemRow(
                    cart_id=cart_id + 1 if missing == "cart" else cart_id,
                    item_id=item_id + 1 if missing == "item" else item_id,
                    quantity=1,
                )
            )

    with Session(store._engine) as session:
        assert session.scalars(select(CartItemRow)).all() == []
    assert store.get_cart(cart_id).items == []

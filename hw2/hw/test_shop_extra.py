import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from shop_api import main
from shop_api.chat import ChatRoomManager
from shop_api.database import database_url
from shop_api.store import ShopStore

MESSAGE_RE = re.compile(r"^(\S+) :: (.*)$")


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, store: ShopStore) -> TestClient:
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "chat_rooms", ChatRoomManager())
    return TestClient(main.app)


def create_item(client: TestClient, name: str = "item", price: float = 10.0) -> int:
    response = client.post("/item", json={"name": name, "price": price})
    assert response.status_code == HTTPStatus.CREATED
    return response.json()["id"]


def create_cart(client: TestClient) -> int:
    response = client.post("/cart")
    assert response.status_code == HTTPStatus.CREATED
    return response.json()["id"]


def parse(message: str) -> tuple[str, str]:
    match = MESSAGE_RE.match(message)
    assert match is not None, message
    return match.group(1), match.group(2)


def test_post_cart_location_points_to_cart(client: TestClient) -> None:
    response = client.post("/cart")
    cart_id = response.json()["id"]

    assert response.headers["location"] == f"/cart/{cart_id}"
    assert client.get(response.headers["location"]).json() == {
        "id": cart_id,
        "items": [],
        "price": 0.0,
    }


def test_post_item_returns_full_item_with_location(client: TestClient) -> None:
    response = client.post("/item", json={"name": "milk", "price": 1.5})
    data = response.json()

    assert response.status_code == HTTPStatus.CREATED
    assert data == {"id": data["id"], "name": "milk", "price": 1.5, "deleted": False}
    assert response.headers["location"] == f"/item/{data['id']}"


def test_server_generates_unique_ids(client: TestClient) -> None:
    ids = {create_item(client) for _ in range(5)}
    cart_ids = {create_cart(client) for _ in range(5)}

    assert len(ids) == 5
    assert len(cart_ids) == 5


@pytest.mark.parametrize(
    "body",
    [
        {"id": 100, "name": "x", "price": 1.0},
        {"name": "x", "price": 1.0, "deleted": True},
        {"name": "x"},
        {"price": 1.0},
        {"name": "x", "price": -0.01},
        {"name": "x", "price": "abc"},
        {"name": None, "price": 1.0},
    ],
)
def test_post_item_rejects_invalid_body(client: TestClient, body: dict) -> None:
    response = client.post("/item", json=body)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert client.get("/item", params={"show_deleted": True}).json() == []


def test_post_item_rejects_non_finite_price(client: TestClient) -> None:
    response = client.post(
        "/item",
        content=b'{"name": "x", "price": 1e400}',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize("price", [True, False, "5", "1.5"])
@pytest.mark.parametrize("method", ["post", "put", "patch"])
def test_item_price_rejects_bools_and_strings(
    client: TestClient, method: str, price: object
) -> None:
    item_id = create_item(client, "x", 3.0)
    path = "/item" if method == "post" else f"/item/{item_id}"

    response = client.request(method, path, json={"name": "y", "price": price})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert client.get("/item").json() == [
        {"id": item_id, "name": "x", "price": 3.0, "deleted": False}
    ]


@pytest.mark.parametrize("method", ["put", "patch"])
def test_item_price_accepts_integers(client: TestClient, method: str) -> None:
    item_id = create_item(client, "x", 3.0)

    response = client.request(method, f"/item/{item_id}", json={"name": "x", "price": 7})

    assert response.status_code == HTTPStatus.OK
    assert response.json()["price"] == 7.0


def test_post_item_accepts_zero_price(client: TestClient) -> None:
    response = client.post("/item", json={"name": "free", "price": 0})

    assert response.status_code == HTTPStatus.CREATED
    assert response.json()["price"] == 0.0


@pytest.mark.parametrize(
    "query",
    [
        {"min_price": "nan"},
        {"max_price": "abc"},
        {"offset": "1.5"},
        {"limit": "x"},
        {"show_deleted": "maybe"},
    ],
)
def test_get_item_list_rejects_bad_query(client: TestClient, query: dict) -> None:
    assert client.get("/item", params=query).status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize(
    "query",
    [{"min_price": "nan"}, {"min_quantity": "1.5"}, {"limit": 0}],
)
def test_get_cart_list_rejects_bad_query(client: TestClient, query: dict) -> None:
    assert client.get("/cart", params=query).status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/item/999"),
        ("get", "/cart/999"),
        ("delete", "/item/999"),
        ("post", "/cart/999/add/1"),
    ],
)
def test_missing_resources_are_not_found(client: TestClient, method: str, path: str) -> None:
    create_item(client)

    assert client.request(method, path).status_code == HTTPStatus.NOT_FOUND


def test_add_missing_item_to_existing_cart_is_not_found(client: TestClient) -> None:
    cart_id = create_cart(client)

    assert client.post(f"/cart/{cart_id}/add/999").status_code == HTTPStatus.NOT_FOUND
    assert client.get(f"/cart/{cart_id}").json()["items"] == []


def test_add_deleted_item_to_cart_is_not_found(client: TestClient) -> None:
    cart_id = create_cart(client)
    item_id = create_item(client)
    client.delete(f"/item/{item_id}")

    assert client.post(f"/cart/{cart_id}/add/{item_id}").status_code == HTTPStatus.NOT_FOUND


def test_put_missing_item_is_not_found_and_does_not_create(client: TestClient) -> None:
    response = client.put("/item/42", json={"name": "x", "price": 1.0})

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert client.get("/item/42").status_code == HTTPStatus.NOT_FOUND


def test_put_item_rejects_extra_fields(client: TestClient) -> None:
    item_id = create_item(client)

    response = client.put(
        f"/item/{item_id}", json={"name": "x", "price": 1.0, "deleted": True}
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert client.get(f"/item/{item_id}").json()["deleted"] is False


def test_put_deleted_item_is_not_modified(client: TestClient) -> None:
    item_id = create_item(client, "old", 1.0)
    client.delete(f"/item/{item_id}")

    response = client.put(f"/item/{item_id}", json={"name": "new", "price": 2.0})

    assert response.status_code == HTTPStatus.NOT_MODIFIED
    listed = client.get("/item", params={"show_deleted": True}).json()
    assert listed == [{"id": item_id, "name": "old", "price": 1.0, "deleted": True}]


def test_patch_missing_item_is_not_found(client: TestClient) -> None:
    assert client.patch("/item/7", json={"name": "x"}).status_code == HTTPStatus.NOT_FOUND


def test_patch_rejects_negative_price(client: TestClient) -> None:
    item_id = create_item(client, price=3.0)

    response = client.patch(f"/item/{item_id}", json={"price": -1})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert client.get(f"/item/{item_id}").json()["price"] == 3.0


def test_patch_rejects_null_fields(client: TestClient) -> None:
    item_id = create_item(client, name="kept")

    response = client.patch(f"/item/{item_id}", json={"name": None})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert client.get(f"/item/{item_id}").json()["name"] == "kept"


def test_patch_rejects_null_price(client: TestClient) -> None:
    item_id = create_item(client, price=4.0)

    response = client.patch(f"/item/{item_id}", json={"price": None})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert client.get(f"/item/{item_id}").json()["price"] == 4.0


def test_patch_with_empty_body_keeps_item(client: TestClient) -> None:
    item_id = create_item(client, "a", 5.0)

    response = client.patch(f"/item/{item_id}", json={})

    assert response.json() == {"id": item_id, "name": "a", "price": 5.0, "deleted": False}


def test_patch_changes_only_given_fields(client: TestClient) -> None:
    item_id = create_item(client, "a", 5.0)

    response = client.patch(f"/item/{item_id}", json={"name": "b"})

    assert response.json() == {"id": item_id, "name": "b", "price": 5.0, "deleted": False}


def test_delete_is_soft_and_idempotent(client: TestClient) -> None:
    item_id = create_item(client)

    assert client.delete(f"/item/{item_id}").status_code == HTTPStatus.OK
    assert client.delete(f"/item/{item_id}").status_code == HTTPStatus.OK
    assert client.get("/item").json() == []
    assert client.get("/item", params={"show_deleted": True}).json()[0]["deleted"] is True


def test_add_to_cart_increments_quantity(client: TestClient) -> None:
    cart_id = create_cart(client)
    item_id = create_item(client, "tea", 2.5)

    client.post(f"/cart/{cart_id}/add/{item_id}")
    response = client.post(f"/cart/{cart_id}/add/{item_id}")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "id": cart_id,
        "items": [{"id": item_id, "name": "tea", "quantity": 2, "available": True}],
        "price": 5.0,
    }


def test_cart_price_excludes_deleted_items_and_tracks_current_prices(
    client: TestClient,
) -> None:
    cart_id = create_cart(client)
    kept = create_item(client, "kept", 1.25)
    removed = create_item(client, "removed", 100.0)
    for item_id in (kept, kept, kept, removed):
        client.post(f"/cart/{cart_id}/add/{item_id}")

    assert client.get(f"/cart/{cart_id}").json()["price"] == pytest.approx(103.75)

    client.delete(f"/item/{removed}")
    client.patch(f"/item/{kept}", json={"price": 2.0})
    cart = client.get(f"/cart/{cart_id}").json()

    assert cart["price"] == pytest.approx(6.0)
    assert cart["items"] == [
        {"id": kept, "name": "kept", "quantity": 3, "available": True},
        {"id": removed, "name": "removed", "quantity": 1, "available": False},
    ]


def test_cart_reflects_renamed_item(client: TestClient) -> None:
    cart_id = create_cart(client)
    item_id = create_item(client, "old")
    client.post(f"/cart/{cart_id}/add/{item_id}")

    client.put(f"/item/{item_id}", json={"name": "new", "price": 1.0})

    assert client.get(f"/cart/{cart_id}").json()["items"][0]["name"] == "new"


def test_item_list_pagination_and_inclusive_price_filters(client: TestClient) -> None:
    ids = [create_item(client, f"i{i}", float(i)) for i in range(6)]
    client.delete(f"/item/{ids[3]}")

    def listed(**params) -> list[int]:
        return [item["id"] for item in client.get("/item", params=params).json()]

    assert listed() == [ids[0], ids[1], ids[2], ids[4], ids[5]]
    assert listed(offset=1, limit=2) == [ids[1], ids[2]]
    assert listed(offset=10) == []
    assert listed(limit=100, show_deleted=True) == ids
    assert listed(min_price=2.0, max_price=4.0) == [ids[2], ids[4]]
    assert listed(min_price=2.0, max_price=4.0, show_deleted=True) == ids[2:5]
    assert listed(min_price=5.0, max_price=1.0) == []
    assert len(listed()) == 5
    assert len(listed(limit=1)) == 1


def test_item_list_default_limit_is_ten(client: TestClient) -> None:
    for i in range(12):
        create_item(client, f"i{i}")

    assert len(client.get("/item").json()) == 10


def test_cart_list_filters_by_price_and_total_quantity(client: TestClient) -> None:
    cheap = create_item(client, "cheap", 1.0)
    pricey = create_item(client, "pricey", 50.0)
    empty = create_cart(client)
    small = create_cart(client)
    big = create_cart(client)
    client.post(f"/cart/{small}/add/{cheap}")
    for item_id in (cheap, cheap, pricey):
        client.post(f"/cart/{big}/add/{item_id}")

    def listed(**params) -> list[int]:
        return [cart["id"] for cart in client.get("/cart", params=params).json()]

    assert listed() == [empty, small, big]
    assert listed(min_price=1.0) == [small, big]
    assert listed(max_price=1.0) == [empty, small]
    assert listed(min_quantity=1, max_quantity=1) == [small]
    assert listed(min_quantity=3) == [big]
    assert listed(max_quantity=0) == [empty]
    assert listed(offset=1, limit=1) == [small]
    assert listed(min_price=1.0, offset=1) == [big]

    client.delete(f"/item/{pricey}")

    assert listed(min_quantity=3) == [big]
    assert listed(min_price=50.0) == []


def test_add_to_cart_overflow_is_rejected_without_changes(client: TestClient) -> None:
    cart_id = create_cart(client)
    item_id = create_item(client, "huge", 1e308)
    assert client.post(f"/cart/{cart_id}/add/{item_id}").status_code == HTTPStatus.OK
    before = client.get(f"/cart/{cart_id}").json()

    response = client.post(f"/cart/{cart_id}/add/{item_id}")

    assert response.status_code == HTTPStatus.CONFLICT
    assert client.get(f"/cart/{cart_id}").json() == before
    assert before["items"][0]["quantity"] == 1


def test_add_other_item_overflow_is_rejected_without_changes(client: TestClient) -> None:
    cart_id = create_cart(client)
    first = create_item(client, "a", 1.5e308)
    second = create_item(client, "b", 1.5e308)
    client.post(f"/cart/{cart_id}/add/{first}")
    before = client.get(f"/cart/{cart_id}").json()

    assert client.post(f"/cart/{cart_id}/add/{second}").status_code == HTTPStatus.CONFLICT
    assert client.get(f"/cart/{cart_id}").json() == before


@pytest.mark.parametrize("method", ["put", "patch"])
def test_item_price_update_overflowing_cart_is_rejected_without_changes(
    client: TestClient, method: str
) -> None:
    cart_id = create_cart(client)
    item_id = create_item(client, "x", 1.0)
    client.post(f"/cart/{cart_id}/add/{item_id}")
    client.post(f"/cart/{cart_id}/add/{item_id}")

    response = client.request(
        method, f"/item/{item_id}", json={"name": "y", "price": 1e308}
    )

    assert response.status_code == HTTPStatus.CONFLICT
    assert client.get(f"/item/{item_id}").json() == {
        "id": item_id,
        "name": "x",
        "price": 1.0,
        "deleted": False,
    }
    assert client.get(f"/cart/{cart_id}").json()["price"] == 2.0


def test_large_price_update_allowed_when_item_is_in_no_cart(client: TestClient) -> None:
    item_id = create_item(client, "x", 1.0)

    response = client.patch(f"/item/{item_id}", json={"price": 1e308})

    assert response.status_code == HTTPStatus.OK
    assert response.json()["price"] == 1e308


def test_concurrent_adds_do_not_lose_updates(client: TestClient) -> None:
    cart_id = create_cart(client)
    item_id = create_item(client, "x", 1.0)
    workers, per_worker = 8, 50

    def add_many() -> None:
        for _ in range(per_worker):
            assert client.post(f"/cart/{cart_id}/add/{item_id}").status_code == HTTPStatus.OK

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(add_many) for _ in range(workers)]:
            future.result()

    cart = client.get(f"/cart/{cart_id}").json()
    assert cart["items"][0]["quantity"] == workers * per_worker
    assert cart["price"] == workers * per_worker


def test_concurrent_store_creates_have_unique_ids(store: ShopStore) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: store.create_cart(), range(400)))

    assert sorted(ids) == list(range(1, 401))


def test_failed_add_to_cart_rolls_back_whole_transaction(make_store, snapshot_metrics) -> None:
    store = make_store(snapshot_metrics)
    cart_id = store.create_cart()
    item_id = store.create_item("x", 1.0).id

    snapshot_metrics.failing = True
    with pytest.raises(RuntimeError):
        store.add_to_cart(cart_id, item_id)
    snapshot_metrics.failing = False

    assert store.get_cart(cart_id).items == []
    assert store.add_to_cart(cart_id, item_id).items[0].quantity == 1


def test_database_check_rejects_negative_price(store: ShopStore) -> None:
    with pytest.raises(IntegrityError):
        store.create_item("bad", -1.0)

    assert store.list_items(0, 10, None, None, True) == []


def test_data_survives_new_store_on_same_database(make_store) -> None:
    store = make_store(name="shared.db")
    item = store.create_item("kept", 2.5)
    cart_id = store.create_cart()
    store.add_to_cart(cart_id, item.id)
    store.dispose()

    reopened = make_store(name="shared.db")

    assert reopened.get_item(item.id) == item
    assert reopened.get_cart(cart_id).price == 2.5


@pytest.mark.parametrize("key", [2**31, 2**63, 2**70, -(2**70)])
def test_out_of_range_ids_are_not_found(client: TestClient, key: int) -> None:
    item_id = create_item(client)
    cart_id = create_cart(client)

    assert client.get(f"/item/{key}").status_code == HTTPStatus.NOT_FOUND
    assert client.get(f"/cart/{key}").status_code == HTTPStatus.NOT_FOUND
    assert client.delete(f"/item/{key}").status_code == HTTPStatus.NOT_FOUND
    assert client.patch(f"/item/{key}", json={"name": "x"}).status_code == HTTPStatus.NOT_FOUND
    assert client.post(f"/cart/{key}/add/{item_id}").status_code == HTTPStatus.NOT_FOUND
    assert client.post(f"/cart/{cart_id}/add/{key}").status_code == HTTPStatus.NOT_FOUND


def test_huge_pagination_values_are_accepted(client: TestClient) -> None:
    create_item(client)
    create_cart(client)

    assert client.get("/item", params={"offset": 2**70}).json() == []
    assert len(client.get("/item", params={"limit": 2**70}).json()) == 1
    assert client.get("/cart", params={"offset": 2**70}).json() == []
    assert len(client.get("/cart", params={"limit": 2**70}).json()) == 1


@pytest.mark.parametrize("huge", [2**63 - 1, 2**63, 2**70])
def test_huge_quantity_filters_are_accepted(client: TestClient, huge: int) -> None:
    cart_id = create_cart(client)
    client.post(f"/cart/{cart_id}/add/{create_item(client)}")

    assert client.get("/cart", params={"min_quantity": huge}).json() == []
    assert client.get("/cart", params={"min_quantity": huge, "min_price": 0}).json() == []
    assert [cart["id"] for cart in client.get("/cart", params={"max_quantity": huge}).json()] == [
        cart_id
    ]


def test_huge_price_filters_are_accepted(client: TestClient) -> None:
    cart_id = create_cart(client)
    client.post(f"/cart/{cart_id}/add/{create_item(client, price=1.0)}")

    for path in ("/item", "/cart"):
        assert client.get(path, params={"min_price": 1e308}).json() == []
        assert len(client.get(path, params={"max_price": 1e308}).json()) == 1


def test_items_inserted_without_deleted_flag_are_not_deleted(store: ShopStore) -> None:
    with store._engine.begin() as connection:
        connection.execute(text("INSERT INTO items (name, price) VALUES ('raw', 1.0)"))

    assert [item.name for item in store.list_items(0, 10, None, None, False)] == ["raw"]


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        database_url()


def test_chat_broadcasts_to_others_in_same_room_only(client: TestClient) -> None:
    with (
        client.websocket_connect("/chat/room1") as alice,
        client.websocket_connect("/chat/room1") as bob,
        client.websocket_connect("/chat/room2") as carol,
        client.websocket_connect("/chat/room2") as dave,
    ):
        alice.send_text("hello room1")
        alice_name, text = parse(bob.receive_text())
        assert text == "hello room1"

        bob.send_text("hi alice")
        bob_name, text = parse(alice.receive_text())
        assert text == "hi alice"
        assert bob_name != alice_name

        carol.send_text("hello room2")
        carol_name, text = parse(dave.receive_text())
        assert text == "hello room2"

        dave.send_text("hi carol")
        dave_name, text = parse(carol.receive_text())
        assert text == "hi carol"

        alice.send_text("again")
        assert parse(bob.receive_text()) == (alice_name, "again")

        assert len({alice_name, bob_name, carol_name, dave_name}) == 4


def test_chat_message_format_keeps_separator_in_text(client: TestClient) -> None:
    with (
        client.websocket_connect("/chat/fmt") as sender,
        client.websocket_connect("/chat/fmt") as receiver,
    ):
        sender.send_text("a :: b")
        message = receiver.receive_text()

    name, text = parse(message)
    assert message == f"{name} :: a :: b"
    assert text == "a :: b"


def test_chat_keeps_rooms_separate_after_clients_leave(client: TestClient) -> None:
    with client.websocket_connect("/chat/stay") as stayer:
        with client.websocket_connect("/chat/stay") as leaver:
            leaver.send_text("bye")
            leaver_name, text = parse(stayer.receive_text())
            assert text == "bye"

        with (
            client.websocket_connect("/chat/other") as other,
            client.websocket_connect("/chat/stay") as newcomer,
        ):
            other.send_text("ping")
            stayer.send_text("still here")
            stayer_name, text = parse(newcomer.receive_text())
            assert text == "still here"

            newcomer.send_text("welcome")
            newcomer_name, text = parse(stayer.receive_text())
            assert text == "welcome"
            assert len({leaver_name, stayer_name, newcomer_name}) == 3


def test_chat_closes_connection_on_binary_frame(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    rooms: ChatRoomManager = main.chat_rooms
    disconnected = []
    original = rooms.disconnect

    def spy(room: str, websocket: object) -> None:
        disconnected.append(room)
        original(room, websocket)

    monkeypatch.setattr(rooms, "disconnect", spy)

    with client.websocket_connect("/chat/bin") as peer:
        with client.websocket_connect("/chat/bin") as sender:
            sender.send_bytes(b"\x00\x01")
            message = sender.receive()

        assert message["type"] == "websocket.close"
        assert message["code"] == 1003
        assert disconnected == ["bin"]

        with client.websocket_connect("/chat/bin") as other:
            other.send_text("after")
            assert parse(peer.receive_text())[1] == "after"


class FakeSocket:
    def __init__(self, broken: bool = False) -> None:
        self.broken = broken
        self.accepted = False
        self.attempts = 0
        self.sent: list[str] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, text: str) -> None:
        self.attempts += 1
        if self.broken:
            raise RuntimeError("socket is closed")
        self.sent.append(text)


def test_chat_broadcast_survives_dead_socket() -> None:
    async def scenario() -> None:
        rooms = ChatRoomManager()
        sender, dead, alive = FakeSocket(), FakeSocket(broken=True), FakeSocket()
        sender_name = await rooms.connect("r", sender)
        await rooms.connect("r", dead)
        await rooms.connect("r", alive)

        await rooms.broadcast("r", sender, "msg")

        assert sender.accepted and dead.accepted and alive.accepted
        assert alive.sent == [f"{sender_name} :: msg"]
        assert sender.sent == []

        await rooms.broadcast("r", sender, "again")

        assert dead.attempts == 1
        assert alive.sent == [f"{sender_name} :: msg", f"{sender_name} :: again"]

    asyncio.run(scenario())


def test_chat_disconnected_client_stops_receiving() -> None:
    async def scenario() -> None:
        rooms = ChatRoomManager()
        sender, leaver, stayer, outsider = FakeSocket(), FakeSocket(), FakeSocket(), FakeSocket()
        sender_name = await rooms.connect("r", sender)
        await rooms.connect("r", leaver)
        await rooms.connect("r", stayer)
        await rooms.connect("elsewhere", outsider)

        rooms.disconnect("r", leaver)
        await rooms.broadcast("r", sender, "msg")

        assert leaver.sent == []
        assert stayer.sent == [f"{sender_name} :: msg"]
        assert outsider.sent == []

    asyncio.run(scenario())


def test_chat_ignores_messages_from_unknown_or_disconnected_sender() -> None:
    async def scenario() -> None:
        rooms = ChatRoomManager()
        sender, receiver = FakeSocket(), FakeSocket()
        await rooms.connect("r", sender)
        await rooms.connect("r", receiver)

        rooms.disconnect("r", sender)
        rooms.disconnect("missing", sender)
        await rooms.broadcast("r", sender, "lost")
        await rooms.broadcast("missing", sender, "lost")

        assert receiver.sent == []

    asyncio.run(scenario())

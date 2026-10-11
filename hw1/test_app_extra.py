import json
from http import HTTPStatus
from typing import Any

import pytest
from async_asgi_testclient import TestClient

from app import application as app

pytestmark = pytest.mark.asyncio


class RawCall:
    """Drives the ASGI application with hand-crafted messages."""

    def __init__(self, scope: dict[str, Any], messages: list[dict[str, Any]]):
        self.scope = scope
        self.incoming = list(messages)
        self.sent: list[dict[str, Any]] = []

    async def receive(self) -> dict[str, Any]:
        return self.incoming.pop(0)

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def run(self) -> "RawCall":
        await app(self.scope, self.receive, self.send)
        return self

    @property
    def status(self) -> int:
        return self.sent[0]["status"]

    @property
    def headers(self) -> dict[bytes, bytes]:
        return dict(self.sent[0]["headers"])

    @property
    def payload(self) -> Any:
        return json.loads(b"".join(m.get("body", b"") for m in self.sent[1:]))


def http_scope(path: str, query: bytes = b"", method: str = "GET") -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query,
        "headers": [],
    }


def chunks(*parts: bytes) -> list[dict[str, Any]]:
    last = len(parts) - 1
    return [
        {"type": "http.request", "body": part, "more_body": index < last}
        for index, part in enumerate(parts)
    ]


async def get(path: str, **kwargs: Any):
    async with TestClient(app) as client:
        return await client.get(path, **kwargs)


@pytest.mark.parametrize(
    ("n", "expected"),
    [(0, 1), (1, 1), (5, 120), (20, 2432902008176640000)],
)
async def test_factorial_values(n: int, expected: int):
    response = await get("/factorial", query_string={"n": n})

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"result": expected}


async def test_factorial_large_value_is_exact():
    response = await get("/factorial", query_string={"n": 30})

    assert response.json() == {"result": 265252859812191058636308480000000}


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, 0),
        (1, 1),
        (2, 1),
        (10, 55),
        (100, 354224848179261915075),
    ],
)
async def test_fibonacci_values(n: int, expected: int):
    response = await get(f"/fibonacci/{n}")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"result": expected}


@pytest.mark.parametrize(
    ("numbers", "expected"),
    [
        ([1, 2, 3], 2.0),
        ([1, 2], 1.5),
        ([0.1, 0.2, 0.3], 0.2),
        ([-1, 1], 0.0),
        ([7], 7.0),
    ],
)
async def test_mean_values(numbers: list[float], expected: float):
    response = await get("/mean", json=numbers)

    assert response.status_code == HTTPStatus.OK
    assert response.json()["result"] == pytest.approx(expected, abs=1e-12)


async def test_mean_precision_uses_exact_summation():
    response = await get("/mean", json=[1e16, 1.0, -1e16, 1.0])

    assert response.json() == {"result": 0.5}


async def test_mean_from_query_parameter():
    response = await get("/mean", query_string={"numbers": "1,2,3"})

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"result": 2.0}


async def test_mean_query_accepts_floats_and_negatives():
    response = await get("/mean", query_string={"numbers": "-1.5,2.5,2"})

    assert response.json() == {"result": 1.0}


async def test_mean_body_takes_priority_over_query():
    call = await RawCall(
        http_scope("/mean", b"numbers=100,200"), chunks(b"[1, 2, 3]")
    ).run()

    assert call.status == HTTPStatus.OK
    assert call.payload == {"result": 2.0}


async def test_mean_body_assembled_from_several_messages():
    call = await RawCall(
        http_scope("/mean"), chunks(b"[1, ", b"2", b", 3", b"]")
    ).run()

    assert call.status == HTTPStatus.OK
    assert call.payload == {"result": 2.0}


async def test_mean_body_with_empty_trailing_message():
    call = await RawCall(http_scope("/mean"), chunks(b"[4, 6]", b"")).run()

    assert call.payload == {"result": 5.0}


async def test_response_is_json():
    call = await RawCall(http_scope("/fibonacci/3"), chunks(b"")).run()

    assert call.headers[b"content-type"] == b"application/json"
    assert call.payload == {"result": 2}


@pytest.mark.parametrize(
    "value",
    ["1.5", "1e3", " 1", "+1", "1_0", "١", "0x1", "abc"],
)
async def test_factorial_rejects_non_integer_formats(value: str):
    response = await get("/factorial", query_string={"n": value})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_factorial_rejects_too_large_argument():
    response = await get("/factorial", query_string={"n": 100000})

    assert response.status_code == HTTPStatus.BAD_REQUEST


@pytest.mark.parametrize("params", ["/1.5", "/", "/1/2", "/%20"])
async def test_fibonacci_rejects_non_integer_formats(params: str):
    response = await get("/fibonacci" + params)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_fibonacci_rejects_too_large_argument():
    response = await get("/fibonacci/1000000000")

    assert response.status_code == HTTPStatus.BAD_REQUEST


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"{\"numbers\": [1, 2]}",
        b"\"1,2,3\"",
        b"5",
        b"[1, \"2\"]",
        b"[true, 1]",
        b"[null]",
        b"[[1]]",
        b"[NaN]",
        b"[Infinity, 1]",
        b"[1e999]",
    ],
)
async def test_mean_rejects_unsupported_body(body: bytes):
    call = await RawCall(http_scope("/mean"), chunks(body)).run()

    assert call.status == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize("numbers", ["a,b", "1,,2", "1,nan", "1,inf", "1;2"])
async def test_mean_rejects_unsupported_query(numbers: str):
    response = await get("/mean", query_string={"numbers": numbers})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_mean_rejects_empty_query():
    response = await get("/mean", query_string={"numbers": ""})

    assert response.status_code == HTTPStatus.BAD_REQUEST


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/factorial/5"),
        ("GET", "/fibonacci"),
        ("GET", "/mean/1"),
        ("GET", "/FACTORIAL"),
    ],
)
async def test_unknown_paths(method: str, path: str):
    call = await RawCall(http_scope(path, method=method), chunks(b"")).run()

    assert call.status == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize("path", ["/factorial", "/fibonacci/1", "/mean"])
async def test_wrong_method_on_known_path(path: str):
    call = await RawCall(http_scope(path, method="POST"), chunks(b"[1]")).run()

    assert call.status == HTTPStatus.METHOD_NOT_ALLOWED


async def test_lifespan_startup_and_shutdown():
    call = await RawCall(
        {"type": "lifespan"},
        [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}],
    ).run()

    assert call.sent == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]


async def test_test_client_runs_lifespan():
    async with TestClient(app) as client:
        response = await client.get("/factorial", query_string={"n": 3})

    assert response.json() == {"result": 6}


@pytest.mark.parametrize("path", ["/factorial", "/fibonacci/"])
@pytest.mark.parametrize(
    "digits",
    ["1" * 5000, "0" * 5000, "-" + "1" * 5000],
    ids=["ones", "zeros", "negative"],
)
async def test_overlong_integer_is_out_of_range(path: str, digits: str):
    if path == "/factorial":
        call = await RawCall(http_scope(path, b"n=" + digits.encode()), chunks(b"")).run()
    else:
        call = await RawCall(http_scope(path + digits), chunks(b"")).run()

    assert call.status == HTTPStatus.BAD_REQUEST


@pytest.mark.parametrize(
    ("numbers", "expected"),
    [([-48.99] * 3, -48.99), ([5e-324] * 3, 5e-324)],
)
async def test_mean_sums_before_dividing(numbers: list[float], expected: float):
    call = await RawCall(http_scope("/mean"), chunks(json.dumps(numbers).encode())).run()

    assert call.payload == {"result": expected}


async def test_mean_of_huge_values_stays_finite():
    call = await RawCall(http_scope("/mean"), chunks(b"[1.7e308, 1.7e308, 1.7e308]")).run()

    assert call.status == HTTPStatus.OK
    assert call.payload["result"] == pytest.approx(1.7e308)


async def test_disconnect_while_reading_body_sends_nothing():
    call = await RawCall(
        http_scope("/mean"),
        [
            {"type": "http.request", "body": b"[1, ", "more_body": True},
            {"type": "http.disconnect"},
        ],
    ).run()

    assert call.sent == []


async def test_unsupported_scope_type_is_ignored():
    call = await RawCall({"type": "websocket", "path": "/"}, []).run()

    assert call.sent == []

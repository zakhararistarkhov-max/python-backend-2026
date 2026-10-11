import asyncio
import functools
import itertools
import time

import httpx
import pytest

import load_generator
from load_generator import RatePacer, ShopLoadGenerator


def _handler(request: httpx.Request) -> httpx.Response:
    if request.method == "POST" and request.url.path in ("/item", "/cart"):
        return httpx.Response(201, json={"id": next(_ids)})
    return httpx.Response(200, json={})


_ids = itertools.count(1)


@pytest.fixture
def mock_client(monkeypatch):
    client_cls = functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(_handler))
    monkeypatch.setattr(load_generator.httpx, "AsyncClient", client_cls)


def test_pacer_refuses_slot_after_deadline():
    async def scenario():
        pacer = RatePacer(0.1)
        deadline = time.monotonic() + 0.2
        first = await pacer.wait(deadline)
        start = time.monotonic()
        second = await pacer.wait(deadline)
        return first, second, time.monotonic() - start

    first, second, waited = asyncio.run(scenario())
    assert first is True
    assert second is False
    assert waited < 0.5


def test_run_honours_duration_at_low_rps(mock_client):
    generator = ShopLoadGenerator("http://shop", duration=1.0, rps=0.5, concurrency=4, seed=1)
    start = time.monotonic()
    summary = asyncio.run(generator.run())
    assert time.monotonic() - start < 2.0
    assert sum(summary.operations.values()) <= 10 + 1


def test_overflow_requests_are_paced(mock_client):
    generator = ShopLoadGenerator("http://shop", duration=1.0, rps=0.5, concurrency=1, seed=1)
    generator._operations = {generator._overflow: 1}
    start = time.monotonic()
    summary = asyncio.run(generator.run())
    assert time.monotonic() - start < 2.0
    assert summary.operations["overflow"] == 1

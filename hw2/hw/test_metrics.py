import re
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from shop_api import main
from shop_api.store import ShopStore

ID_IN_PATH_RE = re.compile(r"/\d+(/|$)")


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(main, "store", ShopStore())
    return TestClient(main.app)


def scrape(client: TestClient) -> list:
    response = client.get("/metrics")
    assert response.status_code == HTTPStatus.OK
    return [
        sample
        for family in text_string_to_metric_families(response.text)
        for sample in family.samples
    ]


def value(client: TestClient, name: str, **labels: str) -> float:
    return sum(
        sample.value
        for sample in scrape(client)
        if sample.name == name and labels.items() <= sample.labels.items()
    )


def create_item(client: TestClient, price: float = 10.0) -> int:
    return client.post("/item", json={"name": "item", "price": price}).json()["id"]


def create_cart(client: TestClient) -> int:
    return client.post("/cart").json()["id"]


def test_metrics_endpoint_exposes_http_latency_histogram(client: TestClient) -> None:
    client.get("/item")

    names = {sample.name for sample in scrape(client)}

    assert "http_request_duration_seconds_bucket" in names
    assert "http_request_duration_highr_seconds_bucket" in names
    assert "http_requests_total" in names


def test_http_latency_buckets_are_fine_grained(client: TestClient) -> None:
    client.get("/item")

    bounds = {
        float(sample.labels["le"])
        for sample in scrape(client)
        if sample.name == "http_request_duration_highr_seconds_bucket"
    }

    assert len([bound for bound in bounds if bound <= 0.1]) >= 10


def test_items_and_carts_created_are_counted(client: TestClient) -> None:
    items_before = value(client, "shop_items_created_total")
    carts_before = value(client, "shop_carts_created_total")

    create_item(client)
    create_item(client)
    create_cart(client)

    assert value(client, "shop_items_created_total") == items_before + 2
    assert value(client, "shop_carts_created_total") == carts_before + 1


def test_item_deletes_are_counted(client: TestClient) -> None:
    item_id = create_item(client)
    before = value(client, "shop_item_deletes_total")

    client.delete(f"/item/{item_id}")

    assert value(client, "shop_item_deletes_total") == before + 1


@pytest.mark.parametrize("reason", ["missing_cart", "missing_item", "overflow"])
def test_rejected_cart_adds_are_counted_by_reason(client: TestClient, reason: str) -> None:
    cart_id = create_cart(client)
    item_id = create_item(client, price=1e308)
    client.post(f"/cart/{cart_id}/add/{item_id}")
    targets = {
        "missing_cart": f"/cart/{cart_id + 1000}/add/{item_id}",
        "missing_item": f"/cart/{cart_id}/add/{item_id + 1000}",
        "overflow": f"/cart/{cart_id}/add/{item_id}",
    }
    before = value(client, "shop_cart_add_attempts_total", result="rejected", reason=reason)

    response = client.post(targets[reason])

    assert response.status_code in (HTTPStatus.NOT_FOUND, HTTPStatus.CONFLICT)
    assert (
        value(client, "shop_cart_add_attempts_total", result="rejected", reason=reason)
        == before + 1
    )


def test_accepted_cart_adds_are_counted(client: TestClient) -> None:
    cart_id = create_cart(client)
    item_id = create_item(client)
    before = value(client, "shop_cart_add_attempts_total", result="accepted")

    client.post(f"/cart/{cart_id}/add/{item_id}")

    assert value(client, "shop_cart_add_attempts_total", result="accepted") == before + 1


def test_returned_carts_are_observed(client: TestClient) -> None:
    cart_id = create_cart(client)
    item_id = create_item(client, price=7.0)
    gone_id = create_item(client, price=3.0)
    client.post(f"/cart/{cart_id}/add/{item_id}")
    client.post(f"/cart/{cart_id}/add/{item_id}")
    client.post(f"/cart/{cart_id}/add/{gone_id}")
    client.delete(f"/item/{gone_id}")
    count_before = value(client, "shop_cart_observed_price_count")
    price_before = value(client, "shop_cart_observed_price_sum")
    quantity_before = value(client, "shop_cart_observed_item_quantity_sum")
    unavailable_before = value(client, "shop_cart_observed_unavailable_items_sum")

    client.get(f"/cart/{cart_id}")

    assert value(client, "shop_cart_observed_price_count") == count_before + 1
    assert value(client, "shop_cart_observed_price_sum") == price_before + 14.0
    assert value(client, "shop_cart_observed_item_quantity_sum") == quantity_before + 3
    assert value(client, "shop_cart_observed_unavailable_items_sum") == unavailable_before + 1


def test_listed_carts_are_each_observed(client: TestClient) -> None:
    create_cart(client)
    create_cart(client)
    before = value(client, "shop_cart_observed_price_count")

    carts = client.get("/cart", params={"limit": 2}).json()

    assert value(client, "shop_cart_observed_price_count") == before + len(carts) == before + 2


def test_store_stage_latency_is_labelled_by_fixed_stages(client: TestClient) -> None:
    cart_id = create_cart(client)
    item_id = create_item(client)
    client.post(f"/cart/{cart_id}/add/{item_id}")
    client.get("/cart")

    stages = {
        sample.labels["stage"]
        for sample in scrape(client)
        if sample.name == "shop_store_stage_duration_seconds_count" and sample.value > 0
    }

    assert {"create_item", "create_cart", "add_to_cart", "cart_snapshot", "list_carts"} <= stages


def test_no_label_contains_ids(client: TestClient) -> None:
    cart_id = create_cart(client)
    item_id = create_item(client)
    client.post(f"/cart/{cart_id}/add/{item_id}")
    client.get(f"/cart/{cart_id}")
    client.get(f"/item/{item_id}")
    client.get(f"/item/{item_id + 1000}")
    client.delete(f"/item/{item_id}")

    for sample in scrape(client):
        assert not any(name.endswith("id") for name in sample.labels), sample
        for label_value in sample.labels.values():
            assert not ID_IN_PATH_RE.search(label_value), sample

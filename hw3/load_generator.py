import argparse
import asyncio
import random
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field

import httpx

MAX_DURATION = 3600.0
MAX_RPS = 500.0
MAX_CONCURRENCY = 64
POOL_LIMIT = 500
OVERFLOW_PRICE = 1.5e308


class RatePacer:
    """Spaces request starts evenly to keep a global requests-per-second limit."""

    def __init__(self, rps: float) -> None:
        self._interval = 1.0 / rps
        self._next = time.monotonic()
        self._lock = asyncio.Lock()

    async def wait(self, deadline: float) -> bool:
        async with self._lock:
            now = time.monotonic()
            start = max(self._next, now)
            if start >= deadline:
                return False
            self._next = start + self._interval
        if start > now:
            await asyncio.sleep(start - now)
        return True


@dataclass
class LoadSummary:
    """Aggregated outcome of a load run."""

    statuses: Counter = field(default_factory=Counter)
    operations: Counter = field(default_factory=Counter)
    failures: Counter = field(default_factory=Counter)
    latencies: list[float] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    finished: float = 0.0

    def record(self, operation: str, status: int, elapsed: float) -> None:
        self.operations[operation] += 1
        self.statuses[status] += 1
        self.latencies.append(elapsed)

    def render(self) -> str:
        elapsed = max(self.finished - self.started, 1e-9)
        total = sum(self.statuses.values())
        lines = [
            f"requests: {total} in {elapsed:.1f}s ({total / elapsed:.1f} req/s)",
            "statuses: " + ", ".join(f"{k}={v}" for k, v in sorted(self.statuses.items())),
            "operations: " + ", ".join(f"{k}={v}" for k, v in sorted(self.operations.items())),
        ]
        if self.latencies:
            cuts = statistics.quantiles(self.latencies, n=100) if total > 1 else self.latencies * 99
            lines.append(
                f"client latency: p50={cuts[49] * 1000:.1f}ms p95={cuts[94] * 1000:.1f}ms "
                f"p99={cuts[98] * 1000:.1f}ms"
            )
        if self.failures:
            lines.append(
                "transport errors: " + ", ".join(f"{k}={v}" for k, v in self.failures.items())
            )
        return "\n".join(lines)


class ShopLoadGenerator:
    """Bounded mixed workload of real shop API operations."""

    def __init__(
        self, base_url: str, duration: float, rps: float, concurrency: int, seed: int | None
    ) -> None:
        self._base_url = base_url
        self._duration = duration
        self._concurrency = concurrency
        self._pacer = RatePacer(rps)
        self._deadline = 0.0
        self._random = random.Random(seed)
        self._items: list[int] = []
        self._deleted: list[int] = []
        self._carts: list[int] = []
        self._summary = LoadSummary()
        self._operations = {
            self._create_item: 10,
            self._create_cart: 6,
            self._add_to_cart: 25,
            self._get_cart: 10,
            self._list_carts: 8,
            self._get_item: 8,
            self._list_items: 8,
            self._patch_item: 4,
            self._put_item: 3,
            self._delete_item: 3,
            self._add_deleted_item: 2,
            self._invalid_request: 5,
            self._overflow: 1,
        }

    @property
    def summary(self) -> LoadSummary:
        return self._summary

    async def run(self) -> LoadSummary:
        self._deadline = time.monotonic() + self._duration
        limits = httpx.Limits(max_connections=self._concurrency)
        async with httpx.AsyncClient(base_url=self._base_url, timeout=5.0, limits=limits) as client:
            await self._seed(client)
            await asyncio.gather(*(self._worker(client) for _ in range(self._concurrency)))
        self._summary.finished = time.monotonic()
        return self._summary

    async def _seed(self, client: httpx.AsyncClient) -> None:
        for _ in range(5):
            await self._create_item(client)
            await self._create_cart(client)

    async def _worker(self, client: httpx.AsyncClient) -> None:
        operations = list(self._operations)
        weights = list(self._operations.values())
        while await self._pacer.wait(self._deadline):
            operation = self._random.choices(operations, weights)[0]
            try:
                await operation(client)
            except httpx.HTTPError as exc:
                self._summary.failures[type(exc).__name__] += 1

    async def _call(
        self, client: httpx.AsyncClient, name: str, method: str, url: str, **kwargs
    ) -> httpx.Response:
        start = time.perf_counter()
        response = await client.request(method, url, **kwargs)
        self._summary.record(name, response.status_code, time.perf_counter() - start)
        return response

    def _remember(self, pool: list[int], value: int) -> None:
        pool.append(value)
        if len(pool) > POOL_LIMIT:
            pool.pop(0)

    def _price(self) -> float:
        return round(self._random.uniform(1.0, 500.0), 2)

    def _item_body(self) -> dict:
        return {"name": f"item-{self._random.randint(1, 10_000)}", "price": self._price()}

    async def _create_item(self, client: httpx.AsyncClient) -> None:
        response = await self._call(client, "create_item", "POST", "/item", json=self._item_body())
        if response.status_code == 201:
            self._remember(self._items, response.json()["id"])

    async def _create_cart(self, client: httpx.AsyncClient) -> None:
        response = await self._call(client, "create_cart", "POST", "/cart")
        if response.status_code == 201:
            self._remember(self._carts, response.json()["id"])

    async def _add_to_cart(self, client: httpx.AsyncClient) -> None:
        if not self._items or not self._carts:
            return
        cart_id = self._random.choice(self._carts)
        item_id = self._random.choice(self._items)
        await self._call(client, "add_to_cart", "POST", f"/cart/{cart_id}/add/{item_id}")

    async def _get_cart(self, client: httpx.AsyncClient) -> None:
        if self._carts:
            await self._call(client, "get_cart", "GET", f"/cart/{self._random.choice(self._carts)}")

    async def _list_carts(self, client: httpx.AsyncClient) -> None:
        params = {"limit": self._random.choice([5, 10, 20]), "max_price": 100_000}
        if self._random.random() < 0.5:
            params["min_price"] = self._random.choice([0, 50, 200, 1000])
        if self._random.random() < 0.5:
            params["min_quantity"] = self._random.randint(0, 3)
            params["max_quantity"] = params["min_quantity"] + self._random.randint(1, 20)
        await self._call(client, "list_carts", "GET", "/cart", params=params)

    async def _get_item(self, client: httpx.AsyncClient) -> None:
        if self._items:
            await self._call(client, "get_item", "GET", f"/item/{self._random.choice(self._items)}")

    async def _list_items(self, client: httpx.AsyncClient) -> None:
        params = {
            "offset": self._random.randint(0, 20),
            "limit": self._random.choice([5, 10, 50]),
            "min_price": self._random.choice([0, 10, 100]),
            "max_price": self._random.choice([200, 400, 1000]),
            "show_deleted": self._random.random() < 0.3,
        }
        await self._call(client, "list_items", "GET", "/item", params=params)

    async def _patch_item(self, client: httpx.AsyncClient) -> None:
        if self._items:
            item_id = self._random.choice(self._items)
            body = {"price": self._price()}
            await self._call(client, "patch_item", "PATCH", f"/item/{item_id}", json=body)

    async def _put_item(self, client: httpx.AsyncClient) -> None:
        if self._items:
            item_id = self._random.choice(self._items)
            await self._call(client, "put_item", "PUT", f"/item/{item_id}", json=self._item_body())

    async def _delete_item(self, client: httpx.AsyncClient) -> None:
        if len(self._items) < 10:
            return
        item_id = self._items.pop(self._random.randrange(len(self._items)))
        await self._call(client, "delete_item", "DELETE", f"/item/{item_id}")
        self._remember(self._deleted, item_id)

    async def _add_deleted_item(self, client: httpx.AsyncClient) -> None:
        if self._deleted and self._carts:
            cart_id = self._random.choice(self._carts)
            item_id = self._random.choice(self._deleted)
            await self._call(client, "add_deleted_item", "POST", f"/cart/{cart_id}/add/{item_id}")

    async def _invalid_request(self, client: httpx.AsyncClient) -> None:
        missing = 10**9 + self._random.randint(0, 1000)
        item_id = self._random.choice(self._items) if self._items else missing
        requests = [
            ("GET", f"/item/{missing}", {}),
            ("GET", f"/cart/{missing}", {}),
            ("POST", f"/cart/{missing}/add/{item_id}", {}),
            ("POST", "/item", {"json": {"name": "broken", "price": -1}}),
            ("PATCH", f"/item/{item_id}", {"json": {"unknown": 1}}),
            ("GET", "/item", {"params": {"limit": 0}}),
        ]
        method, url, kwargs = self._random.choice(requests)
        await self._call(client, "invalid", method, url, **kwargs)

    async def _paced_call(
        self, client: httpx.AsyncClient, name: str, method: str, url: str, **kwargs
    ) -> httpx.Response | None:
        if not await self._pacer.wait(self._deadline):
            return None
        return await self._call(client, name, method, url, **kwargs)

    async def _overflow(self, client: httpx.AsyncClient) -> None:
        cart = await self._call(client, "overflow", "POST", "/cart")
        body = {"name": "huge", "price": OVERFLOW_PRICE}
        item = await self._paced_call(client, "overflow", "POST", "/item", json=body)
        if item is None or cart.status_code != 201 or item.status_code != 201:
            return
        item_id = item.json()["id"]
        url = f"/cart/{cart.json()['id']}/add/{item_id}"
        for method, path in (("POST", url), ("POST", url), ("DELETE", f"/item/{item_id}")):
            if await self._paced_call(client, "overflow", method, path) is None:
                return


def bounded(kind: type, low: float, high: float):
    def parse(raw: str):
        value = kind(raw)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be in [{low}, {high}]")
        return value

    return parse


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bounded load generator for the shop API.")
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--duration", type=bounded(float, 1, MAX_DURATION), default=60.0)
    parser.add_argument("--rps", type=bounded(float, 0.1, MAX_RPS), default=30.0)
    parser.add_argument("--concurrency", type=bounded(int, 1, MAX_CONCURRENCY), default=8)
    parser.add_argument("--seed", type=int, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    generator = ShopLoadGenerator(
        args.base_url, args.duration, args.rps, args.concurrency, args.seed
    )
    try:
        asyncio.run(generator.run())
    except KeyboardInterrupt:
        generator.summary.finished = time.monotonic()
        print(generator.summary.render())
        raise SystemExit(130) from None
    print(generator.summary.render())


if __name__ == "__main__":
    main()

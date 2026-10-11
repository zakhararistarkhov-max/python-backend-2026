from prometheus_client import REGISTRY, CollectorRegistry, Counter, Histogram
from prometheus_client.context_managers import Timer

from .contracts import CartResponse

HTTP_LATENCY_BUCKETS = (
    0.0005,
    0.001,
    0.0015,
    0.002,
    0.003,
    0.004,
    0.005,
    0.0075,
    0.01,
    0.015,
    0.02,
    0.03,
    0.05,
    0.075,
    0.1,
    0.15,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
)


class ShopMetrics:
    """Business and store-stage Prometheus metrics of the shop service."""

    REJECT_REASONS = ("missing_cart", "missing_item", "overflow")
    STAGES = (
        "create_item",
        "update_item",
        "delete_item",
        "list_items",
        "create_cart",
        "add_to_cart",
        "list_carts",
        "cart_snapshot",
    )

    def __init__(self, registry: CollectorRegistry = REGISTRY) -> None:
        self._items_created = Counter(
            "shop_items_created",
            "Items successfully created via POST /item.",
            registry=registry,
        )
        self._carts_created = Counter(
            "shop_carts_created",
            "Carts successfully created via POST /cart.",
            registry=registry,
        )
        self._item_deletes = Counter(
            "shop_item_deletes",
            "Successful DELETE /item operations that marked an existing item as deleted "
            "(idempotent repeats included).",
            registry=registry,
        )
        self._cart_adds = Counter(
            "shop_cart_add_attempts",
            "POST /cart/{cart_id}/add/{item_id} attempts by result: accepted (reason=none) "
            "or rejected with reason missing_cart, missing_item or overflow "
            "(cart price would become non-finite).",
            ["result", "reason"],
            registry=registry,
        )
        self._cart_price = Histogram(
            "shop_cart_observed_price",
            "Total price of a cart in currency units, observed once per cart each time "
            "a cart is returned to a client (GET /cart/{id}, every cart of GET /cart, "
            "successful add to cart).",
            buckets=(0, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000),
            registry=registry,
        )
        self._cart_quantity = Histogram(
            "shop_cart_observed_item_quantity",
            "Total number of item units (sum of quantities) in a cart, observed once per cart "
            "each time a cart is returned to a client.",
            buckets=(0, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144),
            registry=registry,
        )
        self._cart_unavailable = Histogram(
            "shop_cart_observed_unavailable_items",
            "Number of distinct cart lines whose item is deleted (available=false), observed "
            "once per cart each time a cart is returned to a client.",
            buckets=(0, 1, 2, 3, 5, 8, 13, 21),
            registry=registry,
        )
        self._stage_seconds = Histogram(
            "shop_store_stage_duration_seconds",
            "Wall time in seconds of a critical ShopStore stage, including lock wait "
            "and failed or rejected attempts; cart_snapshot is building one cart "
            "response with its price.",
            ["stage"],
            buckets=(
                0.00001,
                0.000025,
                0.00005,
                0.0001,
                0.00025,
                0.0005,
                0.001,
                0.0025,
                0.005,
                0.01,
                0.025,
                0.05,
                0.1,
                0.25,
            ),
            registry=registry,
        )
        self._cart_adds.labels(result="accepted", reason="none")
        for reason in self.REJECT_REASONS:
            self._cart_adds.labels(result="rejected", reason=reason)
        for stage in self.STAGES:
            self._stage_seconds.labels(stage=stage)

    def item_created(self) -> None:
        self._items_created.inc()

    def cart_created(self) -> None:
        self._carts_created.inc()

    def item_deleted(self) -> None:
        self._item_deletes.inc()

    def cart_add_accepted(self) -> None:
        self._cart_adds.labels(result="accepted", reason="none").inc()

    def cart_add_rejected(self, reason: str) -> None:
        if reason not in self.REJECT_REASONS:
            raise ValueError(reason)
        self._cart_adds.labels(result="rejected", reason=reason).inc()

    def cart_returned(self, cart: CartResponse) -> None:
        self._cart_price.observe(cart.price)
        self._cart_quantity.observe(sum(line.quantity for line in cart.items))
        self._cart_unavailable.observe(sum(not line.available for line in cart.items))

    def stage(self, name: str) -> Timer:
        if name not in self.STAGES:
            raise ValueError(name)
        return self._stage_seconds.labels(stage=name).time()


shop_metrics = ShopMetrics()

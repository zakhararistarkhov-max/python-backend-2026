import math
from dataclasses import dataclass, field
from threading import Lock

from .contracts import CartItemResponse, CartResponse, ItemResponse
from .metrics import ShopMetrics, shop_metrics


class NotFoundError(Exception):
    """Requested resource does not exist."""


class NotModifiedError(Exception):
    """Resource cannot be modified in its current state."""


class PriceOverflowError(Exception):
    """Operation would make a cart price non-finite."""


@dataclass(slots=True)
class Item:
    """Stored item."""

    id: int
    name: str
    price: float
    deleted: bool = False


@dataclass(slots=True)
class Cart:
    """Stored cart with item quantities keyed by item id."""

    id: int
    quantities: dict[int, int] = field(default_factory=dict)


class ShopStore:
    """Thread-safe in-memory storage for items and carts."""

    def __init__(self, metrics: ShopMetrics = shop_metrics) -> None:
        self._metrics = metrics
        self._lock = Lock()
        self._items: dict[int, Item] = {}
        self._carts: dict[int, Cart] = {}
        self._last_item_id = 0
        self._last_cart_id = 0

    def create_item(self, name: str, price: float) -> ItemResponse:
        with self._metrics.stage("create_item"), self._lock:
            self._last_item_id += 1
            item = Item(self._last_item_id, name, price)
            self._items[item.id] = item
            self._metrics.item_created()
            return self._item_response(item)

    def get_item(self, item_id: int) -> ItemResponse:
        with self._lock:
            return self._item_response(self._active_item(item_id))

    def list_items(
        self,
        offset: int,
        limit: int,
        min_price: float | None,
        max_price: float | None,
        show_deleted: bool,
    ) -> list[ItemResponse]:
        with self._metrics.stage("list_items"), self._lock:
            items = [
                item
                for item in self._items.values()
                if (show_deleted or not item.deleted)
                and _in_range(item.price, min_price, max_price)
            ]
            return [self._item_response(item) for item in items[offset : offset + limit]]

    def update_item(
        self, item_id: int, name: str | None = None, price: float | None = None
    ) -> ItemResponse:
        with self._metrics.stage("update_item"), self._lock:
            item = self._items.get(item_id)
            if item is None:
                raise NotFoundError
            if item.deleted:
                raise NotModifiedError
            if price is not None:
                self._check_price_change(item, price)
                item.price = price
            if name is not None:
                item.name = name
            return self._item_response(item)

    def delete_item(self, item_id: int) -> None:
        with self._metrics.stage("delete_item"), self._lock:
            item = self._items.get(item_id)
            if item is None:
                raise NotFoundError
            item.deleted = True
        self._metrics.item_deleted()

    def create_cart(self) -> int:
        with self._metrics.stage("create_cart"), self._lock:
            self._last_cart_id += 1
            cart_id = self._last_cart_id
            self._carts[cart_id] = Cart(cart_id)
        self._metrics.cart_created()
        return cart_id

    def get_cart(self, cart_id: int) -> CartResponse:
        with self._lock:
            return self._cart_response(self._cart(cart_id))

    def list_carts(
        self,
        offset: int,
        limit: int,
        min_price: float | None,
        max_price: float | None,
        min_quantity: int | None,
        max_quantity: int | None,
    ) -> list[CartResponse]:
        with self._metrics.stage("list_carts"), self._lock:
            carts = [
                cart
                for cart in self._carts.values()
                if _in_range(self._cart_price(cart.quantities), min_price, max_price)
                and _in_range(sum(cart.quantities.values()), min_quantity, max_quantity)
            ]
            return [self._cart_response(cart) for cart in carts[offset : offset + limit]]

    def add_to_cart(self, cart_id: int, item_id: int) -> CartResponse:
        with self._metrics.stage("add_to_cart"), self._lock:
            cart = self._carts.get(cart_id)
            if cart is None:
                self._metrics.cart_add_rejected("missing_cart")
                raise NotFoundError
            item = self._items.get(item_id)
            if item is None or item.deleted:
                self._metrics.cart_add_rejected("missing_item")
                raise NotFoundError
            quantities = dict(cart.quantities)
            quantities[item.id] = quantities.get(item.id, 0) + 1
            if not math.isfinite(self._cart_price(quantities)):
                self._metrics.cart_add_rejected("overflow")
                raise PriceOverflowError
            cart.quantities = quantities
            self._metrics.cart_add_accepted()
            return self._cart_response(cart)

    def _active_item(self, item_id: int) -> Item:
        item = self._items.get(item_id)
        if item is None or item.deleted:
            raise NotFoundError
        return item

    def _cart(self, cart_id: int) -> Cart:
        cart = self._carts.get(cart_id)
        if cart is None:
            raise NotFoundError
        return cart

    def _check_price_change(self, item: Item, price: float) -> None:
        for cart in self._carts.values():
            if item.id in cart.quantities and not math.isfinite(
                self._cart_price(cart.quantities, {item.id: price})
            ):
                raise PriceOverflowError

    def _cart_price(
        self, quantities: dict[int, int], price_overrides: dict[int, float] | None = None
    ) -> float:
        overrides = price_overrides or {}
        try:
            return math.fsum(
                overrides.get(item_id, self._items[item_id].price) * quantity
                for item_id, quantity in quantities.items()
                if not self._items[item_id].deleted
            )
        except OverflowError:
            return math.inf

    def _cart_response(self, cart: Cart) -> CartResponse:
        with self._metrics.stage("cart_snapshot"):
            response = CartResponse(
                id=cart.id,
                items=[
                    CartItemResponse(
                        id=item_id,
                        name=self._items[item_id].name,
                        quantity=quantity,
                        available=not self._items[item_id].deleted,
                    )
                    for item_id, quantity in cart.quantities.items()
                ],
                price=self._cart_price(cart.quantities),
            )
        self._metrics.cart_returned(response)
        return response

    @staticmethod
    def _item_response(item: Item) -> ItemResponse:
        return ItemResponse(id=item.id, name=item.name, price=item.price, deleted=item.deleted)


def _in_range(value: float, low: float | None, high: float | None) -> bool:
    return (low is None or value >= low) and (high is None or value <= high)

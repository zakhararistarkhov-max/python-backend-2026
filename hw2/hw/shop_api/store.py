import math
from collections import defaultdict
from collections.abc import Iterable, Sequence

from sqlalchemy import Engine, Row, Select, func, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session, sessionmaker

from .contracts import CartItemResponse, CartResponse, ItemResponse
from .metrics import ShopMetrics, shop_metrics
from .models import Base, CartItemRow, CartRow, ItemRow

MAX_KEY = 2**63 - 1
UPSERT_INSERTS = {"postgresql": postgresql.insert, "sqlite": sqlite.insert}


class NotFoundError(Exception):
    """Requested resource does not exist."""


class NotModifiedError(Exception):
    """Resource cannot be modified in its current state."""


class PriceOverflowError(Exception):
    """Operation would make a cart price non-finite."""


class ShopStore:
    """Transactional SQL storage for items and carts."""

    def __init__(self, engine: Engine, metrics: ShopMetrics = shop_metrics) -> None:
        if engine.dialect.name not in UPSERT_INSERTS:
            raise ValueError(f"Unsupported database dialect: {engine.dialect.name}")
        self._engine = engine
        self._metrics = metrics
        self._sessions = sessionmaker(engine, expire_on_commit=False)
        self._insert = UPSERT_INSERTS[engine.dialect.name]

    def create_schema(self) -> None:
        Base.metadata.create_all(self._engine)

    def dispose(self) -> None:
        self._engine.dispose()

    def create_item(self, name: str, price: float) -> ItemResponse:
        with self._metrics.stage("create_item"), self._sessions.begin() as session:
            item = ItemRow(name=name, price=price, deleted=False)
            session.add(item)
            session.flush()
            response = _item_response(item)
        self._metrics.item_created()
        return response

    def get_item(self, item_id: int) -> ItemResponse:
        with self._sessions.begin() as session:
            item = _find(session, ItemRow, item_id)
            if item is None or item.deleted:
                raise NotFoundError
            return _item_response(item)

    def list_items(
        self,
        offset: int,
        limit: int,
        min_price: float | None,
        max_price: float | None,
        show_deleted: bool,
    ) -> list[ItemResponse]:
        query = select(ItemRow).order_by(ItemRow.id).offset(_clamp(offset)).limit(_clamp(limit))
        if not show_deleted:
            query = query.where(ItemRow.deleted.is_(False))
        if min_price is not None:
            query = query.where(ItemRow.price >= min_price)
        if max_price is not None:
            query = query.where(ItemRow.price <= max_price)
        with self._metrics.stage("list_items"), self._sessions.begin() as session:
            return [_item_response(item) for item in session.scalars(query)]

    def update_item(
        self, item_id: int, name: str | None = None, price: float | None = None
    ) -> ItemResponse:
        with self._metrics.stage("update_item"), self._sessions.begin() as session:
            item = _find(session, ItemRow, item_id, lock=True)
            if item is None:
                raise NotFoundError
            if item.deleted:
                raise NotModifiedError
            if price is not None:
                self._check_price_change(session, item_id, price)
                item.price = price
            if name is not None:
                item.name = name
            session.flush()
            return _item_response(item)

    def delete_item(self, item_id: int) -> None:
        statement = update(ItemRow).where(ItemRow.id == item_id).values(deleted=True)
        with self._metrics.stage("delete_item"), self._sessions.begin() as session:
            if not _fits(item_id) or session.execute(statement).rowcount == 0:
                raise NotFoundError
        self._metrics.item_deleted()

    def create_cart(self) -> int:
        with self._metrics.stage("create_cart"), self._sessions.begin() as session:
            cart = CartRow()
            session.add(cart)
            session.flush()
            cart_id = cart.id
        self._metrics.cart_created()
        return cart_id

    def get_cart(self, cart_id: int) -> CartResponse:
        with self._sessions.begin() as session:
            if _find(session, CartRow, cart_id) is None:
                raise NotFoundError
            return self._cart_response(cart_id, self._lines(session, [cart_id])[cart_id])

    def list_carts(
        self,
        offset: int,
        limit: int,
        min_price: float | None,
        max_price: float | None,
        min_quantity: int | None,
        max_quantity: int | None,
    ) -> list[CartResponse]:
        total = func.coalesce(func.sum(CartItemRow.quantity), 0)
        query: Select = (
            select(CartRow.id).outerjoin(CartItemRow).group_by(CartRow.id).order_by(CartRow.id)
        )
        if min_quantity is not None:
            query = query.having(total >= min_quantity)
        if max_quantity is not None:
            query = query.having(total <= max_quantity)
        filter_price = min_price is not None or max_price is not None
        if not filter_price:
            query = query.offset(_clamp(offset)).limit(_clamp(limit))
        with self._metrics.stage("list_carts"), self._sessions.begin() as session:
            cart_ids = list(session.scalars(query))
            lines = self._lines(session, cart_ids)
            if filter_price:
                cart_ids = [
                    cart_id
                    for cart_id in cart_ids
                    if _in_range(_cart_price(lines[cart_id]), min_price, max_price)
                ][offset : offset + limit]
            return [self._cart_response(cart_id, lines[cart_id]) for cart_id in cart_ids]

    def add_to_cart(self, cart_id: int, item_id: int) -> CartResponse:
        with self._metrics.stage("add_to_cart"), self._sessions.begin() as session:
            item = _find(session, ItemRow, item_id, lock=True, shared=True)
            if _find(session, CartRow, cart_id, lock=True) is None:
                self._metrics.cart_add_rejected("missing_cart")
                raise NotFoundError
            if item is None or item.deleted:
                self._metrics.cart_add_rejected("missing_item")
                raise NotFoundError
            lines = self._lines(session, [cart_id])[cart_id]
            if not math.isfinite(_total([*_line_prices(lines), item.price])):
                self._metrics.cart_add_rejected("overflow")
                raise PriceOverflowError
            session.execute(
                self._insert(CartItemRow)
                .values(cart_id=cart_id, item_id=item_id, quantity=1)
                .on_conflict_do_update(
                    index_elements=[CartItemRow.cart_id, CartItemRow.item_id],
                    set_={"quantity": CartItemRow.quantity + 1},
                )
            )
            response = self._cart_response(cart_id, self._lines(session, [cart_id])[cart_id])
        self._metrics.cart_add_accepted()
        return response

    def _check_price_change(self, session: Session, item_id: int, price: float) -> None:
        cart_ids = list(
            session.scalars(
                select(CartRow.id)
                .join(CartItemRow)
                .where(CartItemRow.item_id == item_id)
                .order_by(CartRow.id)
                .with_for_update(of=CartRow)
            )
        )
        for lines in self._lines(session, cart_ids).values():
            if not math.isfinite(_cart_price(lines, {item_id: price})):
                raise PriceOverflowError

    @staticmethod
    def _lines(session: Session, cart_ids: Sequence[int]) -> dict[int, list[Row]]:
        lines: dict[int, list[Row]] = defaultdict(list)
        if not cart_ids:
            return lines
        rows = session.execute(
            select(
                CartItemRow.cart_id,
                CartItemRow.quantity,
                ItemRow.id,
                ItemRow.name,
                ItemRow.price,
                ItemRow.deleted,
            )
            .join(ItemRow, ItemRow.id == CartItemRow.item_id)
            .where(CartItemRow.cart_id.in_(cart_ids))
            .order_by(CartItemRow.cart_id, CartItemRow.item_id)
        )
        for row in rows:
            lines[row.cart_id].append(row)
        return lines

    def _cart_response(self, cart_id: int, lines: list[Row]) -> CartResponse:
        with self._metrics.stage("cart_snapshot"):
            response = CartResponse(
                id=cart_id,
                items=[
                    CartItemResponse(
                        id=line.id,
                        name=line.name,
                        quantity=line.quantity,
                        available=not line.deleted,
                    )
                    for line in lines
                ],
                price=_cart_price(lines),
            )
        self._metrics.cart_returned(response)
        return response


def _find(
    session: Session, model: type, key: int, lock: bool = False, shared: bool = False
) -> object | None:
    if not _fits(key):
        return None
    with_for_update = {"read": shared} if lock else None
    return session.get(model, key, with_for_update=with_for_update)


def _fits(key: int) -> bool:
    return -MAX_KEY <= key <= MAX_KEY


def _clamp(value: int) -> int:
    return min(value, MAX_KEY)


def _line_prices(
    lines: Iterable[Row], price_overrides: dict[int, float] | None = None
) -> Iterable[float]:
    overrides = price_overrides or {}
    return (
        overrides.get(line.id, line.price) * line.quantity for line in lines if not line.deleted
    )


def _total(prices: Iterable[float]) -> float:
    try:
        return math.fsum(prices)
    except OverflowError:
        return math.inf


def _cart_price(lines: Iterable[Row], price_overrides: dict[int, float] | None = None) -> float:
    return _total(_line_prices(lines, price_overrides))


def _item_response(item: ItemRow) -> ItemResponse:
    return ItemResponse(id=item.id, name=item.name, price=item.price, deleted=item.deleted)


def _in_range(value: float, low: float | None, high: float | None) -> bool:
    return (low is None or value >= low) and (high is None or value <= high)

from sqlalchemy import BigInteger, CheckConstraint, Double, ForeignKey, Identity, Integer, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

Key = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    """Declarative base of the shop tables."""


class ItemRow(Base):
    """Row of the items table."""

    __tablename__ = "items"
    __table_args__ = (CheckConstraint("price >= 0", name="items_price_non_negative"),)

    id: Mapped[int] = mapped_column(Key, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    price: Mapped[float] = mapped_column(Double)
    deleted: Mapped[bool] = mapped_column(default=False, server_default="false")


class CartRow(Base):
    """Row of the carts table."""

    __tablename__ = "carts"

    id: Mapped[int] = mapped_column(Key, Identity(), primary_key=True)


class CartItemRow(Base):
    """Quantity of one item in one cart."""

    __tablename__ = "cart_items"
    __table_args__ = (CheckConstraint("quantity > 0", name="cart_items_quantity_positive"),)

    cart_id: Mapped[int] = mapped_column(Key, ForeignKey("carts.id"), primary_key=True)
    item_id: Mapped[int] = mapped_column(Key, ForeignKey("items.id"), primary_key=True, index=True)
    quantity: Mapped[int]

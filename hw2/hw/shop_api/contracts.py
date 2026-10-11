from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

Price = Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]


class ItemRequest(BaseModel):
    """Full item body for creation and replacement."""

    model_config = ConfigDict(extra="forbid")

    name: str
    price: Price


class PatchItemRequest(BaseModel):
    """Partial item update body."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    price: Price | None = None

    @field_validator("name", "price", mode="before")
    @classmethod
    def reject_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("Field may be omitted but not null")
        return value


class ItemResponse(BaseModel):
    """Item resource."""

    id: int
    name: str
    price: float
    deleted: bool


class CartItemResponse(BaseModel):
    """Item line inside a cart."""

    id: int
    name: str
    quantity: int
    available: bool


class CartResponse(BaseModel):
    """Cart resource."""

    id: int
    items: list[CartItemResponse]
    price: float


class CartCreatedResponse(BaseModel):
    """Identifier of a newly created cart."""

    id: int

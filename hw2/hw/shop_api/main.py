from http import HTTPStatus
from typing import Annotated

from fastapi import FastAPI, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import NonNegativeInt, PositiveInt

from .chat import ChatRoomManager
from .contracts import (
    CartCreatedResponse,
    CartResponse,
    ItemRequest,
    ItemResponse,
    PatchItemRequest,
)
from .store import NotFoundError, NotModifiedError, PriceOverflowError, ShopStore

PriceQuery = Annotated[float | None, Query(ge=0, allow_inf_nan=False)]

app = FastAPI(title="Shop API")
store = ShopStore()
chat_rooms = ChatRoomManager()


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [{k: v for k, v in error.items() if k != "input"} for error in exc.errors()]
    return JSONResponse({"detail": jsonable_encoder(errors)}, HTTPStatus.UNPROCESSABLE_ENTITY)


@app.exception_handler(NotFoundError)
async def handle_not_found(request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse({"detail": "Resource not found"}, HTTPStatus.NOT_FOUND)


@app.exception_handler(NotModifiedError)
async def handle_not_modified(request: Request, exc: NotModifiedError) -> Response:
    return Response(status_code=HTTPStatus.NOT_MODIFIED)


@app.exception_handler(PriceOverflowError)
async def handle_price_overflow(request: Request, exc: PriceOverflowError) -> JSONResponse:
    return JSONResponse({"detail": "Cart price would overflow"}, HTTPStatus.CONFLICT)


@app.post("/cart", status_code=HTTPStatus.CREATED)
def create_cart(response: Response) -> CartCreatedResponse:
    cart_id = store.create_cart()
    response.headers["location"] = f"/cart/{cart_id}"
    return CartCreatedResponse(id=cart_id)


@app.get("/cart/{cart_id}")
def get_cart(cart_id: int) -> CartResponse:
    return store.get_cart(cart_id)


@app.get("/cart")
def list_carts(
    offset: Annotated[NonNegativeInt, Query()] = 0,
    limit: Annotated[PositiveInt, Query()] = 10,
    min_price: PriceQuery = None,
    max_price: PriceQuery = None,
    min_quantity: Annotated[NonNegativeInt | None, Query()] = None,
    max_quantity: Annotated[NonNegativeInt | None, Query()] = None,
) -> list[CartResponse]:
    return store.list_carts(offset, limit, min_price, max_price, min_quantity, max_quantity)


@app.post("/cart/{cart_id}/add/{item_id}")
def add_to_cart(cart_id: int, item_id: int) -> CartResponse:
    return store.add_to_cart(cart_id, item_id)


@app.post("/item", status_code=HTTPStatus.CREATED)
def create_item(body: ItemRequest, response: Response) -> ItemResponse:
    item = store.create_item(body.name, body.price)
    response.headers["location"] = f"/item/{item.id}"
    return item


@app.get("/item/{item_id}")
def get_item(item_id: int) -> ItemResponse:
    return store.get_item(item_id)


@app.get("/item")
def list_items(
    offset: Annotated[NonNegativeInt, Query()] = 0,
    limit: Annotated[PositiveInt, Query()] = 10,
    min_price: PriceQuery = None,
    max_price: PriceQuery = None,
    show_deleted: bool = False,
) -> list[ItemResponse]:
    return store.list_items(offset, limit, min_price, max_price, show_deleted)


@app.put("/item/{item_id}")
def replace_item(item_id: int, body: ItemRequest) -> ItemResponse:
    return store.update_item(item_id, body.name, body.price)


@app.patch("/item/{item_id}")
def patch_item(item_id: int, body: PatchItemRequest) -> ItemResponse:
    return store.update_item(item_id, body.name, body.price)


@app.delete("/item/{item_id}")
def delete_item(item_id: int) -> None:
    store.delete_item(item_id)


@app.websocket("/chat/{chat_name}")
async def chat(websocket: WebSocket, chat_name: str) -> None:
    await chat_rooms.connect(chat_name, websocket)
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            text = message.get("text")
            if text is None:
                await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
                break
            await chat_rooms.broadcast(chat_name, websocket, text)
    except WebSocketDisconnect:
        pass
    finally:
        chat_rooms.disconnect(chat_name, websocket)

import json
import math
import re
from http import HTTPStatus
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class ApiError(Exception):
    """Request failure carrying the HTTP status to respond with."""

    def __init__(self, status: HTTPStatus, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class MathApi:
    """ASGI application serving factorial, fibonacci and mean endpoints."""

    INTEGER = re.compile(r"-?[0-9]+")
    NUMBER = re.compile(r"-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?")
    MAX_FACTORIAL_N = 1000
    MAX_FIBONACCI_N = 10000

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
        elif scope["type"] == "http":
            await self._http(scope, receive, send)

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            handler = self._route(scope["path"])
            if scope["method"] != "GET":
                raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "Only GET is allowed")
            status, payload = HTTPStatus.OK, {"result": await handler(scope, receive)}
        except ApiError as error:
            status, payload = error.status, {"detail": error.detail}
        await self._respond(send, status, payload)

    def _route(self, path: str) -> Callable[[Scope, Receive], Awaitable[int | float]]:
        if path == "/factorial":
            return self._factorial_endpoint
        if path == "/mean":
            return self._mean_endpoint
        if path.startswith("/fibonacci/"):
            return self._fibonacci_endpoint
        raise ApiError(HTTPStatus.NOT_FOUND, "Not found")

    async def _factorial_endpoint(self, scope: Scope, receive: Receive) -> int:
        values = self._query(scope).get("n")
        if values is None:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "Query parameter n is required")
        return self.factorial(self._parse_integer(values[0]))

    async def _fibonacci_endpoint(self, scope: Scope, receive: Receive) -> int:
        return self.fibonacci(self._parse_integer(scope["path"].removeprefix("/fibonacci/")))

    async def _mean_endpoint(self, scope: Scope, receive: Receive) -> float:
        body = await self._read_body(receive)
        if body:
            return self.mean(self._parse_json_numbers(body))
        values = self._query(scope).get("numbers")
        if values is None:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "A JSON array of numbers is required")
        return self.mean(self._parse_query_numbers(values[0]))

    def factorial(self, n: int) -> int:
        self._check_range(n, self.MAX_FACTORIAL_N)
        return math.factorial(n)

    def fibonacci(self, n: int) -> int:
        self._check_range(n, self.MAX_FIBONACCI_N)
        current, following = 0, 1
        for _ in range(n):
            current, following = following, current + following
        return current

    def mean(self, numbers: list[float]) -> float:
        if not numbers:
            raise ApiError(HTTPStatus.BAD_REQUEST, "At least one number is required")
        return math.fsum(number / len(numbers) for number in numbers)

    @staticmethod
    def _check_range(n: int, maximum: int) -> None:
        if not 0 <= n <= maximum:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"n must be between 0 and {maximum}")

    @staticmethod
    def _query(scope: Scope) -> dict[str, list[str]]:
        return parse_qs(scope["query_string"].decode("latin-1"), keep_blank_values=True)

    @staticmethod
    async def _read_body(receive: Receive) -> bytes:
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        return bytes(body)

    def _parse_integer(self, text: str) -> int:
        if not self.INTEGER.fullmatch(text):
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "n must be an integer")
        return int(text)

    def _parse_json_numbers(self, body: bytes) -> list[float]:
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "Body must be valid JSON") from None
        if not isinstance(data, list) or not all(
            isinstance(item, int | float) and not isinstance(item, bool) for item in data
        ):
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "Body must be a JSON array of numbers")
        return [self._finite(item) for item in data]

    def _parse_query_numbers(self, text: str) -> list[float]:
        if not text:
            return []
        items = text.split(",")
        if not all(self.NUMBER.fullmatch(item) for item in items):
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "numbers must be comma-separated numbers")
        return [self._finite(item) for item in items]

    @staticmethod
    def _finite(value: int | float | str) -> float:
        try:
            number = float(value)
        except OverflowError:
            number = math.inf
        if not math.isfinite(number):
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "Numbers must be finite")
        return number

    @staticmethod
    async def _respond(send: Send, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


math_api = MathApi()


async def application(
    scope: dict[str, Any],
    receive: Callable[[], Awaitable[dict[str, Any]]],
    send: Callable[[dict[str, Any]], Awaitable[None]],
):
    await math_api(scope, receive, send)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:application", host="0.0.0.0", port=8000, reload=True)

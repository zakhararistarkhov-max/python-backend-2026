import secrets

from fastapi import WebSocket


class ChatRoomManager:
    """Tracks WebSocket participants per chat room and relays their messages."""

    def __init__(self) -> None:
        self._rooms: dict[str, dict[WebSocket, str]] = {}

    async def connect(self, room: str, websocket: WebSocket) -> str:
        await websocket.accept()
        username = f"user-{secrets.token_hex(4)}"
        self._rooms.setdefault(room, {})[websocket] = username
        return username

    def disconnect(self, room: str, websocket: WebSocket) -> None:
        members = self._rooms.get(room)
        if members is None:
            return
        members.pop(websocket, None)
        if not members:
            del self._rooms[room]

    async def broadcast(self, room: str, sender: WebSocket, message: str) -> None:
        members = self._rooms.get(room, {})
        username = members.get(sender)
        if username is None:
            return
        text = f"{username} :: {message}"
        for websocket in list(members):
            if websocket is sender:
                continue
            try:
                await websocket.send_text(text)
            except Exception:
                self.disconnect(room, websocket)

    def size(self, room: str) -> int:
        return len(self._rooms.get(room, {}))

    def room_names(self) -> list[str]:
        return list(self._rooms)

from fastapi import WebSocket
from typing import Dict, List, Set


class ConnectionManager:

    def __init__(self):
        self.admin_connections: Set[WebSocket] = set()
        self.user_connections: Dict[str, WebSocket] = {}
        self.message_history: Dict[str, List[dict]] = {}
        self.telegram_chat_ids: Dict[int, str] = (
            {}
        )  # Mapping telegram chat_id to user_id

    async def connect_admin(self, websocket: WebSocket):
        await websocket.accept()
        self.admin_connections.add(websocket)
        print("Admin connected")

    async def connect_user(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.user_connections[user_id] = websocket
        print(f"User {user_id} connected")

    def disconnect_admin(self, websocket: WebSocket):
        self.admin_connections.remove(websocket)
        print("Admin disconnected")

    def disconnect_user(self, user_id: str):
        self.user_connections.pop(user_id, None)
        print(f"User {user_id} disconnected")

    async def broadcast_to_admins(self, message: dict):
        for connection in self.admin_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                print(f"Error sending to admin: {e}")

    async def send_to_user(self, user_id: str, message: dict):
        if user_id in self.user_connections:
            try:
                await self.user_connections[user_id].send_json(message)
            except Exception as e:
                print(f"Error sending to user {user_id}: {e}")

    def store_message(self, user_id: str, message: dict):
        if user_id not in self.message_history:
            self.message_history[user_id] = []
        self.message_history[user_id].append(message)

    def register_telegram_user(self, chat_id: int, user_id: str):
        self.telegram_chat_ids[chat_id] = user_id
        print(f"Registered Telegram chat_id {chat_id} for user {user_id}")

import requests
from fastapi import WebSocket, HTTPException
from typing import Dict, List, Set, Optional
from enum import Enum
from pydantic import BaseModel
from datetime import datetime

from config import TELEGRAM_API_URL


class UserType(Enum):
    ADMIN = "admin"
    MECHANIC = "mechanic"
    CUSTOMER = "customer"


class User(BaseModel):
    id: str
    type: UserType
    api_key: Optional[str] = None
    phone: Optional[str] = None
    telegram_id: Optional[int] = None


class Message(BaseModel):
    from_user: str
    to_user: str
    content: str
    timestamp: datetime
    message_type: str = "text"
    source: str = "web"

    def to_json(self):
        return {
            "from_user": self.from_user,
            "to_user": self.to_user,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "message_type": self.message_type,
            "source": self.source
        }


class ConnectionManager:

    def __init__(self):
        self.connections: Dict[str, WebSocket] = {}
        self.users: Dict[str, User] = {}
        self.message_history: Dict[str, List[Message]] = {}
        self.api_keys: Dict[str, User] = {}
        self.phone_numbers: Dict[str, User] = {}
        self.telegram_ids: Dict[int, User] = {}

    async def register_user(self, user: User) -> None:
        self.users[user.id] = user
        if user.api_key:
            self.api_keys[user.api_key] = user
        if user.phone:
            self.phone_numbers[user.phone] = user
        if user.telegram_id:
            self.telegram_ids[user.telegram_id] = user

    def get_user_by_api_key(self, api_key: str) -> Optional[User]:
        return self.api_keys.get(api_key)

    def get_user_by_phone(self, phone: str) -> Optional[User]:
        return self.phone_numbers.get(phone)

    def get_user_by_telegram_id(self, telegram_id: int) -> Optional[User]:
        return self.telegram_ids.get(telegram_id)

    def get_active_admins(self) -> List[str]:
        return [
            user_id for user_id, user in self.users.items()
            if user.type == UserType.ADMIN and user_id in self.connections
        ]

    def get_user_telegram_id(self, user_id: str) -> Optional[int]:
        user = self.users.get(user_id)
        return user.telegram_id if user else None

    async def send_message_to_active_admin(self, message: Message) -> bool:
        active_admins = self.get_active_admins()
        if not active_admins:
            return False

        admin_id = active_admins[0]
        message.to_user = admin_id
        await self.send_message(message)
        return True

    async def send_telegram_message(self, chat_id: int, text: str):
        url = f"{TELEGRAM_API_URL}/sendMessage"
        data = {
            "chat_id": chat_id,
            "text": text
        }
        response = requests.post(url, json=data)
        return response.json()

    async def connect(self, websocket: WebSocket, user_id: str) -> None:
        await websocket.accept()
        self.connections[user_id] = websocket

    async def disconnect(self, user_id: str) -> None:
        self.connections.pop(user_id, None)

    def store_message(self, message: Message) -> None:
        for user_id in [message.from_user, message.to_user]:
            if user_id not in self.message_history:
                self.message_history[user_id] = []
            self.message_history[user_id].append(message)

    async def send_message(self, message: Message) -> None:
        self.store_message(message)

        if message.to_user.startswith("telegram_"):
            telegram_id = int(message.to_user.split("_")[1])
            await self.send_telegram_message(telegram_id, message.content)

        if message.to_user in self.connections:
            await self.connections[message.to_user].send_json(message.to_json())

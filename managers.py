import os
from uuid import uuid4

import aiofiles
import requests
from fastapi import WebSocket, HTTPException, UploadFile
from typing import Dict, List, Optional
from enum import Enum
from pydantic import BaseModel
from datetime import datetime

from config import TELEGRAM_API_URL, UPLOAD_DIR, MAX_UPLOAD_SIZE, ALLOWED_IMAGE_TYPES, ALLOWED_AUDIO_TYPES, \
    ALLOWED_VIDEO_TYPES, ALLOWED_VOICE_TYPES


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


class MessageType(Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    VOICE = "voice"
    FILE = "file"


class Message(BaseModel):
    from_user: str
    to_user: str
    content: str
    timestamp: datetime
    message_type: MessageType = MessageType.TEXT
    file_path: Optional[str] = None
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    source: str = "web"

    def to_json(self):
        return {
            "from_user": self.from_user,
            "to_user": self.to_user,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "message_type": self.message_type.value,
            "file_path": self.file_path,
            "file_name": self.file_name,
            "mime_type": self.mime_type,
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

    async def send_telegram_message(self, chat_id: int, text: str, file_path: Optional[str] = None,
                                    message_type: Optional[MessageType] = None):
        """Send message to Telegram, optionally with a file"""
        try:
            if file_path and message_type:
                method = {
                    MessageType.IMAGE: "sendPhoto",
                    MessageType.AUDIO: "sendAudio",
                    MessageType.VIDEO: "sendVideo",
                    MessageType.VOICE: "sendVoice",
                    MessageType.FILE: "sendDocument"
                }.get(message_type)

                if not method:
                    method = "sendMessage"

                url = f"{TELEGRAM_API_URL}/{method}"

                if not os.path.isabs(file_path):
                    full_path = os.path.join(UPLOAD_DIR, file_path)
                else:
                    full_path = file_path

                print(f"Attempting to send file: {full_path}")

                if os.path.exists(full_path):
                    with open(full_path, 'rb') as file:
                        param_name = {
                            'sendPhoto': 'photo',
                            'sendAudio': 'audio',
                            'sendVideo': 'video',
                            'sendVoice': 'voice',
                            'sendDocument': 'document'
                        }.get(method, 'document')

                        files = {
                            param_name: file
                        }
                        data = {
                            "chat_id": chat_id,
                        }

                        if method != "sendVoice":
                            data["caption"] = text

                        print(f"Sending to Telegram API: {url}")
                        print(f"With data: {data}")
                        response = requests.post(url, data=data, files=files)
                        print(f"Telegram API response: {response.text}")

                        if response.status_code != 200:
                            print(f"Error sending file to Telegram: {response.text}")
                            # Fallback to text message
                            return await self.send_telegram_message(chat_id, f"{text} (File upload failed)")
                else:
                    print(f"File not found: {full_path}")
                    return await self.send_telegram_message(chat_id, f"{text} (File not found)")
            else:
                url = f"{TELEGRAM_API_URL}/sendMessage"
                data = {
                    "chat_id": chat_id,
                    "text": text
                }
                response = requests.post(url, json=data)
                print(f"Sent text message to Telegram: {response.text}")

            return response.json()

        except Exception as e:
            print(f"Error in send_telegram_message: {str(e)}")
            # Final fallback - try to send just the text
            url = f"{TELEGRAM_API_URL}/sendMessage"
            data = {
                "chat_id": chat_id,
                "text": f"{text} (Error sending file: {str(e)})"
            }
            return requests.post(url, json=data).json()

    async def send_message(self, message: Message) -> None:
        try:
            self.store_message(message)

            if message.to_user.startswith("telegram_"):
                telegram_id = int(message.to_user.split("_")[1])
                print(f"Sending message to Telegram {telegram_id}")
                print(f"Message type: {message.message_type}")
                print(f"File path: {message.file_path}")
                print(f"Content: {message.content}")

                await self.send_telegram_message(
                    telegram_id,
                    message.content,
                    message.file_path,
                    message.message_type
                )

            if message.to_user in self.connections:
                await self.connections[message.to_user].send_json(message.to_json())

        except Exception as e:
            print(f"Error in send_message: {str(e)}")
            raise


class FileManager:
    @staticmethod
    async def save_file(file: UploadFile, message_type: MessageType) -> tuple[str, str]:
        """Save uploaded file and return its path and filename"""

        type_dir = os.path.join(UPLOAD_DIR, message_type.value)
        os.makedirs(type_dir, exist_ok=True)

        ext = os.path.splitext(file.filename)[1]
        filename = f"{uuid4()}{ext}"
        filepath = os.path.join(type_dir, filename)

        async with aiofiles.open(filepath, 'wb') as out_file:
            while content := await file.read(1024):
                await out_file.write(content)

        return filepath, filename

    @staticmethod
    def validate_file(file: UploadFile, message_type: MessageType):
        """Validate file size and type"""
        if file.size > MAX_UPLOAD_SIZE:
            raise HTTPException(status_code=400, detail="File too large")

        content_type = file.content_type
        if message_type == MessageType.IMAGE and content_type not in ALLOWED_IMAGE_TYPES:
            raise HTTPException(status_code=400, detail="Invalid image type")
        elif message_type == MessageType.AUDIO and content_type not in ALLOWED_AUDIO_TYPES:
            raise HTTPException(status_code=400, detail="Invalid audio type")
        elif message_type == MessageType.VIDEO and content_type not in ALLOWED_VIDEO_TYPES:
            raise HTTPException(status_code=400, detail="Invalid video type")
        elif message_type == MessageType.VOICE and content_type not in ALLOWED_VOICE_TYPES:
            raise HTTPException(status_code=400, detail="Invalid voice message type")

    @staticmethod
    async def save_telegram_file(content: bytes, file_id: str, message_type: MessageType) -> tuple[str, str]:
        """
        Save file received from Telegram and return its path and filename
        Returns: (file_path, relative_path)
        """
        type_dir = os.path.join(UPLOAD_DIR, message_type.value)
        os.makedirs(type_dir, exist_ok=True)

        filename = f"{file_id}_file.jpg"
        filepath = os.path.join(type_dir, filename)

        relative_path = f"{message_type.value}/{filename}"

        async with aiofiles.open(filepath, 'wb') as out_file:
            await out_file.write(content)

        return filepath, relative_path

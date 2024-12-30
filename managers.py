import os
import traceback
from uuid import uuid4
from datetime import datetime
from typing import Dict, List, Optional
from enum import Enum
from pydantic import BaseModel

import aiofiles
import requests
from fastapi import WebSocket, HTTPException, UploadFile

from config import (
    TELEGRAM_API_URL, UPLOAD_DIR, MAX_UPLOAD_SIZE,
    ALLOWED_IMAGE_TYPES, ALLOWED_AUDIO_TYPES,
    ALLOWED_VIDEO_TYPES, ALLOWED_VOICE_TYPES
)
from mongodb_manager import MongoDBManager


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
    def __init__(self, db_manager: 'MongoDBManager'):
        self.db = db_manager
        self.chat_manager = ChatManager(db_manager)

    async def register_user(self, user: User) -> None:
        await self.db.create_user(user.dict())

    async def get_user_by_api_key(self, api_key: str) -> Optional[User]:
        user_data = await self.db.get_user_by_field("api_key", api_key)
        return User(**user_data) if user_data else None

    async def get_user_by_phone(self, phone: str) -> Optional[User]:
        user_data = await self.db.get_user_by_field("phone", phone)
        return User(**user_data) if user_data else None

    async def get_user_by_telegram_id(self, telegram_id: int) -> Optional[User]:
        user_data = await self.db.get_user_by_field("telegram_id", telegram_id)
        if not user_data:
            return None

        return User(
            id=str(user_data.get('_id')),  # Convert ObjectId to string
            type=UserType[user_data.get('type', 'CUSTOMER').upper()],
            telegram_id=user_data.get('telegram_id'),
            name=user_data.get('name', 'Unknown')
        )

    async def get_active_staff(self) -> List[str]:
        active_staff = await self.db.get_active_staff()
        print(f"Getting active staff: {active_staff}")
        return active_staff

    async def connect(self, websocket: WebSocket, user_id: str) -> None:
        await websocket.accept()
        self.db.register_connection(user_id, websocket)
        await self.db.update_user_online_status(user_id, True)
        print(f"Registered connection for user {user_id}")

    async def disconnect(self, user_id: str) -> None:
        self.db.remove_connection(user_id)
        await self.db.update_user_online_status(user_id, False)

    async def send_telegram_message(
            self, chat_id: int, text: str,
            file_path: Optional[str] = None,
            message_type: Optional[MessageType] = None
    ):
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

                if os.path.exists(full_path):
                    with open(full_path, 'rb') as file:
                        param_name = method.replace('send', '').lower()
                        files = {param_name: file}
                        data = {"chat_id": chat_id}

                        if method != "sendVoice":
                            data["caption"] = text

                        response = requests.post(url, data=data, files=files)
                        if response.status_code != 200:
                            return await self.send_telegram_message(
                                chat_id,
                                f"{text} (File upload failed)"
                            )
                else:
                    return await self.send_telegram_message(
                        chat_id,
                        f"{text} (File not found)"
                    )
            else:
                url = f"{TELEGRAM_API_URL}/sendMessage"
                data = {"chat_id": chat_id, "text": text}
                response = requests.post(url, json=data)

            return response.json()

        except Exception as e:
            print(f"Error in send_telegram_message: {str(e)}")
            url = f"{TELEGRAM_API_URL}/sendMessage"
            data = {
                "chat_id": chat_id,
                "text": f"{text} (Error sending file: {str(e)})"
            }
            return requests.post(url, json=data).json()

    async def send_message(self, message: Message, chat_id: Optional[str] = None) -> None:
        try:
            if not chat_id:
                # Спочатку шукаємо існуючий чат
                existing_chat = await self.db.get_chat_by_users(message.from_user, message.to_user)
                if existing_chat:
                    chat_id = existing_chat['chat_id']
                else:
                    if message.from_user.startswith('customer_'):
                        active_staff = await self.db.get_active_staff()
                        if not active_staff:
                            raise ValueError("No active staff members available")
                        message.to_user = active_staff[0]

                    chat = await self.chat_manager.get_or_create_chat(
                        message.from_user,
                        message.to_user
                    )
                    chat_id = chat['chat_id']

            await self.chat_manager.add_message_to_chat(chat_id, message)

            if message.to_user.startswith('telegram_'):
                telegram_id = int(message.to_user.split('_')[1])
                try:
                    await self.send_telegram_message(
                        telegram_id,
                        message.content,
                        message.file_path,
                        message.message_type
                    )
                except Exception as e:
                    print(f"Error sending telegram message: {str(e)}")
                    # Спробуємо відправити хоча б текст
                    await self.send_telegram_message(
                        telegram_id,
                        f"{message.content} (Error sending file: {str(e)})"
                    )
            else:
                connection = self.db.active_connections.get(message.to_user)
                if connection:
                    await connection.send_json({
                        **message.to_json(),
                        "chat_id": chat_id
                    })

        except Exception as e:
            print(f"Error in send_message: {str(e)}")
            raise


class ChatManager:
    def __init__(self, db_manager: 'MongoDBManager'):
        self.db = db_manager

    async def create_chat(
            self,
            admin_id: str,
            customer_id: str,
            title: Optional[str] = None
    ) -> str:
        return await self.db.create_chat(admin_id, customer_id, title)

    async def get_chat(self, chat_id: str) -> Optional[dict]:
        return await self.db.get_chat(chat_id)

    async def get_or_create_chat(self, from_user: str, to_user: str) -> dict:
        chat = await self.db.get_chat_by_users(from_user, to_user)
        if not chat:
            admin_id = from_user if from_user.startswith(('admin_', 'mechanic_')) else to_user
            customer_id = to_user if from_user.startswith(('admin_', 'mechanic_')) else from_user
            chat_id = await self.create_chat(admin_id, customer_id)
            chat = await self.get_chat(chat_id)
        return chat

    async def add_message_to_chat(self, chat_id: str, message: Message) -> None:
        await self.db.add_message(chat_id, message.dict())

    def get_chat_messages(self, chat_id: str, limit: int = 100) -> List[Message]:
        messages = self.db.get_chat_messages(chat_id, limit)
        return [Message(**msg) for msg in messages]

    async def mark_chat_as_read(self, chat_id: str, user_id: str) -> None:
        await self.db.mark_chat_as_read(chat_id, user_id)


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
        allowed_types = {
            MessageType.IMAGE: ALLOWED_IMAGE_TYPES,
            MessageType.AUDIO: ALLOWED_AUDIO_TYPES,
            MessageType.VIDEO: ALLOWED_VIDEO_TYPES,
            MessageType.VOICE: ALLOWED_VOICE_TYPES
        }

        if message_type in allowed_types and content_type not in allowed_types[message_type]:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {message_type.value} type"
            )

    @staticmethod
    async def save_telegram_file(
            content: bytes,
            file_id: str,
            message_type: MessageType
    ) -> tuple[str, str]:
        type_dir = os.path.join(UPLOAD_DIR, message_type.value)
        os.makedirs(type_dir, exist_ok=True)

        extensions = {
            MessageType.IMAGE: '.jpg',
            MessageType.VIDEO: '.mp4',
            MessageType.AUDIO: '.mp3',
            MessageType.VOICE: '.ogg',
            MessageType.FILE: ''
        }
        ext = extensions.get(message_type, '')

        filename = f"{file_id}{ext}"
        filepath = os.path.join(type_dir, filename)

        async with aiofiles.open(filepath, 'wb') as out_file:
            await out_file.write(content)

        return filepath, filename


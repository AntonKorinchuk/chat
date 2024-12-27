import os
import traceback
from uuid import uuid4

import aiofiles
import requests
from fastapi import WebSocket, HTTPException, UploadFile
from typing import Dict, List, Optional, Set
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
        self.chat_manager = ChatManager()

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

    def get_active_staff(self) -> List[str]:
        """Get list of active admin and mechanic IDs"""
        return [
            user_id for user_id, user in self.users.items()
            if (user.type in [UserType.ADMIN, UserType.MECHANIC]) and user_id in self.connections
        ]

    def get_user_telegram_id(self, user_id: str) -> Optional[int]:
        user = self.users.get(user_id)
        return user.telegram_id if user else None

    async def send_message_to_active_staff(self, message: Message) -> bool:
        """Send message to first available staff member (admin or mechanic)"""
        active_staff = self.get_active_staff()
        if not active_staff:
            return False

        staff_id = active_staff[0]
        message.to_user = staff_id
        await self.send_message(message)
        return True

    async def connect(self, websocket: WebSocket, user_id: str) -> None:
        await websocket.accept()


        if user_id in self.connections:
            await self.connections[user_id].close(code=4000, reason="New connection established")

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

    async def get_or_create_chat(self, from_user: str, to_user: str) -> str:
        """Get existing chat or create new one"""
        # Перевіряємо існуючі чати
        for chat_id, chat in self.chat_manager.chats.items():
            if (chat.admin_id == from_user and chat.customer_id == to_user) or \
                    (chat.admin_id == to_user and chat.customer_id == from_user):
                return chat_id

        # Визначаємо хто адмін, а хто клієнт
        admin_id = from_user if from_user.startswith('admin_') else to_user
        customer_id = to_user if from_user.startswith('admin_') else from_user

        # Створюємо новий чат
        chat = await self.chat_manager.create_chat(admin_id, customer_id)
        return chat.id

    async def send_message(self, message: Message, chat_id: Optional[str] = None) -> None:
        try:
            print(f"Sending message: {message.to_json()}")
            print(f"Current connections: {list(self.connections.keys())}")

            if not chat_id:
                if message.from_user.startswith('customer_'):
                    active_staff = self.get_active_staff()
                    print(f"Active staff members: {active_staff}")
                    if not active_staff:
                        raise ValueError("No active staff members available")
                    message.to_user = active_staff[0]
                    print(f"Selected staff member: {message.to_user}")

                chat_id = await self.get_or_create_chat(message.from_user, message.to_user)
                print(f"Created/Retrieved chat_id: {chat_id}")

            await self.chat_manager.add_message_to_chat(chat_id, message)
            print(f"Message added to chat: {chat_id}")

            # Перевіряємо, чи отримувач є Telegram користувачем
            if message.to_user.startswith('telegram_'):
                telegram_id = int(message.to_user.split('_')[1])
                print(f"Sending to Telegram user: {telegram_id}")

                # Відправляємо повідомлення через Telegram API
                await self.send_telegram_message(
                    telegram_id,
                    message.content,
                    message.file_path,
                    message.message_type
                )
            elif message.to_user in self.connections:
                print(f"Sending to WebSocket connection: {message.to_user}")
                await self.connections[message.to_user].send_json({
                    **message.to_json(),
                    "chat_id": chat_id
                })
            else:
                print(f"No WebSocket connection for user: {message.to_user}")
                # Якщо немає з'єднання, зберігаємо повідомлення для подальшої доставки
                if message.to_user not in self.message_history:
                    self.message_history[message.to_user] = []
                self.message_history[message.to_user].append(message)

        except Exception as e:
            print(f"Error in send_message: {str(e)}")
            print(f"Error traceback: {traceback.format_exc()}")
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

        relative_path = filename

        async with aiofiles.open(filepath, 'wb') as out_file:
            await out_file.write(content)

        return filepath, relative_path


class ChatStatus(Enum):
    ACTIVE = "active"
    CLOSED = "closed"
    PENDING = "pending"


class Chat(BaseModel):
    id: str
    admin_id: str
    customer_id: str
    status: ChatStatus
    created_at: datetime
    updated_at: datetime
    title: Optional[str] = None
    last_message: Optional[str] = None
    unread_count: int = 0


class ChatManager:
    def __init__(self):
        self.chats: Dict[str, Chat] = {}
        self.admin_chats: Dict[str, Set[str]] = {}  # admin_id -> set of chat_ids
        self.customer_chats: Dict[str, Set[str]] = {}  # customer_id -> set of chat_ids
        self.chat_messages: Dict[str, List[Message]] = {}  # chat_id -> list of messages

    async def create_chat(self, staff_id: str, customer_id: str, title: Optional[str] = None) -> Chat:
        chat_id = f"chat_{len(self.chats) + 1}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

        # Determine if staff is admin or mechanic
        is_admin = staff_id.startswith('admin_')
        staff_type = "admin" if is_admin else "mechanic"

        chat = Chat(
            id=chat_id,
            admin_id=staff_id,  # We'll keep the field name as admin_id but it can store mechanic_id too
            customer_id=customer_id,
            status=ChatStatus.ACTIVE,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            title=title or f"Chat with {customer_id}",
            staff_type=staff_type  # Add new field to track staff type
        )

        self.chats[chat_id] = chat

        # Update relationships
        if staff_id not in self.admin_chats:
            self.admin_chats[staff_id] = set()
        self.admin_chats[staff_id].add(chat_id)

        if customer_id not in self.customer_chats:
            self.customer_chats[customer_id] = set()
        self.customer_chats[customer_id].add(chat_id)

        self.chat_messages[chat_id] = []

        return chat

    def get_chat(self, chat_id: str) -> Optional[Chat]:
        return self.chats.get(chat_id)

    def get_admin_chats(self, admin_id: str) -> List[Chat]:
        chat_ids = self.admin_chats.get(admin_id, set())
        return [self.chats[chat_id] for chat_id in chat_ids]

    def get_customer_chats(self, customer_id: str) -> List[Chat]:
        chat_ids = self.customer_chats.get(customer_id, set())
        return [self.chats[chat_id] for chat_id in chat_ids]

    async def add_message_to_chat(self, chat_id: str, message: Message) -> None:
        if chat_id not in self.chat_messages:
            raise ValueError(f"Chat {chat_id} not found")

        self.chat_messages[chat_id].append(message)

        # Оновлюємо інформацію про чат
        chat = self.chats[chat_id]
        chat.updated_at = datetime.now()
        chat.last_message = message.content

        # Оновлюємо лічильник непрочитаних повідомлень
        if message.from_user == chat.customer_id:
            chat.unread_count += 1

    def get_chat_messages(self, chat_id: str) -> List[Message]:
        return self.chat_messages.get(chat_id, [])

    async def mark_chat_as_read(self, chat_id: str, user_id: str) -> None:
        chat = self.chats.get(chat_id)
        if chat and chat.admin_id == user_id:
            chat.unread_count = 0

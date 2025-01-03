import traceback
from datetime import datetime
from typing import Optional, List, Dict, Any
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from bson import ObjectId


def convert_object_id(obj):
    """Convert MongoDB ObjectId to string in document"""
    if isinstance(obj, dict):
        return {k: convert_object_id(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_object_id(elem) for elem in obj]
    elif isinstance(obj, ObjectId):
        return str(obj)
    return obj


class MongoDBManager:
    def __init__(self, connection_string: str = "mongodb://localhost:27017/"):
        self.client = MongoClient(connection_string)
        self.db: Database = self.client.chat_system
        self.users: Collection = self.db.users
        self.chats: Collection = self.db.chats
        self.messages: Collection = self.db.messages
        self.active_connections: Dict[str, Any] = {}
        self.comments = self.db.comments
        self.message_templates = self.db.message_templates

        self.users.drop_indexes()


        # Create indexes
        self.users.create_index("user_id", unique=True)
        self.users.create_index("api_key", sparse=True, unique=True)
        self.users.create_index("phone", sparse=True, unique=True)
        self.users.create_index("telegram_id", sparse=True, unique=True)

        self.chats.create_index("chat_id", unique=True)
        self.chats.create_index("admin_id")
        self.chats.create_index("customer_id")

        self.messages.create_index("chat_id")
        self.messages.create_index("timestamp")

        self.comments.create_index([("chat_id", 1), ("timestamp", 1)])
        self.message_templates.create_index("created_by")

    async def create_user(self, user_data: dict) -> str:
        """Create a new user in the database"""
        user_doc = {
            "user_id": user_data["id"],
            "type": user_data["type"].value,
            "created_at": datetime.now()
        }

        if user_data.get("api_key"):
            user_doc["api_key"] = user_data["api_key"]

        if user_data.get("phone"):
            user_doc["phone"] = user_data["phone"]

        if user_data.get("telegram_id"):
            user_doc["telegram_id"] = user_data["telegram_id"]

        try:
            result = self.users.insert_one(user_doc)
            return str(result.inserted_id)
        except Exception as e:
            print(f"Error creating user: {str(e)}")
            raise

    async def get_user_by_field(self, field: str, value: Any) -> Optional[dict]:
        """Get user by any field (api_key, phone, telegram_id, etc.)"""
        return self.users.find_one({field: value})

    async def create_chat(self, **kwargs) -> str:
        chat_doc = {
            "chat_id": f"chat_{ObjectId()}",
            "admin_id": kwargs["admin_id"],
            "customer_id": kwargs["customer_id"],
            "status": kwargs.get("status", "new"),
            "priority": kwargs.get("priority", "normal"),
            "source": kwargs.get("source", "web"),
            "created_at": datetime.now(),
            "updated_at": datetime.now(),
            "title": kwargs.get("title") or f"Chat with {kwargs['customer_id']}",
            "unread_count": 0,
            "last_message": None
        }

        result = self.chats.insert_one(chat_doc)
        return chat_doc["chat_id"]

    async def update_chat(self, chat_id: str, update_data: dict) -> None:
        self.chats.update_one(
            {"chat_id": chat_id},
            {
                "$set": {
                    **update_data,
                    "updated_at": datetime.now()
                }
            }
        )

    async def add_comment(self, comment_data: dict) -> str:
        result = self.comments.insert_one(comment_data)
        return str(result.inserted_id)

    async def get_chat_comments(self, chat_id: str) -> List[dict]:
        comments = list(self.comments.find({"chat_id": chat_id}).sort("timestamp", 1))
        return [convert_object_id(comment) for comment in comments]

    async def create_message_template(self, template_data: dict) -> str:
        result = self.message_templates.insert_one(template_data)
        return str(result.inserted_id)

    async def get_message_templates(self, user_id: str) -> List[dict]:
        templates = list(self.message_templates.find({"created_by": user_id}))
        return [convert_object_id(template) for template in templates]

    async def get_filtered_chats(
            self,
            status: Optional[str] = None,
            priority: Optional[str] = None,
            source: Optional[str] = None,
            admin_id: Optional[str] = None,
    ) -> List[dict]:
        try:
            query = {}

            if status:
                query["status"] = status.lower()
            if priority:
                query["priority"] = priority.lower()
            if source:
                query["source"] = source.lower()
            if admin_id:
                query["admin_id"] = admin_id


            # Execute the query and sort by updated_at in descending order
            chats = list(self.chats.find(query).sort("updated_at", -1))

            return [convert_object_id(chat) for chat in chats]

        except Exception as e:
            print(f"Error in get_filtered_chats: {str(e)}")
            traceback.print_exc()
            return []

    async def get_sorted_chats(self, query: dict, sort_field: str, sort_order: int) -> List[dict]:
        """Get chats with custom sorting"""
        try:
            chats = list(self.chats.find(query).sort(sort_field, sort_order))
            return [convert_object_id(chat) for chat in chats]
        except Exception as e:
            print(f"Error in get_sorted_chats: {str(e)}")
            traceback.print_exc()
            return []

    async def get_chat(self, chat_id: str) -> Optional[dict]:
        """Get chat by ID"""
        chat = self.chats.find_one({"chat_id": chat_id})
        if chat:
            return convert_object_id(chat)
        return None

    async def get_user_chats(self, user_id: str, user_type: str) -> List[dict]:
        """Get all chats for a user based on their type"""
        if user_type in ["admin", "mechanic"]:
            query = {}
        else:
            query = {"customer_id": user_id}

        chats = list(self.chats.find(query).sort("updated_at", -1))
        return [convert_object_id(chat) for chat in chats]

    async def get_chat_by_users(self, user1_id: str, user2_id: str) -> Optional[dict]:
        """Get chat by two user IDs"""
        chat = self.chats.find_one({
            "$or": [
                {"admin_id": user1_id, "customer_id": user2_id},
                {"admin_id": user2_id, "customer_id": user1_id}
            ]
        })
        return chat

    def update_chat_admin(self, chat_id: str, admin_id: str):
        return self.chats.update_one(
            {"chat_id": chat_id},
            {"$set": {"admin_id": admin_id}}
        )

    async def add_message(self, chat_id: str, message_data: dict) -> str:
        """Add a new message to a chat"""
        message_doc = {
            "chat_id": chat_id,
            "from_user": message_data["from_user"],
            "to_user": message_data["to_user"],
            "content": message_data["content"],
            "timestamp": message_data["timestamp"],
            "message_type": message_data["message_type"].value,
            "file_path": message_data.get("file_path"),
            "file_name": message_data.get("file_name"),
            "mime_type": message_data.get("mime_type"),
            "source": message_data.get("source", "web")
        }

        result = self.messages.insert_one(message_doc)

        # Update chat's last message and timestamp
        self.chats.update_one(
            {"chat_id": chat_id},
            {
                "$set": {
                    "last_message": message_data["content"],
                    "updated_at": message_data["timestamp"]
                },
                "$inc": {"unread_count": 1}
            }
        )

        return str(result.inserted_id)

    async def get_chat_messages(self, chat_id: str, limit: int = 100, skip: int = 0) -> List[dict]:
        """Get messages for a specific chat with pagination"""
        messages = list(self.messages.find(
            {"chat_id": chat_id},
            sort=[("timestamp", 1)],
            limit=limit,
            skip=skip
        ))
        return [convert_object_id(msg) for msg in messages]

    async def mark_chat_as_read(self, chat_id: str, user_id: str) -> None:
        """Mark all messages in a chat as read"""
        self.chats.update_one(
            {"chat_id": chat_id, "admin_id": user_id},
            {"$set": {"unread_count": 0}}
        )

    async def get_active_staff(self) -> List[str]:
        """Get list of active admin and mechanic IDs"""
        active_staff = []
        for user_id, connection in self.active_connections.items():
            try:
                if user_id.startswith(('admin_', 'mechanic_')):
                    active_staff.append(user_id)
            except Exception as e:
                print(f"Error checking connection for {user_id}: {str(e)}")
                continue
        print(f"Active staff found: {active_staff}")
        return active_staff

    async def update_user_online_status(self, user_id: str, is_online: bool) -> None:
        """Update user's online status"""
        self.users.update_one(
            {"user_id": user_id},
            {"$set": {"is_online": is_online, "last_active": datetime.now()}}
        )

    def register_connection(self, user_id: str, websocket: Any) -> None:
        """Register a new WebSocket connection"""
        self.active_connections[user_id] = websocket

    def remove_connection(self, user_id: str) -> None:
        """Remove a WebSocket connection"""
        self.active_connections.pop(user_id, None)

    def close(self):
        """Close MongoDB connection"""
        self.client.close()

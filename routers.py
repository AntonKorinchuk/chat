from fastapi import (
    WebSocket,
    WebSocketDisconnect,
    HTTPException,
    Request,
    APIRouter,
    FastAPI,
)
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import requests
from datetime import datetime

from config import TELEGRAM_API_URL
from managers import ConnectionManager


router = APIRouter()

templates = Jinja2Templates(directory="templates")

manager = ConnectionManager()


class Message(BaseModel):
    chat_id: int
    text: str
    sender: str
    timestamp: datetime


async def send_telegram_message(chat_id: int, text: str) -> bool:
    try:
        response = requests.post(
            f"{TELEGRAM_API_URL}/sendMessage", json={"chat_id": chat_id, "text": text}
        )
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"Error sending Telegram message: {e}")
        return False


# WebSocket endpoints
@router.websocket("/ws/admin")
async def admin_websocket(websocket: WebSocket):
    await manager.connect_admin(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            user_id = data.get("user_id")
            message = data.get("message")
            message_type = data.get("type", "web")

            if not all([user_id, message]):
                continue

            message_obj = {
                "user_id": user_id,
                "message": message,
                "sender": "admin",
                "timestamp": datetime.now().isoformat(),
            }

            manager.store_message(user_id, message_obj)

            if message_type == "telegram":
                telegram_chat_id = None
                for chat_id, uid in manager.telegram_chat_ids.items():
                    if uid == user_id:
                        telegram_chat_id = chat_id
                        break

                if telegram_chat_id:
                    success = await send_telegram_message(telegram_chat_id, message)
                    if not success:
                        await websocket.send_json(
                            {
                                "error": True,
                                "message": "Failed to send Telegram message",
                            }
                        )
            else:
                await manager.send_to_user(user_id, message_obj)

    except WebSocketDisconnect:
        manager.disconnect_admin(websocket)


@router.websocket("/ws/user/{user_id}")
async def user_websocket(websocket: WebSocket, user_id: str):
    await manager.connect_user(websocket, user_id)
    try:
        while True:
            data = await websocket.receive_json()
            message = data.get("message")

            if not message:
                continue

            message_obj = {
                "user_id": user_id,
                "message": message,
                "sender": "user",
                "timestamp": datetime.now().isoformat(),
            }

            manager.store_message(user_id, message_obj)
            await manager.broadcast_to_admins(message_obj)

    except WebSocketDisconnect:
        manager.disconnect_user(user_id)


@router.post("/")
async def webhook_handler(update: dict):
    try:
        print("Received update:", update)

        if "message" in update:
            chat_id = update["message"]["chat"]["id"]
            sender_name = update["message"].get("from", {}).get("first_name", "Unknown")

            if "text" not in update["message"]:
                print("Message does not contain text. Skipping.")
                return {"status": "skipped", "reason": "no text in message"}

            message_text = update["message"]["text"]

            user_id = f"telegram_{chat_id}"
            manager.register_telegram_user(chat_id, user_id)

            message_obj = {
                "user_id": user_id,
                "message": message_text,
                "sender": f"telegram_user_{sender_name}",
                "timestamp": datetime.now().isoformat(),
                "platform": "telegram",
            }

            manager.store_message(user_id, message_obj)

            await manager.broadcast_to_admins(message_obj)

        elif "my_chat_member" in update:
            chat_id = update["my_chat_member"]["chat"]["id"]
            new_status = update["my_chat_member"]["new_chat_member"]["status"]
            old_status = update["my_chat_member"]["old_chat_member"]["status"]

            print(
                f"Bot membership status changed: {old_status} -> {new_status} in chat {chat_id}"
            )

        else:
            print("Received update of unsupported type:", update)

        return {"status": "success"}
    except Exception as e:
        print(f"Error in webhook handler: {e}")
        raise HTTPException(status_code=400, detail="Invalid webhook payload")


@router.get("/messages/{user_id}")
async def get_messages(user_id: str):
    messages = manager.message_history.get(user_id, [])
    return {"user_id": user_id, "messages": messages}


@router.get("/active-users")
async def get_active_users():
    web_users = list(manager.user_connections.keys())
    telegram_users = [
        f"telegram_{chat_id}" for chat_id in manager.telegram_chat_ids.keys()
    ]
    return {"users": list(set(web_users + telegram_users))}


@router.get("/", response_class=HTMLResponse)
async def get_user_page(request: Request):
    return templates.TemplateResponse("user.html", {"request": request})


@router.get("/admin", response_class=HTMLResponse)
async def get_admin_page(request: Request):
    return templates.TemplateResponse("admin.html", {"request": request})

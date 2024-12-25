import os
from datetime import datetime

import requests
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Header, HTTPException, Request, UploadFile, Form, File
from typing import Optional, Union

from fastapi.templating import Jinja2Templates
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config import UPLOAD_DIR, TELEGRAM_API_URL, TELEGRAM_TOKEN
from managers import ConnectionManager, Message, User, UserType, MessageType, FileManager

router = APIRouter()
manager = ConnectionManager()
templates = Jinja2Templates(directory="templates")


class StaffRegistration(BaseModel):
    user_type: str
    name: str


class CustomerRegistration(BaseModel):
    name: str
    phone: str


async def get_token(
        websocket: WebSocket,
        api_key: Optional[str] = None,
        phone: Optional[str] = None,
) -> Union[User, None]:
    if not api_key and not phone:
        return None

    if api_key:
        user = manager.get_user_by_api_key(api_key)
        if user:
            return user

    if phone:
        user = manager.get_user_by_phone(phone)
        if user:
            return user

    return None


async def download_telegram_file(file_id: str) -> tuple[bytes, str, str]:
    url = f"{TELEGRAM_API_URL}/getFile"
    params = {"file_id": file_id}
    response = requests.get(url, params=params)
    response.raise_for_status()

    file_info = response.json()["result"]
    file_path = file_info["file_path"]

    download_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    response = requests.get(download_url, stream=True)
    response.raise_for_status()

    ext = os.path.splitext(file_path)[1]
    if not ext:
        mime_type = response.headers.get('content-type', 'application/octet-stream')
        mime_to_ext = {
            'video/mp4': '.mp4',
            'audio/mpeg': '.mp3',
            'audio/ogg': '.ogg',
            'image/jpeg': '.jpg',
            'image/png': '.png'
        }
        ext = mime_to_ext.get(mime_type, '')

    return response.content, file_path, response.headers.get('content-type', 'application/octet-stream')


@router.websocket("/ws/{user_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    user_id: str,
    api_key: Optional[str] = None,
    phone: Optional[str] = None
):
    user = await get_token(websocket, api_key, phone)
    if not user:
        await websocket.close(code=4001, reason="Authentication failed")
        return

    if user.id != user_id:
        await websocket.close(code=4002, reason="Invalid user ID")
        return

    await manager.connect(websocket, user_id)

    try:
        while True:
            data = await websocket.receive_json()

            to_user_id = data.get("to_user")
            content = data.get("content")
            message_type = data.get("message_type", "text")
            file_data = data.get("file_data")  # For media messages

            if not all([to_user_id, content]):
                await websocket.send_json({
                    "error": True,
                    "message": "Missing required fields"
                })
                continue

            message = Message(
                from_user=user_id,
                to_user=to_user_id,
                content=content,
                timestamp=datetime.now(),
                message_type=MessageType[message_type.upper()],
                file_path=file_data.get("file_path") if file_data else None,
                file_name=file_data.get("file_name") if file_data else None,
                mime_type=file_data.get("mime_type") if file_data else None
            )

            await manager.send_message(message)

    except WebSocketDisconnect:
        await manager.disconnect(user_id)
    except Exception as e:
        print(f"Error in websocket: {str(e)}")
        await websocket.close(code=4000, reason="Internal server error")


@router.post("/")
async def telegram_webhook(update: dict):
    try:
        chat_id = update["message"]["chat"]["id"]
        username = update["message"]["from"].get("username", "Unknown")

        user = manager.get_user_by_telegram_id(chat_id)
        if not user:
            user = User(
                id=f"telegram_{chat_id}",
                type=UserType.CUSTOMER,
                telegram_id=chat_id,
                name=username
            )
            await manager.register_user(user)

        message_type = MessageType.TEXT
        content = update["message"].get("text", "")
        file_path = None
        relative_path = None
        file_name = None
        mime_type = None

        if "photo" in update["message"]:
            message_type = MessageType.IMAGE
            photo = update["message"]["photo"][-1]
            file_id = photo["file_id"]
            file_content, _, mime_type = await download_telegram_file(file_id)
            file_path, relative_path = await FileManager.save_telegram_file(
                file_content,
                file_id,
                message_type
            )
            content = update["message"].get("caption", "Image")
            file_name = photo.get("file_name", f"{file_id}.jpg")

        elif "audio" in update["message"]:
            message_type = MessageType.AUDIO
            audio = update["message"]["audio"]
            file_id = audio["file_id"]
            file_content, _, mime_type = await download_telegram_file(file_id)
            file_path, relative_path = await FileManager.save_telegram_file(
                file_content,
                file_id,
                message_type
            )
            content = update["message"].get("caption", "Audio")
            file_name = audio.get("file_name", f"{file_id}.mp3")

        elif "voice" in update["message"]:
            message_type = MessageType.VOICE
            voice = update["message"]["voice"]
            file_id = voice["file_id"]
            file_content, _, mime_type = await download_telegram_file(file_id)
            file_path, relative_path = await FileManager.save_telegram_file(
                file_content,
                file_id,
                message_type
            )
            content = "Voice message"
            file_name = voice.get("file_name", f"{file_id}.ogg")

        elif "video" in update["message"]:
            message_type = MessageType.VIDEO
            video = update["message"]["video"]
            file_id = video["file_id"]
            file_content, _, mime_type = await download_telegram_file(file_id)
            file_path, relative_path = await FileManager.save_telegram_file(
                file_content,
                file_id,
                message_type
            )
            content = update["message"].get("caption", "Video")
            file_name = video.get("file_name", f"{file_id}.mp4")

        elif "document" in update["message"]:
            message_type = MessageType.FILE
            document = update["message"]["document"]
            file_id = document["file_id"]
            file_content, _, mime_type = await download_telegram_file(file_id)
            file_path, relative_path = await FileManager.save_telegram_file(
                file_content,
                file_id,
                message_type
            )
            content = update["message"].get("caption", "Document")
            file_name = document.get("file_name", f"{file_id}_file")

        message = Message(
            from_user=user.id,
            to_user="admin",
            content=content,
            timestamp=datetime.now(),
            source="telegram",
            message_type=message_type,
            file_path=relative_path,
            file_name=file_name,
            mime_type=mime_type
        )

        success = await manager.send_message_to_active_admin(message)
        if not success:
            return {"status": "no_active_admins"}

        return {"status": "success"}

    except Exception as e:
        print(f"Error processing telegram webhook: {str(e)}")
        return {"status": "error", "detail": str(e)}


@router.post("/register/staff")
async def register_staff(
        data: StaffRegistration,
        api_key: str = Header(...)
):
    try:
        if data.user_type not in ["admin", "mechanic"]:
            raise HTTPException(status_code=400, detail="Invalid user type")

        user = User(
            id=f"{data.user_type}_{data.name}",
            type=UserType[data.user_type.upper()],
            api_key=api_key
        )
        await manager.register_user(user)
        return {
            "status": "success",
            "user_id": user.id,
            "message": f"Successfully registered {data.user_type} {data.name}"
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/register/customer")
async def register_customer(
        data: CustomerRegistration
):
    try:
        user = User(
            id=f"customer_{data.phone}",
            type=UserType.CUSTOMER,
            phone=data.phone
        )
        await manager.register_user(user)
        return {
            "status": "success",
            "user_id": user.id,
            "message": f"Successfully registered customer {data.name}"
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/messages/manager-history")
async def get_manager_message_history(
    target_type: str,  # "mechanic", "customer"
    target_identifier: str,  # api_key for mechanic, phone/telegram_id for customer
    api_key: str = Header(...)
):
    """Get complete message history between a manager and a target user (mechanic or customer)"""
    manager_user = manager.get_user_by_api_key(api_key)
    if not manager_user or manager_user.type != UserType.ADMIN:
        raise HTTPException(status_code=403, detail="Invalid manager credentials")

    target_user = None
    if target_type == "mechanic":
        target_user = manager.get_user_by_api_key(target_identifier)
        if not target_user or target_user.type != UserType.MECHANIC:
            raise HTTPException(status_code=404, detail="Mechanic not found")
    elif target_type == "customer":

        target_user = manager.get_user_by_phone(target_identifier)
        if not target_user:

            try:
                telegram_id = int(target_identifier)
                target_user = manager.get_user_by_telegram_id(telegram_id)
            except ValueError:
                pass
        if not target_user or target_user.type != UserType.CUSTOMER:
            raise HTTPException(status_code=404, detail="Customer not found")
    else:
        raise HTTPException(status_code=400, detail="Invalid target type")

    sent_messages = [
        msg.to_json() for msg in manager.message_history.get(manager_user.id, [])
        if msg.to_user == target_user.id
    ]

    received_messages = [
        msg.to_json() for msg in manager.message_history.get(target_user.id, [])
        if msg.to_user == manager_user.id
    ]

    all_messages = sorted(
        sent_messages + received_messages,
        key=lambda x: datetime.fromisoformat(x['timestamp'])
    )

    return {
        "status": "success",
        "messages": all_messages
    }


@router.get("/messages/mechanic-history")
async def get_mechanic_message_history(
    target_type: str,  # "manager", "customer"
    target_identifier: str,
    api_key: str = Header(...)
):
    """Get complete message history between a mechanic and a target user (manager or customer)"""
    mechanic_user = manager.get_user_by_api_key(api_key)
    if not mechanic_user or mechanic_user.type != UserType.MECHANIC:
        raise HTTPException(status_code=403, detail="Invalid mechanic credentials")

    target_user = None
    if target_type == "manager":
        target_user = manager.get_user_by_api_key(target_identifier)
        if not target_user or target_user.type != UserType.ADMIN:
            raise HTTPException(status_code=404, detail="Manager not found")
    elif target_type == "customer":
        target_user = manager.get_user_by_phone(target_identifier)
        if not target_user:
            try:
                telegram_id = int(target_identifier)
                target_user = manager.get_user_by_telegram_id(telegram_id)
            except ValueError:
                pass
        if not target_user or target_user.type != UserType.CUSTOMER:
            raise HTTPException(status_code=404, detail="Customer not found")
    else:
        raise HTTPException(status_code=400, detail="Invalid target type")

    sent_messages = [
        msg.to_json() for msg in manager.message_history.get(mechanic_user.id, [])
        if msg.to_user == target_user.id
    ]

    received_messages = [
        msg.to_json() for msg in manager.message_history.get(target_user.id, [])
        if msg.to_user == mechanic_user.id
    ]


    all_messages = sorted(
        sent_messages + received_messages,
        key=lambda x: datetime.fromisoformat(x['timestamp'])
    )

    return {
        "status": "success",
        "messages": all_messages
    }


@router.get("/messages/customer-history")
async def get_customer_message_history(
    target_type: str,  # "manager", "mechanic"
    target_identifier: str,  # api_key
    identifier: str,
    identifier_type: str = "phone"  # Can be "phone" or "telegram"
):
    """Get complete message history between a customer and a target user (manager or mechanic)"""
    customer_user = None
    if identifier_type == "phone":
        customer_user = manager.get_user_by_phone(identifier)
    elif identifier_type == "telegram":
        try:
            telegram_id = int(identifier)
            customer_user = manager.get_user_by_telegram_id(telegram_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Telegram ID format")
    else:
        raise HTTPException(status_code=400, detail="Invalid identifier type")

    if not customer_user or customer_user.type != UserType.CUSTOMER:
        raise HTTPException(status_code=404, detail="Customer not found")

    target_user = None
    if target_type == "manager":
        target_user = manager.get_user_by_api_key(target_identifier)
        if not target_user or target_user.type != UserType.ADMIN:
            raise HTTPException(status_code=404, detail="Manager not found")
    elif target_type == "mechanic":
        target_user = manager.get_user_by_api_key(target_identifier)
        if not target_user or target_user.type != UserType.MECHANIC:
            raise HTTPException(status_code=404, detail="Mechanic not found")
    else:
        raise HTTPException(status_code=400, detail="Invalid target type")

    sent_messages = [
        msg.to_json() for msg in manager.message_history.get(customer_user.id, [])
        if msg.to_user == target_user.id
    ]

    received_messages = [
        msg.to_json() for msg in manager.message_history.get(target_user.id, [])
        if msg.to_user == customer_user.id
    ]

    all_messages = sorted(
        sent_messages + received_messages,
        key=lambda x: datetime.fromisoformat(x['timestamp'])
    )

    return {
        "status": "success",
        "messages": all_messages
    }


@router.get("/test")
async def test_page(request: Request):
    return templates.TemplateResponse("test.html", {"request": request})


@router.post("/upload/{message_type}")
async def upload_file(
    message_type: str,
    to_user: str = Form(...),
    file: UploadFile = File(...),
    api_key: Optional[str] = Header(None),
    phone: Optional[str] = Header(None)
):
    """Upload media file and send it as a message"""
    user = await get_token(None, api_key, phone)
    if not user:
        raise HTTPException(status_code=403, detail="Authentication failed")

    try:
        msg_type = MessageType[message_type.upper()]
    except KeyError:
        raise HTTPException(status_code=400, detail="Invalid message type")


    FileManager.validate_file(file, msg_type)
    filepath, filename = await FileManager.save_file(file, msg_type)

    # Create message
    message = Message(
        from_user=user.id,
        to_user=to_user,
        content=file.filename,
        timestamp=datetime.now(),
        message_type=msg_type,
        file_path=filepath,
        file_name=filename,
        mime_type=file.content_type
    )


    await manager.send_message(message)

    return {"status": "success", "message": message.to_json()}


@router.get("/files/{message_type}/{filename}")
async def get_file(message_type: str, filename: str):
    """Retrieve uploaded file"""
    try:
        msg_type = MessageType[message_type.upper()]
    except KeyError:
        raise HTTPException(status_code=400, detail="Invalid message type")


    filepath = os.path.join(UPLOAD_DIR, msg_type.value, filename)

    if not os.path.exists(filepath):

        print(f"File not found: {filepath}")

        if '_' in filename:
            simple_filename = filename.split('_', 1)[1]
            alternative_path = os.path.join(UPLOAD_DIR, msg_type.value, simple_filename)
            if os.path.exists(alternative_path):
                return FileResponse(alternative_path)

        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(filepath)

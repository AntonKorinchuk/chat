import os
from datetime import datetime

import requests
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Header, HTTPException, Request, UploadFile, Form, File
from typing import Optional, Union

from fastapi.templating import Jinja2Templates
from fastapi.responses import FileResponse
from pydantic import BaseModel
from jose import JWTError, jwt

from auth import create_access_token, SECRET_KEY, ALGORITHM
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


@router.post("/login")
async def login(api_key: Optional[str] = None, phone: Optional[str] = None):
    user = None
    if api_key:
        user = manager.get_user_by_api_key(api_key)
    elif phone:
        user = manager.get_user_by_phone(phone)

    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Create access token
    token_data = {
        "sub": user.id,
        "type": user.type.value,
        "api_key": user.api_key,
        "phone": user.phone
    }

    access_token = create_access_token(token_data)
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_id": user.id,
        "user_type": user.type.value
    }


@router.websocket("/ws/{user_id}")
async def websocket_endpoint(
        websocket: WebSocket,
        user_id: str,
        api_key: Optional[str] = None,
        phone: Optional[str] = None
):
    try:
        # Перевірка токена з query parameters
        token = websocket.query_params.get("token")
        if token:
            try:
                payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
                if payload["sub"] != user_id:
                    await websocket.close(code=4002, reason="Invalid user ID")
                    return
            except JWTError:
                await websocket.close(code=4001, reason="Authentication failed")
                return
        else:
            # Альтернативна аутентифікація через API-ключ або телефон
            user = await get_token(websocket, api_key, phone)
            if not user or user.id != user_id:
                await websocket.close(code=4001, reason="Authentication failed")
                return

        await manager.connect(websocket, user_id)

        try:
            while True:
                try:
                    data = await websocket.receive_json()

                    if not data:
                        continue

                    to_user_id = data.get("to_user")
                    content = data.get("content")
                    message_type = data.get("message_type", "text")
                    file_data = data.get("file_data")
                    chat_id = data.get("chat_id")

                    if not content:
                        await websocket.send_json({
                            "error": True,
                            "message": "Missing required fields"
                        })
                        continue

                    # Якщо клієнт відправляє повідомлення без вказання to_user_id
                    if not to_user_id and user_id.startswith('customer_'):
                        active_staff = manager.get_active_staff()
                        if not active_staff:
                            await websocket.send_json({
                                "error": True,
                                "message": "No active staff available"
                            })
                            continue
                        to_user_id = active_staff[0]

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

                    await manager.send_message(message, chat_id)

                except WebSocketDisconnect:
                    raise
                except Exception as e:
                    print(f"Error processing message: {str(e)}")
                    await websocket.send_json({
                        "error": True,
                        "message": "Error processing message"
                    })

        except WebSocketDisconnect:
            raise

    except WebSocketDisconnect:
        await manager.disconnect(user_id)
        print(f"WebSocket disconnected for user {user_id}")
    except Exception as e:
        print(f"Error in websocket: {str(e)}")
        try:
            await websocket.close(code=4000, reason="Internal server error")
        except:
            pass
    finally:
        await manager.disconnect(user_id)


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

        success = await manager.send_message_to_active_staff(message)
        if not success:
            return {"status": "no_active_admins"}

        return {"status": "success"}

    except Exception as e:
        print(f"Error processing telegram webhook: {str(e)}")
        return {"status": "error", "detail": str(e)}


@router.post("/register/staff")
async def register_staff(
        data: StaffRegistration
):
    try:
        if data.user_type not in ["admin", "mechanic"]:
            raise HTTPException(status_code=400, detail="Invalid user type")

        api_key = f"{data.user_type}_{data.name}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

        user = User(
            id=f"{data.user_type}_{data.name}",
            type=UserType[data.user_type.upper()],
            api_key=api_key,
            name=data.name
        )
        await manager.register_user(user)
        return {
            "status": "success",
            "user_id": user.id,
            "api_key": api_key,  # Повертаємо згенерований API key
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


@router.get("/chats")
async def get_chats(api_key: str = Header(...)):
    """Get all chats for staff member (admin or mechanic)"""
    user = manager.get_user_by_api_key(api_key)
    if not user or user.type not in [UserType.ADMIN, UserType.MECHANIC]:
        raise HTTPException(status_code=403, detail="Only staff members can view chats")

    chats = manager.chat_manager.get_admin_chats(user.id)
    return {
        "status": "success",
        "chats": [chat.dict() for chat in chats]
    }


@router.get("/chats/{chat_id}")
async def get_chat_details(
        chat_id: str,
        api_key: Optional[str] = Header(None),
        phone: Optional[str] = Header(None)
):
    """Get chat details and messages"""
    # Check authentication using either API key or phone
    user = None
    if api_key:
        user = manager.get_user_by_api_key(api_key)
    if not user and phone:
        user = manager.get_user_by_phone(phone)
    if not user:
        raise HTTPException(status_code=403, detail="Authentication failed")

    chat = manager.chat_manager.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    # Modified access check - allow access if user is either the staff member (admin/mechanic) or the customer
    if user.id == chat.admin_id or user.id == chat.customer_id:
        messages = manager.chat_manager.get_chat_messages(chat_id)

        # Mark messages as read if staff member is viewing
        if user.type in [UserType.ADMIN, UserType.MECHANIC]:
            await manager.chat_manager.mark_chat_as_read(chat_id, user.id)

        return {
            "status": "success",
            "chat": chat.dict(),
            "messages": [msg.to_json() for msg in messages]
        }
    else:
        raise HTTPException(status_code=403, detail="Access denied")


@router.post("/chats/{chat_id}/messages")
async def send_chat_message(
        chat_id: str,
        message_content: str = Form(...),
        file: Optional[UploadFile] = File(None),
        api_key: str = Header(...)
):
    """Send message to specific chat"""
    user = manager.get_user_by_api_key(api_key)
    if not user:
        raise HTTPException(status_code=403, detail="Authentication failed")

    chat = manager.chat_manager.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    # Визначаємо отримувача
    to_user = chat.customer_id if user.id == chat.admin_id else chat.admin_id

    message_type = MessageType.TEXT
    file_path = None
    file_name = None
    mime_type = None

    if file:
        message_type = MessageType[file.content_type.split('/')[0].upper()]
        FileManager.validate_file(file, message_type)
        file_path, file_name = await FileManager.save_file(file, message_type)
        mime_type = file.content_type

    message = Message(
        from_user=user.id,
        to_user=to_user,
        content=message_content,
        timestamp=datetime.now(),
        message_type=message_type,
        file_path=file_path,
        file_name=file_name,
        mime_type=mime_type
    )

    await manager.send_message(message, chat_id)

    return {"status": "success", "message": message.to_json()}

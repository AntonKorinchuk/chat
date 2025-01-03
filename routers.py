import traceback
from datetime import datetime
import os
from typing import Optional, Union, Dict, Any

import requests
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Header, HTTPException, Request, UploadFile, Form, File, \
    Query
from fastapi.templating import Jinja2Templates
from fastapi.responses import FileResponse
from pydantic import BaseModel, ValidationError
from jose import JWTError, jwt

from auth import create_access_token, SECRET_KEY, ALGORITHM
from config import UPLOAD_DIR, TELEGRAM_API_URL, TELEGRAM_TOKEN
from managers import ConnectionManager, ChatManager, FileManager, Message, User, UserType, MessageType, ChatStatus
from mongodb_manager import MongoDBManager

router = APIRouter()
db_manager = MongoDBManager()
connection_manager = ConnectionManager(db_manager)
chat_manager = ChatManager(db_manager)
file_manager = FileManager()
template = Jinja2Templates(directory="templates")


class StaffRegistration(BaseModel):
    user_type: str
    name: str


class CustomerRegistration(BaseModel):
    name: str
    phone: str


async def get_user_from_credentials(
        api_key: Optional[str] = None,
        phone: Optional[str] = None,
) -> Union[User, None]:
    if api_key:
        user_data = await db_manager.get_user_by_field("api_key", api_key)
        if user_data:
            return User(
                id=user_data["user_id"],
                type=UserType[user_data["type"].upper()],
                api_key=user_data.get("api_key"),
                phone=user_data.get("phone"),
                telegram_id=user_data.get("telegram_id")
            )

    if phone:
        user_data = await db_manager.get_user_by_field("phone", phone)
        if user_data:
            return User(
                id=user_data["user_id"],
                type=UserType[user_data["type"].upper()],
                api_key=user_data.get("api_key"),
                phone=user_data.get("phone"),
                telegram_id=user_data.get("telegram_id")
            )

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
    user = await get_user_from_credentials(api_key, phone)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

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
            user = await get_user_from_credentials(api_key, phone)
            if not user or user.id != user_id:
                await websocket.close(code=4001, reason="Authentication failed")
                return

        await connection_manager.connect(websocket, user_id)
        print(f"User {user_id} connected to WebSocket")

        try:
            while True:
                data = await websocket.receive_json()
                print(f"Received message from {user_id}: {data}")

                if not data:
                    continue

                content = data.get("content")
                message_type = data.get("message_type", "text")
                file_data = data.get("file_data")
                chat_id = data.get("chat_id")

                if not content:
                    await websocket.send_json({
                        "error": True,
                        "message": "Missing content field"
                    })
                    continue

                to_user_id = None
                if user_id.startswith('customer_'):

                    active_staff = await connection_manager.get_active_staff()
                    print(f"Active staff: {active_staff}")
                    if not active_staff:
                        await websocket.send_json({
                            "error": True,
                            "message": "No active staff available"
                        })
                        continue
                    to_user_id = active_staff[0]
                else:

                    to_user_id = data.get("to_user")
                    if chat_id:
                        chat = await chat_manager.get_chat(chat_id)
                        if chat:
                            to_user_id = chat["customer_id"] if user_id == chat["admin_id"] else chat["admin_id"]

                if not to_user_id:
                    await websocket.send_json({
                        "error": True,
                        "message": "Could not determine message recipient"
                    })
                    continue

                try:
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


                    if not chat_id:
                        chat = await chat_manager.get_or_create_chat(
                            from_user=user_id,
                            to_user=to_user_id
                        )
                        chat_id = chat["chat_id"]

                    # Send message
                    print(f"Sending message in chat {chat_id} from {user_id} to {to_user_id}")
                    await connection_manager.send_message(message, chat_id)

                    # Confirm to sender
                    await websocket.send_json({
                        "status": "success",
                        "message": "Message sent successfully",
                        "chat_id": chat_id
                    })

                except ValidationError as e:
                    print(f"Validation error: {str(e)}")
                    await websocket.send_json({
                        "error": True,
                        "message": f"Validation error: {str(e)}"
                    })
                except Exception as e:
                    print(f"Error sending message: {str(e)}")
                    await websocket.send_json({
                        "error": True,
                        "message": f"Error sending message: {str(e)}"
                    })

        except WebSocketDisconnect:
            raise

    except WebSocketDisconnect:
        await connection_manager.disconnect(user_id)
        print(f"WebSocket disconnected for user {user_id}")
    except Exception as e:
        print(f"Error in websocket: {str(e)}")
        traceback.print_exc()
        try:
            await websocket.close(code=4000, reason="Internal server error")
        except:
            pass
    finally:
        await connection_manager.disconnect(user_id)


@router.post("/register/staff")
async def register_staff(data: StaffRegistration):
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

        await connection_manager.register_user(user)

        return {
            "status": "success",
            "user_id": user.id,
            "api_key": api_key,
            "message": f"Successfully registered {data.user_type} {data.name}"
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/register/customer")
async def register_customer(data: CustomerRegistration):
    try:
        user = User(
            id=f"customer_{data.phone}",
            type=UserType.CUSTOMER,
            phone=data.phone,
            name=data.name
        )
        await connection_manager.register_user(user)
        return {
            "status": "success",
            "user_id": user.id,
            "message": f"Successfully registered customer {data.name}"
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/upload/{message_type}")
async def upload_file(
        message_type: str,
        to_user: str = Form(...),
        file: UploadFile = File(...),
        api_key: Optional[str] = Header(None),
        phone: Optional[str] = Header(None)
):
    user = await get_user_from_credentials(api_key, phone)
    if not user:
        raise HTTPException(status_code=403, detail="Authentication failed")

    try:
        msg_type = MessageType[message_type.upper()]
    except KeyError:
        raise HTTPException(status_code=400, detail="Invalid message type")

    file_manager.validate_file(file, msg_type)
    filepath, filename = await file_manager.save_file(file, msg_type)

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

    await connection_manager.send_message(message)

    return {"status": "success", "message": message.to_json()}


@router.get("/files/{message_type}/{filename}")
async def get_file(message_type: str, filename: str):
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


@router.get("/chats/sorted")
async def get_sorted_chats(
    sort_by: str = Query(..., description="Sort field: date, priority, or source"),
    order: str = Query("desc", description="Sort order: asc or desc"),
    api_key: Optional[str] = Header(None),
    phone: Optional[str] = Header(None)
):
    user = await get_user_from_credentials(api_key=api_key, phone=phone)
    if not user:
        raise HTTPException(status_code=403, detail="Authentication failed")

    valid_sort_fields = {
        "date": "updated_at",
        "priority": "priority",
        "source": "source"
    }

    if sort_by not in valid_sort_fields:
        raise HTTPException(status_code=400, detail="Invalid sort field")
    if order not in ["asc", "desc"]:
        raise HTTPException(status_code=400, detail="Invalid sort order")

    try:
        # Determine query based on user type
        if user.type in [UserType.ADMIN, UserType.MECHANIC]:
            query = {}  # Show all chats for admin
        else:
            query = {"customer_id": user.id}  # Show only user's chats for customer

        # Sort chats
        sorted_chats = await db_manager.get_sorted_chats(
            query,
            valid_sort_fields[sort_by],
            -1 if order == "desc" else 1
        )

        return {
            "status": "success",
            "chats": sorted_chats if sorted_chats else []
        }

    except Exception as e:
        print(f"Error in get_sorted_chats: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/chats")
async def get_chats(
    api_key: Optional[str] = Header(None),
    phone: Optional[str] = Header(None)
):
    user = await get_user_from_credentials(api_key=api_key, phone=phone)
    if not user:
        raise HTTPException(status_code=403, detail="Authentication failed")

    if user.type in [UserType.ADMIN, UserType.MECHANIC]:
        chats = await db_manager.get_user_chats(user.id, user.type.value)
    else:

        chats = await db_manager.get_user_chats(user.id, "customer")

    return {
        "status": "success",
        "chats": chats
    }


@router.get("/chats/filtered")
async def get_filtered_chats(
        status: Optional[str] = Query(None),
        priority: Optional[str] = Query(None),
        source: Optional[str] = Query(None),
        admin_id: Optional[str] = Query(None),
        api_key: Optional[str] = Header(None)
):
    user = await get_user_from_credentials(api_key=api_key)
    if not user or user.type not in [UserType.ADMIN, UserType.MECHANIC]:
        raise HTTPException(status_code=403, detail="Not authorized")

    try:
        print(f"Filtering chats with parameters: status={status}, priority={priority}, source={source}, admin_id={admin_id}")

        chats = await db_manager.get_filtered_chats(
            status=status,
            priority=priority,
            source=source,
            admin_id=admin_id
        )

        return {"status": "success", "chats": chats}

    except Exception as e:
        print(f"Error in get_filtered_chats endpoint: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/chats/{chat_id}")
async def get_chat_details(
        chat_id: str,
        api_key: Optional[str] = Header(None),
        phone: Optional[str] = Header(None)
):
    user = await get_user_from_credentials(api_key, phone)
    if not user:
        raise HTTPException(status_code=403, detail="Authentication failed")

    chat = await chat_manager.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    if user.type in [UserType.ADMIN, UserType.MECHANIC] or user.id == chat["customer_id"]:
        messages = await db_manager.get_chat_messages(chat_id)

        if user.type in [UserType.ADMIN, UserType.MECHANIC]:
            await chat_manager.mark_chat_as_read(chat_id, user.id)

        return {
            "status": "success",
            "chat": chat,
            "messages": messages
        }
    else:
        raise HTTPException(status_code=403, detail="Access denied")


@router.post("/")
async def telegram_webhook(update: dict):
    try:
        chat_id = update["message"]["chat"]["id"]
        user_id = update["message"]["from"]["id"]
        username = update["message"]["from"].get("username", "Unknown")
        first_name = update["message"]["from"].get("first_name", "")
        last_name = update["message"]["from"].get("last_name", "")

        display_name = username if username != "Unknown" else f"{first_name} {last_name}".strip()
        if not display_name:
            display_name = f"User_{user_id}"

        telegram_user_id = f"telegram_{user_id}"

        user_data = await connection_manager.get_user_by_telegram_id(user_id)
        if not user_data:
            user = User(
                id=telegram_user_id,
                type=UserType.CUSTOMER,
                telegram_id=user_id,
                name=display_name
            )
            await connection_manager.register_user(user)

        existing_chats = await db_manager.get_user_chats(telegram_user_id, "customer")
        existing_chat = existing_chats[0] if existing_chats else None

        active_staff = await connection_manager.get_active_staff()
        if not active_staff:
            await connection_manager.send_telegram_message(
                chat_id,
                "Sorry, no active staff members are available at the moment."
            )
            return {"status": "no_active_admins"}

        if existing_chat:
            to_user = existing_chat["admin_id"]
            chat_id_to_use = existing_chat["chat_id"]
        else:
            to_user = active_staff[0]
            new_chat = await chat_manager.create_chat(
                admin_id=to_user,
                customer_id=telegram_user_id,
                title=f"Telegram Chat with {display_name}",
                source = "telegram"
            )
            chat_id_to_use = new_chat

        message_type = MessageType.TEXT
        content = update["message"].get("text", "")
        file_path = None
        file_name = None
        mime_type = None

        if "photo" in update["message"]:
            message_type = MessageType.IMAGE
            photo = update["message"]["photo"][-1]
            file_id = photo["file_id"]
            file_content, _, mime_type = await download_telegram_file(file_id)
            file_path, file_name = await file_manager.save_telegram_file(
                file_content,
                file_id,
                message_type
            )
            content = update["message"].get("caption", "Image")
        elif "audio" in update["message"]:
            message_type = MessageType.AUDIO
            audio = update["message"]["audio"]
            file_id = audio["file_id"]
            file_content, _, mime_type = await download_telegram_file(file_id)
            file_path, file_name = await file_manager.save_telegram_file(
                file_content,
                file_id,
                message_type
            )
            content = update["message"].get("caption", "Audio")
        elif "voice" in update["message"]:
            message_type = MessageType.VOICE
            voice = update["message"]["voice"]
            file_id = voice["file_id"]
            file_content, _, mime_type = await download_telegram_file(file_id)
            file_path, file_name = await file_manager.save_telegram_file(
                file_content,
                file_id,
                message_type
            )
            content = "Voice message"
        elif "video" in update["message"]:
            message_type = MessageType.VIDEO
            video = update["message"]["video"]
            file_id = video["file_id"]
            file_content, _, mime_type = await download_telegram_file(file_id)
            file_path, file_name = await file_manager.save_telegram_file(
                file_content,
                file_id,
                message_type
            )
            content = update["message"].get("caption", "Video")
        elif "document" in update["message"]:
            message_type = MessageType.FILE
            document = update["message"]["document"]
            file_id = document["file_id"]
            file_content, _, mime_type = await download_telegram_file(file_id)
            file_path, file_name = await file_manager.save_telegram_file(
                file_content,
                file_id,
                message_type
            )
            content = update["message"].get("caption", "Document")

        message = Message(
            from_user=telegram_user_id,
            to_user=to_user,
            content=content,
            timestamp=datetime.now(),
            source="telegram",
            message_type=message_type,
            file_path=file_path,
            file_name=file_name,
            mime_type=mime_type
        )

        # Store the chat_id mapping for future responses
        if not hasattr(message, '_telegram_chat_id'):
            setattr(message, '_telegram_chat_id', chat_id)

        await connection_manager.send_message(message, chat_id_to_use)
        return {"status": "success"}

    except Exception as e:
        print(f"Error processing telegram webhook: {str(e)}")
        traceback.print_exc()
        try:
            await connection_manager.send_telegram_message(
                chat_id,
                "Sorry, there was an error processing your message."
            )
        except:
            pass
        return {"status": "error", "detail": str(e)}


@router.get("/test")
async def test_page(request: Request):
    """Test page endpoint"""
    return template.TemplateResponse("test.html", {"request": request})


@router.put("/chats/{chat_id}/status")
async def update_chat_status(
        chat_id: str,
        status: str,
        api_key: Optional[str] = Header(None)
):
    user = await get_user_from_credentials(api_key=api_key)
    if not user or user.type not in [UserType.ADMIN, UserType.MECHANIC]:
        raise HTTPException(status_code=403, detail="Not authorized")

    try:
        new_status = ChatStatus[status.upper()]
        await chat_manager.update_chat_status(chat_id, new_status)
        return {"status": "success", "message": f"Chat status updated to {status}"}
    except KeyError:
        raise HTTPException(status_code=400, detail="Invalid status")


@router.post("/chats/{chat_id}/comments")
async def add_chat_comment(
        chat_id: str,
        content: str,
        api_key: Optional[str] = Header(None)
):
    user = await get_user_from_credentials(api_key=api_key)
    if not user or user.type not in [UserType.ADMIN, UserType.MECHANIC]:
        raise HTTPException(status_code=403, detail="Not authorized")

    comment_id = await chat_manager.add_comment(chat_id, user.id, content)
    return {"status": "success", "comment_id": comment_id}


@router.get("/chats/{chat_id}/comments")
async def get_chat_comments(
        chat_id: str,
        api_key: Optional[str] = Header(None)
):
    user = await get_user_from_credentials(api_key=api_key)
    if not user or user.type not in [UserType.ADMIN, UserType.MECHANIC]:
        raise HTTPException(status_code=403, detail="Not authorized")

    comments = await chat_manager.get_chat_comments(chat_id)
    return {"status": "success", "comments": comments}


@router.post("/message-templates")
async def create_message_template(
        name: str = Form(...),
        content: str = Form(...),
        api_key: Optional[str] = Header(None)
):
    user = await get_user_from_credentials(api_key=api_key)
    if not user or user.type not in [UserType.ADMIN, UserType.MECHANIC]:
        raise HTTPException(status_code=403, detail="Not authorized")

    template_id = await chat_manager.create_message_template(name, content, user.id)
    return {"status": "success", "template_id": template_id}


@router.get("/message-templates")
async def get_message_templates(
        api_key: Optional[str] = Header(None)
):
    user = await get_user_from_credentials(api_key=api_key)
    if not user or user.type not in [UserType.ADMIN, UserType.MECHANIC]:
        raise HTTPException(status_code=403, detail="Not authorized")

    templates = await chat_manager.get_message_templates(user.id)
    return {"status": "success", "templates": templates}



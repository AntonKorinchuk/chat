import json
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Header, HTTPException, Request
from typing import Optional, Union

from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from managers import ConnectionManager, Message, User, UserType

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
                timestamp=datetime.now()
            )

            await manager.send_message(message)

    except WebSocketDisconnect:
        await manager.disconnect(user_id)
    except Exception as e:
        print(f"Error in websocket: {str(e)}")
        await websocket.close(code=4000, reason="Internal server error")


@router.post("/")
async def telegram_webhook(update: dict):
    print(f"Received telegram update: {json.dumps(update, indent=2)}")

    if "message" not in update:
        print("No message in update")
        return {"status": "ignored"}

    try:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text", "")
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
            print(f"Created new user: {user}")

        message = Message(
            from_user=user.id,
            to_user="admin",
            content=text,
            timestamp=datetime.now(),
            source="telegram"
        )

        success = await manager.send_message_to_active_admin(message)
        print(f"Message sent to admin: {success}")

        if not success:
            print("No active admins available")
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
    # Verify the requesting manager
    manager_user = manager.get_user_by_api_key(api_key)
    if not manager_user or manager_user.type != UserType.ADMIN:
        raise HTTPException(status_code=403, detail="Invalid manager credentials")

    target_user = None
    if target_type == "mechanic":
        target_user = manager.get_user_by_api_key(target_identifier)
        if not target_user or target_user.type != UserType.MECHANIC:
            raise HTTPException(status_code=404, detail="Mechanic not found")
    elif target_type == "customer":
        # Try phone first
        target_user = manager.get_user_by_phone(target_identifier)
        if not target_user:
            # Try telegram id
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
    # Verify the requesting mechanic
    mechanic_user = manager.get_user_by_api_key(api_key)
    if not mechanic_user or mechanic_user.type != UserType.MECHANIC:
        raise HTTPException(status_code=403, detail="Invalid mechanic credentials")

    # Get the target user based on type
    target_user = None
    if target_type == "manager":
        target_user = manager.get_user_by_api_key(target_identifier)
        if not target_user or target_user.type != UserType.ADMIN:
            raise HTTPException(status_code=404, detail="Manager not found")
    elif target_type == "customer":
        # Try phone first
        target_user = manager.get_user_by_phone(target_identifier)
        if not target_user:
            # Try telegram id
            try:
                telegram_id = int(target_identifier)
                target_user = manager.get_user_by_telegram_id(telegram_id)
            except ValueError:
                pass
        if not target_user or target_user.type != UserType.CUSTOMER:
            raise HTTPException(status_code=404, detail="Customer not found")
    else:
        raise HTTPException(status_code=400, detail="Invalid target type")

    # Get messages sent by mechanic to target
    sent_messages = [
        msg.to_json() for msg in manager.message_history.get(mechanic_user.id, [])
        if msg.to_user == target_user.id
    ]

    # Get messages received by mechanic from target
    received_messages = [
        msg.to_json() for msg in manager.message_history.get(target_user.id, [])
        if msg.to_user == mechanic_user.id
    ]

    # Combine and sort all messages by timestamp
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
    # Get the customer based on identifier type
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

    # Get the target user based on type
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

    # Get messages sent by customer to target
    sent_messages = [
        msg.to_json() for msg in manager.message_history.get(customer_user.id, [])
        if msg.to_user == target_user.id
    ]

    # Get messages received by customer from target
    received_messages = [
        msg.to_json() for msg in manager.message_history.get(target_user.id, [])
        if msg.to_user == customer_user.id
    ]

    # Combine and sort all messages by timestamp
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

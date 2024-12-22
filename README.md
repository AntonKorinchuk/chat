# Chat

This project integrates a Telegram bot with WebSockets and a REST API to enable real-time communication between administrators and users. It allows handling messages via Telegram, storing chat history, and relaying messages over WebSockets.

## Installation

1. Clone the repository:
```bash
git clone https://github.com/AntonKorinchuk/chat.git
cd chat
```

2. Create and activate a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Windows, use `venv\Scripts\activate`
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Set up environment variables:
Rename .env.template to .env file and populate it with the required data:
```
TELEGRAM_TOKEN=your_telegram_token
WEBHOOK_URL=https://your-domain/telegram/webhook
```

5. Set the Webhook
Run the script to set up the Telegram webhook:
```bash
python set_webhook.py
```

6.Start the Server
```bash
python main.py
```

## The server will be accessible at: http://localhost:8000



### REST and WebSocket API
1. Admin WebSocket
- Endpoint: GET /admin/
- Admins can receive messages from users and reply to them.
2. User WebSocket
- Endpoint: GET /
- Users can send messages to administrators.

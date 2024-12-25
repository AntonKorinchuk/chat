import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

UPLOAD_DIR = "E:\\TEST_CHAT\\uploads"
MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50MB
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif"}
ALLOWED_AUDIO_TYPES = {"audio/mpeg", "audio/wav", "audio/ogg"}
ALLOWED_VIDEO_TYPES = {"video/mp4", "video/mpeg", "video/webm"}
ALLOWED_VOICE_TYPES = {"audio/ogg", "audio/webm"}
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

# Telegram credentials
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_DL_BOT_TOKEN")
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

# Remote VPS info
VPS_IP = os.getenv("IP_address")
VPS_LOGIN = os.getenv("LOGIN", "root")
VPS_PASSWORD = os.getenv("PASSWORD")

# Download & Temp Directories
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Settings & Limits
MAX_FILE_SIZE_MB = 1950  # Telethon allows up to 2GB
MAX_ALBUM_SIZE = 10      # Telegram album limit per group
CONCURRENT_DOWNLOADS = 3 # Async semaphore limit

# YouTube quality options
QUALITY_CHOICES = {
    "1080": {"height": 1080, "label": "1080p Full HD", "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]"},
    "720": {"height": 720, "label": "720p HD", "format": "bestvideo[height<=720]+bestaudio/best[height<=720]"},
    "480": {"height": 480, "label": "480p SD", "format": "bestvideo[height<=480]+bestaudio/best[height<=480]"},
    "360": {"height": 360, "label": "360p Fast", "format": "bestvideo[height<=360]+bestaudio/best[height<=360]"},
    "mp3": {"label": "Audio MP3 (320kbps)", "format": "bestaudio/best"},
}

import time
import math
import logging
from typing import Optional, Callable
from telethon import errors

logger = logging.getLogger(__name__)

def human_bytes(size: float) -> str:
    """Format bytes to human readable format (KB, MB, GB)."""
    if not size or math.isnan(size):
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    unit_index = 0
    while size >= 1024.0 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1
    return f"{size:.1f} {units[unit_index]}"

def create_bar(percent: float, length: int = 10) -> str:
    """Create a textual progress bar."""
    completed = int(length * (percent / 100))
    bar = "█" * completed + "░" * (length - completed)
    return f"[{bar}]"

class TelegramProgress:
    """Throttled progress callback handler for Telethon with cancel button preservation."""
    
    def __init__(self, message, action: str = "Uploading", buttons=None):
        self.message = message
        self.action = action
        self.buttons = buttons
        self.start_time = time.time()
        self.last_update_time = 0
        self.min_interval = 4.5  # Safe 4.5s throttle to prevent Telegram FloodWait on EditMessageRequest
        self._last_text = ""

    async def callback(self, current: int, total: int):
        now = time.time()
        if now - self.last_update_time < self.min_interval and current < total:
            return

        total = total or 1
        percent = min(100.0, (current / total) * 100)
        speed = current / (now - self.start_time + 0.001)
        eta = (total - current) / (speed + 0.001)

        bar = create_bar(percent)
        cur_str = human_bytes(current)
        tot_str = human_bytes(total)
        spd_str = f"{human_bytes(speed)}/s"
        eta_str = f"{int(eta)}s" if eta < 3600 else f"{int(eta//60)}m"

        text = (
            f"🚀 **{self.action}...**\n\n"
            f"{bar} `{percent:.1f}%`\n"
            f"📦 **Size:** `{cur_str} / {tot_str}`\n"
            f"⚡ **Speed:** `{spd_str}` | ⏳ **ETA:** `{eta_str}`"
        )
        if text == self._last_text:
            return

        self.last_update_time = now
        self._last_text = text

        try:
            if self.buttons is not None:
                await self.message.edit(text, buttons=self.buttons)
            else:
                await self.message.edit(text)
        except errors.FloodWaitError as e:
            logger.warning(f"Telegram FloodWait in progress update: back off for {e.seconds}s")
            self.min_interval = max(self.min_interval, e.seconds + 2)
            self.last_update_time = now + e.seconds
        except errors.MessageNotModifiedError:
            pass
        except Exception:
            pass

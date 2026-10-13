import time
import math
from typing import Optional, Callable

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
        self.min_interval = 2.5 # Update every 2.5 seconds

    async def callback(self, current: int, total: int):
        now = time.time()
        if now - self.last_update_time < self.min_interval and current < total:
            return

        self.last_update_time = now
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
        try:
            if self.buttons is not None:
                await self.message.edit(text, buttons=self.buttons)
            else:
                await self.message.edit(text)
        except Exception:
            pass

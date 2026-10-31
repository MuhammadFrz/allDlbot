import os
import re
import uuid
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional

from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeAudio, DocumentAttributeAnimated

import config
from utils.cleanup import (
    create_task_dir,
    cleanup_dir,
    get_file_size_mb,
    purge_all_downloads,
    periodic_cleaner_task,
)
from utils.progress import TelegramProgress, human_bytes, create_bar
from utils.detector import extract_url, detect_platform
from utils.converter import video_to_mp3, video_to_gif, convert_video_quality, extract_thumbnail, convert_file_format, get_video_params
from downloaders import (
    YouTubeDownloader,
    InstagramDownloader,
    TikTokDownloader,
    TwitterDownloader,
    SpotifyDownloader,
    PinterestDownloader,
    RedditDownloader,
    SoundCloudDownloader,
    FacebookDownloader,
    GooglePlayDownloader,
    DirectDownloader,
    SxyprnDownloader,
)

# Setup Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("AllDLBot")

# In-memory storage for active download tasks and pending menus
pending_yt_tasks: Dict[str, Dict[str, Any]] = {}
pending_gplay_tasks: Dict[str, Dict[str, Any]] = {}
pending_file_ops: Dict[str, Dict[str, Any]] = {}
waiting_rename_tasks: Dict[int, str] = {}
waiting_custom_ext_tasks: Dict[int, str] = {}
active_jobs: Dict[str, Dict[str, Any]] = {}
active_semaphore = asyncio.Semaphore(config.CONCURRENT_DOWNLOADS)

def get_audio_duration_seconds(file_path: Path) -> int:
    """Extract audio duration in seconds from MP3 file."""
    try:
        from mutagen.mp3 import MP3
        audio = MP3(str(file_path))
        return int(audio.info.length)
    except Exception:
        return 0

def prepare_video_attributes(
    video_path: Path,
    thumb_path: Optional[Path] = None,
    duration: Optional[int] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    is_gif: bool = False,
):
    """
    Prepare DocumentAttributeVideo/Animated and thumbnail path for Telethon send_file.
    Ensures video uploads display with correct duration, thumbnail preview, and dimensions.
    """
    if not duration or not width or not height:
        p_dur, p_w, p_h = get_video_params(video_path)
        duration = duration or p_dur
        width = width or p_w
        height = height or p_h

    duration = int(duration or 0)
    width = int(width or 1280)
    height = int(height or 720)

    if not thumb_path or not Path(thumb_path).exists():
        thumb_path = extract_thumbnail(video_path)

    attrs = []
    if is_gif:
        attrs.append(DocumentAttributeAnimated())
        attrs.append(
            DocumentAttributeVideo(
                duration=duration,
                w=width,
                h=height,
                supports_streaming=True,
                nosound=True,
            )
        )
    else:
        attrs.append(
            DocumentAttributeVideo(
                duration=duration,
                w=width,
                h=height,
                supports_streaming=True,
            )
        )

    final_thumb = thumb_path if (thumb_path and Path(thumb_path).exists()) else None
    return attrs, final_thumb


def make_cancel_button(task_id: str):
    """Return inline cancel button for any active task."""
    return [[Button.inline("❌ Cancel Download", data=f"cancel_job:{task_id}")]]

# Initialize Telethon Client
client = TelegramClient(
    "alldl_bot_session",
    config.TELEGRAM_API_ID,
    config.TELEGRAM_API_HASH,
)

# Persistent Bottom Menu Keyboards (No slash commands needed!)
MAIN_KEYBOARD = [
    [
        Button.text("📥 Download Media", resize=True),
        Button.text("🔄 Media Converter", resize=True),
    ],
    [
        Button.text("ℹ️ Supported Platforms", resize=True),
        Button.text("📖 Help & Instructions", resize=True),
    ],
]

PLATFORM_INLINE_BUTTONS = [
    [
        Button.inline("🎵 TikTok", data=b"plat:tiktok"),
        Button.inline("📸 Instagram", data=b"plat:instagram"),
    ],
    [
        Button.inline("🐦 Twitter / X", data=b"plat:twitter"),
        Button.inline("🎧 Spotify", data=b"plat:spotify"),
    ],
    [
        Button.inline("📺 YouTube", data=b"plat:youtube"),
        Button.inline("📌 Pinterest", data=b"plat:pinterest"),
    ],
    [
        Button.inline("🤖 Reddit", data=b"plat:reddit"),
        Button.inline("☁️ SoundCloud", data=b"plat:soundcloud"),
    ],
    [
        Button.inline("📘 Facebook", data=b"plat:facebook"),
        Button.inline("🏪 Google Play", data=b"plat:googleplay"),
    ],
    [
        Button.inline("🔄 Media Converter", data=b"plat:converter"),
        Button.inline("🔗 Direct Links", data=b"plat:direct"),
    ],
    [
        Button.inline("🔞 SXYPRN", data=b"plat:sxyprn"),
    ],
]


@client.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):
    welcome_text = (
        "👋 **Welcome to All-In-One Media Downloader Bot!**\n\n"
        "You don't need to type any slash commands — use the **buttons below** or simply **paste any link** to start downloading:\n\n"
        "✨ **What I can do:**\n"
        "• 🎵 **TikTok**: HD Video (No Watermark), Photo Slideshows, Audio MP3\n"
        "• 📸 **Instagram**: Reels, Posts, and **Full Carousels (All media items)**\n"
        "• 🐦 **Twitter / X**: HD Videos, Animated GIFs, Multi-image tweets\n"
        "• 🎧 **Spotify**: 320kbps MP3 with album artwork & tags\n"
        "• 📺 **YouTube**: Videos, Shorts, Quality Selector (1080p, 720p, 480p, 360p) & MP3\n"
        "• 🏪 **Google Play**: Direct APK/XAPK downloads with **RAM / Architecture selector**\n"
        "• 📌 **Pinterest**: Original High-Res Images & HD Video Pins\n"
        "• 🤖 **Reddit**: Videos with audio merged, Multi-Image Galleries, GIFs\n"
        "• ☁️ **SoundCloud**: 320kbps MP3 with 500x500 Cover Art & Tags\n"
        "• 📘 **Facebook**: High Quality Reels & Watch Videos\n"
        "• 🔗 **Direct Links**: Download files, videos, music, PDFs, APKs, & archives directly\n"
        "• 🔄 **Media Converter**: Convert any video to MP3, GIF, or change resolution!\n"
        "• 🔞 **SXYPRN**: Direct HD video downloads from sxyprn.com\n\n"
        "👇 **Tap a platform below to learn more, or paste your link directly:**"
    )
    await event.respond(welcome_text, buttons=MAIN_KEYBOARD, parse_mode="md")
    await event.respond("⚡ **Select a platform to explore features:**", buttons=PLATFORM_INLINE_BUTTONS)


@client.on(events.NewMessage(pattern=r"(?i)^(📥 Download Media|download|دانلود)"))
async def download_menu_button(event):
    text = (
        "📥 **Download Media Mode**\n\n"
        "Simply send or paste any public link directly into this chat.\n\n"
        "🌐 **Supported Links:**\n"
        "• `play.google.com/store/apps/...` (APKs with RAM / CPU Architecture selector)\n"
        "• `tiktok.com/...` (Watermark-free video, audio, or slideshows)\n"
        "• `instagram.com/...` (Reels, posts, carousels with all media)\n"
        "• `x.com/...` or `twitter.com/...` (Videos, GIFs, images)\n"
        "• `open.spotify.com/...` (Tracks, albums, playlists)\n"
        "• `youtube.com/...` or `youtu.be/...` (Videos, Shorts, MP3)\n"
        "• `pinterest.com/...` or `pin.it/...` (Original photos & videos)\n"
        "• `reddit.com/...` or `redd.it/...` (Videos with sound & galleries)\n"
        "• `soundcloud.com/...` (320kbps MP3 with cover artwork)\n"
        "• `facebook.com/...` or `fb.watch/...` (Reels & Watch videos)\n"
        "• `sxyprn.com/post/...` (HD Video downloads)\n"
        "• **Direct Links**: Any `http://` or `https://` direct file download link\n\n"
        "👇 **Paste your link here now:**"
    )
    await event.respond(text, buttons=MAIN_KEYBOARD, parse_mode="md")


@client.on(events.NewMessage(pattern=r"(?i)^(🔄 Media Converter|converter|تبدیل)"))
async def converter_menu_button(event):
    text = (
        "🔄 **Media Converter & File Tools**\n\n"
        "Send or forward **any file, video, audio, image, or document** directly into this chat.\n\n"
        "Interactive buttons will appear with options to:\n"
        "• ✏️ **Rename File**: Change the name of any file instantly\n"
        "• 🔄 **Change Format**: Convert audio (MP3, M4A, WAV, FLAC, OGG, OPUS), video (MP4, MKV, WEBM, AVI), images (PNG, JPG, WEBP, PDF), or custom extensions\n"
        "• 🎵 **Extract Audio**: Extract 320kbps MP3 from video\n"
        "• 🎞️ **Convert to GIF**: High-quality animated GIF\n\n"
        "👇 **Send or forward any file now:**"
    )
    await event.respond(text, buttons=MAIN_KEYBOARD, parse_mode="md")


@client.on(events.NewMessage(pattern=r"(?i)^(ℹ️ Supported Platforms|platforms|پلتفرم)"))
async def platforms_menu_button(event):
    text = (
        "ℹ️ **Supported Platforms & Features:**\n\n"
        "Tap any platform button below to see its capabilities and sample links:"
    )
    await event.respond(text, buttons=PLATFORM_INLINE_BUTTONS, parse_mode="md")


@client.on(events.NewMessage(pattern=r"(?i)^(📖 Help & Instructions|help|راهنما)"))
async def help_menu_button(event):
    help_text = (
        "📖 **How to Use This Bot (No Slash Commands Needed!):**\n\n"
        "1️⃣ **Downloading Media & Apps:**\n"
        "   Just copy a link from Google Play, YouTube, TikTok, Instagram, Twitter, Spotify, SoundCloud, Facebook, Reddit, or Direct Download links and send it here.\n\n"
        "2️⃣ **Rename Files & Change Formats:**\n"
        "   Forward or send **any file, video, audio, image, or document** to the bot.\n"
        "   Interactive buttons will appear to `✏️ Rename File` or `🔄 Change Format`!\n\n"
        "3️⃣ **Google Play Architecture & RAM Selector:**\n"
        "   Select ARM64, ARMv7, x86_64, or Universal for ready-to-install APKs.\n\n"
        "4️⃣ **YouTube Quality Selector:**\n"
        "   Choose 1080p, 720p, 480p, 360p, or Audio MP3.\n\n"
        "5️⃣ **Cancel Button on Tasks:**\n"
        "   Every active task has an inline `[❌ Cancel]` button so you can stop it anytime."
    )
    await event.respond(help_text, buttons=MAIN_KEYBOARD, parse_mode="md")


@client.on(events.CallbackQuery(pattern=r"^plat:(.+)$"))
async def platform_info_callback(event):
    plat = event.pattern_match.group(1).decode("utf-8")
    
    details = {
        "googleplay": (
            "🏪 **Google Play Downloader**\n\n"
            "• **Direct APK & XAPK Download**: Fast, safe downloads from Google Play.\n"
            "• **RAM & Architecture Selector**:\n"
            "  - 📱 `ARM64 (arm64-v8a)`: Modern 64-bit phones (4GB, 6GB, 8GB+ RAM)\n"
            "  - 📱 `ARMv7 (armeabi-v7a)`: 32-bit devices (<4GB RAM / Older phones)\n"
            "  - 💻 `x86_64 / x86`: PC Emulators & Chromebooks\n"
            "  - 🌐 `Universal`: Universal APK compatible with all devices\n"
            "• **Full App Details & Icon**: App name, developer, version, star rating, and icon.\n\n"
            "💡 *Paste any Google Play link to try it!*"
        ),
        "tiktok": (
            "🎵 **TikTok Downloader**\n\n"
            "• **Watermark-Free HD**: Direct clean video.\n"
            "• **Photo Slideshows**: Automatically downloads all photos in the slideshow.\n"
            "• **Background Music**: Extracts the MP3 audio track.\n\n"
            "💡 *Paste any TikTok link to try it!*"
        ),
        "instagram": (
            "📸 **Instagram Downloader**\n\n"
            "• **Carousels**: Downloads **EVERY photo and video** in multi-media posts.\n"
            "• **Reels & Videos**: High-definition video download.\n"
            "• **Single Photos**: Full resolution.\n\n"
            "💡 *Paste any Instagram link to try it!*"
        ),
        "twitter": (
            "🐦 **Twitter / X Downloader**\n\n"
            "• **Videos**: Best available video resolution.\n"
            "• **Animated GIFs**: Converted to native Telegram animated GIFs.\n"
            "• **Multi-Image Tweets**: All photos downloaded into an album.\n\n"
            "💡 *Paste any Twitter/X link to try it!*"
        ),
        "spotify": (
            "🎧 **Spotify Downloader**\n\n"
            "• **High Quality**: 320kbps MP3 audio.\n"
            "• **Full Metadata**: Cover artwork, artist, title, and album tags embedded.\n"
            "• Supports tracks, albums, and playlists.\n\n"
            "💡 *Paste any Spotify link to try it!*"
        ),
        "youtube": (
            "📺 **YouTube Downloader**\n\n"
            "• **Interactive Quality Buttons**: 1080p, 720p, 480p, 360p.\n"
            "• **Audio Extraction**: Direct 320kbps MP3 conversion.\n"
            "• **GIF Converter**: Turn short clips into GIFs.\n"
            "• Works with regular videos and YouTube Shorts.\n\n"
            "💡 *Paste any YouTube link to try it!*"
        ),
        "pinterest": (
            "📌 **Pinterest Downloader**\n\n"
            "• **Original Resolution Images**: Highest possible quality.\n"
            "• **HD Video Pins**: Watermark-free MP4 videos.\n"
            "• **GIFs & Idea Pins**: Full animated support.\n"
            "• Supports `pinterest.com` & `pin.it` shortlinks.\n\n"
            "💡 *Paste any Pinterest link to try it!*"
        ),
        "reddit": (
            "🤖 **Reddit Downloader**\n\n"
            "• **Videos With Audio**: Automatically merges video + sound with FFmpeg.\n"
            "• **Galleries**: Downloads all photos in multi-image posts.\n"
            "• **Native GIFs**: High-speed animated format.\n"
            "• Supports `reddit.com`, `redd.it`, & `v.redd.it`.\n\n"
            "💡 *Paste any Reddit link to try it!*"
        ),
        "soundcloud": (
            "☁️ **SoundCloud Downloader**\n\n"
            "• **High Quality Audio**: 320kbps MP3 extraction.\n"
            "• **Cover Artwork**: 500x500 high-resolution album artwork embedded.\n"
            "• **ID3 Tags**: Artist, title, and duration metadata embedded.\n\n"
            "💡 *Paste any SoundCloud link to try it!*"
        ),
        "facebook": (
            "📘 **Facebook Downloader**\n\n"
            "• **Reels & Watch**: High definition video downloads.\n"
            "• **Public Videos**: Direct streaming MP4 format.\n"
            "• Supports `facebook.com` & `fb.watch` links.\n\n"
            "💡 *Paste any Facebook link to try it!*"
        ),
        "converter": (
            "🔄 **Media Converter**\n\n"
            "• **Video to MP3**: High fidelity 320kbps audio extraction.\n"
            "• **Video to GIF**: High quality 2-pass GIF animation.\n"
            "• **Quality Re-encoding**: 720p, 480p, 360p.\n\n"
            "💡 *Send any video file to this chat to start!*"
        ),
        "direct": (
            "🔗 **Direct File Downloader**\n\n"
            "• **Any Direct Link**: Paste any direct HTTP or HTTPS download URL.\n"
            "• **File Types**: Videos, Audio, PDFs, ZIP/RAR/7z Archives, APKs, Images, Documents, and ISOs.\n"
            "• **Smart Streaming**: Automatic media player integration, thumbnail generation, and audio player tagging.\n"
            "• **Speed**: Fast multi-threaded streaming up to 2GB per file.\n\n"
            "💡 *Paste any direct file link to start downloading!*"
        ),
        "sxyprn": (
            "🔞 **SXYPRN Downloader**\n\n"
            "• **HD Video**: Downloads the highest quality MP4 directly.\n"
            "• **Auto-extraction**: Parses embedded player config to get the real CDN link.\n"
            "• **Fallback**: yt-dlp generic extractor if direct scrape fails.\n\n"
            "💡 *Paste any `sxyprn.com/post/...` link to try it!*"
        ),
    }

    text = details.get(plat, "Select a platform to view details.")
    back_buttons = [
        [Button.inline("⬅️ Back to Platforms", data=b"back:platforms")],
    ]
    await event.edit(text, buttons=back_buttons, parse_mode="md")


@client.on(events.CallbackQuery(pattern=r"^back:platforms$"))
async def back_platforms_callback(event):
    await event.edit(
        "⚡ **Select a platform to explore features:**",
        buttons=PLATFORM_INLINE_BUTTONS,
        parse_mode="md",
    )


# -------------------------------------------------------------
# Global Task Cancellation Handler
# -------------------------------------------------------------
@client.on(events.CallbackQuery(pattern=r"^cancel_job:(.+)$"))
async def cancel_job_callback(event):
    task_id = event.pattern_match.group(1).decode("utf-8")
    job = active_jobs.get(task_id)

    if job:
        async_task = job.get("task")
        if async_task and not async_task.done():
            async_task.cancel()

        task_dir = job.get("task_dir")
        if task_dir:
            cleanup_dir(task_dir)

        active_jobs.pop(task_id, None)
        await event.edit("❌ **Download cancelled.** Temporary files purged from server.")
    else:
        await event.edit("❌ **Task already finished or cancelled.**")


@client.on(events.CallbackQuery(pattern=r"^cancel:(.+)$"))
async def cancel_menu_callback(event):
    video_id = event.pattern_match.group(1).decode("utf-8")
    pending_yt_tasks.pop(video_id, None)
    await event.edit("❌ **Operation cancelled.**")


@client.on(events.CallbackQuery(pattern=r"^cancel_menu$"))
async def cancel_menu_general(event):
    waiting_rename_tasks.pop(event.chat_id, None)
    waiting_custom_ext_tasks.pop(event.chat_id, None)
    await event.edit("❌ **Operation cancelled.**")


@client.on(events.NewMessage)
async def message_handler(event):
    # Ignore commands or menu button text triggers that are already handled
    text_content = event.raw_text.strip()
    if text_content.startswith("/") or text_content in [
        "📥 Download Media", "🔄 Media Converter", "ℹ️ Supported Platforms", "📖 Help & Instructions"
    ]:
        return

    # Check if user was waiting to rename a file or enter a custom extension
    if event.chat_id in waiting_rename_tasks:
        if not text_content.startswith("/") and not extract_url(text_content):
            file_id = waiting_rename_tasks.pop(event.chat_id)
            file_info = pending_file_ops.get(file_id)
            if not file_info:
                await event.reply("⚠️ **Session expired or file not found.** Please forward or send the file again.")
                return
            task_id = uuid.uuid4().hex[:8]
            t = asyncio.create_task(run_rename_file_job(event, file_info, text_content, task_id))
            active_jobs[task_id] = {"task": t, "chat_id": event.chat_id}
            return
        else:
            # User sent command or URL, abandon rename state and proceed
            waiting_rename_tasks.pop(event.chat_id, None)

    if event.chat_id in waiting_custom_ext_tasks:
        if not text_content.startswith("/") and not extract_url(text_content):
            file_id = waiting_custom_ext_tasks.pop(event.chat_id)
            file_info = pending_file_ops.get(file_id)
            if not file_info:
                await event.reply("⚠️ **Session expired or file not found.** Please forward or send the file again.")
                return
            target_ext = text_content.strip().lower()
            if not target_ext.startswith("."):
                target_ext = f".{target_ext}"
            task_id = uuid.uuid4().hex[:8]
            t = asyncio.create_task(run_convert_file_job(event, file_info, target_ext, task_id))
            active_jobs[task_id] = {"task": t, "chat_id": event.chat_id}
            return
        else:
            # User sent command or URL, abandon custom ext state and proceed
            waiting_custom_ext_tasks.pop(event.chat_id, None)

    # Check for any forwarded or sent file attachment (video, audio, photo, document, voice)
    if event.message.file or event.message.photo:
        file_obj = event.message.file
        original_name = getattr(file_obj, 'name', None) if file_obj else None
        ext = (getattr(file_obj, 'ext', None) or "").lower() if file_obj else ""
        size = getattr(file_obj, 'size', 0) if file_obj else 0
        mime_type = (getattr(file_obj, 'mime_type', '') or '').lower() if file_obj else ""

        if not original_name:
            if event.message.photo:
                original_name = f"photo_{event.message.id}.jpg"
                ext = ".jpg"
                mime_type = "image/jpeg"
            elif event.message.video:
                original_name = f"video_{event.message.id}{ext or '.mp4'}"
            elif event.message.audio or event.message.voice:
                original_name = f"audio_{event.message.id}{ext or '.mp3'}"
            else:
                original_name = f"file_{event.message.id}{ext or ''}"

        if not ext and "." in original_name:
            ext = Path(original_name).suffix.lower()

        file_id = f"{event.chat_id}_{event.message.id}"
        pending_file_ops[file_id] = {
            "chat_id": event.chat_id,
            "message_id": event.message.id,
            "original_name": original_name,
            "size": size,
            "mime_type": mime_type,
            "ext": ext,
        }

        is_video = event.message.video or mime_type.startswith("video") or ext in [".mp4", ".mkv", ".webm", ".avi", ".mov"]

        buttons = [
            [
                Button.inline("✏️ Rename File", data=f"fop:rename:{file_id}"),
                Button.inline("🔄 Change Format", data=f"fop:fmt_menu:{file_id}"),
            ],
        ]

        if is_video:
            buttons.append([
                Button.inline("🎵 Extract MP3", data=f"conv:mp3:{file_id}"),
                Button.inline("🎞️ Convert to GIF", data=f"conv:gif:{file_id}"),
            ])

        buttons.append([Button.inline("❌ Cancel", data=b"cancel_menu")])

        size_display = human_bytes(size) if size else "Unknown"
        format_display = ext.upper().lstrip(".") if ext else "File"
        prompt_text = (
            f"📁 **File Received:** `{original_name}`\n"
            f"📊 **Size:** `{size_display}` | **Type:** `{format_display}`\n\n"
            f"Select what you would like to do with this file:"
        )
        await event.reply(prompt_text, buttons=buttons)
        return

    # Extract URL from message
    url = extract_url(event.raw_text)
    if not url:
        # Check if user sent a direct Google Play package ID (e.g. com.whatsapp)
        raw_pkg = GooglePlayDownloader.extract_package_id(text_content)
        if raw_pkg:
            platform = "googleplay"
            normalized_url = f"https://play.google.com/store/apps/details?id={raw_pkg}"
        else:
            return
    else:
        platform, normalized_url = detect_platform(url)
        if not platform:
            return

    logger.info(f"Detected platform: {platform} for URL: {normalized_url}")

    if platform == "googleplay":
        task_id = uuid.uuid4().hex[:8]
        t = asyncio.create_task(handle_googleplay_request(event, normalized_url, task_id))
        active_jobs[task_id] = {"task": t, "chat_id": event.chat_id}
    elif platform == "youtube":
        task_id = uuid.uuid4().hex[:8]
        t = asyncio.create_task(handle_youtube_request(event, normalized_url, task_id))
        active_jobs[task_id] = {"task": t, "chat_id": event.chat_id}
    elif platform == "instagram":
        task_id = uuid.uuid4().hex[:8]
        t = asyncio.create_task(run_instagram_job(event, normalized_url, task_id))
        active_jobs[task_id] = {"task": t, "chat_id": event.chat_id}
    elif platform == "tiktok":
        task_id = uuid.uuid4().hex[:8]
        t = asyncio.create_task(run_tiktok_job(event, normalized_url, task_id))
        active_jobs[task_id] = {"task": t, "chat_id": event.chat_id}
    elif platform == "twitter":
        task_id = uuid.uuid4().hex[:8]
        t = asyncio.create_task(run_twitter_job(event, normalized_url, task_id))
        active_jobs[task_id] = {"task": t, "chat_id": event.chat_id}
    elif platform == "spotify":
        task_id = uuid.uuid4().hex[:8]
        t = asyncio.create_task(run_spotify_job(event, normalized_url, task_id))
        active_jobs[task_id] = {"task": t, "chat_id": event.chat_id}
    elif platform == "pinterest":
        task_id = uuid.uuid4().hex[:8]
        t = asyncio.create_task(run_pinterest_job(event, normalized_url, task_id))
        active_jobs[task_id] = {"task": t, "chat_id": event.chat_id}
    elif platform == "reddit":
        task_id = uuid.uuid4().hex[:8]
        t = asyncio.create_task(run_reddit_job(event, normalized_url, task_id))
        active_jobs[task_id] = {"task": t, "chat_id": event.chat_id}
    elif platform == "soundcloud":
        task_id = uuid.uuid4().hex[:8]
        t = asyncio.create_task(run_soundcloud_job(event, normalized_url, task_id))
        active_jobs[task_id] = {"task": t, "chat_id": event.chat_id}
    elif platform == "facebook":
        task_id = uuid.uuid4().hex[:8]
        t = asyncio.create_task(run_facebook_job(event, normalized_url, task_id))
        active_jobs[task_id] = {"task": t, "chat_id": event.chat_id}
    elif platform == "direct":
        task_id = uuid.uuid4().hex[:8]
        t = asyncio.create_task(run_direct_job(event, normalized_url, task_id))
        active_jobs[task_id] = {"task": t, "chat_id": event.chat_id}
    elif platform == "sxyprn":
        task_id = uuid.uuid4().hex[:8]
        t = asyncio.create_task(run_sxyprn_job(event, normalized_url, task_id))
        active_jobs[task_id] = {"task": t, "chat_id": event.chat_id}


# -------------------------------------------------------------
# SXYPRN Job
# -------------------------------------------------------------
async def run_sxyprn_job(event, url: str, task_id: str):
    task_dir = create_task_dir(task_id)
    if task_id in active_jobs:
        active_jobs[task_id]["task_dir"] = task_dir

    status_msg = await event.reply(
        "⏳ **Fetching SXYPRN video...**",
        buttons=make_cancel_button(task_id)
    )

    try:
        async with active_semaphore:
            res = await SxyprnDownloader.download(url, task_dir)
            if not res.get("success") or not res.get("path"):
                await status_msg.edit(f"❌ **{res.get('error', 'Could not download this SXYPRN video.')}**")
                return

            vid_path = res["path"]
            title = res.get("title", "SXYPRN Video")
            caption = f"🔞 **SXYPRN**\n\n{title}".strip()

            attrs, thumb = prepare_video_attributes(vid_path)
            progress = TelegramProgress(status_msg, action="Uploading SXYPRN video", buttons=make_cancel_button(task_id))
            await client.send_file(
                event.chat_id,
                vid_path,
                caption=caption,
                thumb=thumb,
                attributes=attrs,
                supports_streaming=True,
                progress_callback=progress.callback,
            )

            await status_msg.delete()

    except asyncio.CancelledError:
        logger.info(f"SXYPRN job {task_id} cancelled by user.")
    except Exception as e:
        logger.error(f"SXYPRN error: {e}")
        await status_msg.edit(f"❌ **Error downloading SXYPRN video:** {str(e)}")
    finally:
        cleanup_dir(task_dir)
        active_jobs.pop(task_id, None)


# -------------------------------------------------------------
# Google Play Handler & Architecture Selector
# -------------------------------------------------------------
async def handle_googleplay_request(event, url: str, task_id: str):
    status_msg = await event.reply("🔍 **Fetching Google Play app information...**", buttons=make_cancel_button(task_id))

    try:
        package_id = GooglePlayDownloader.extract_package_id(url)
        if not package_id:
            await status_msg.edit("❌ **Invalid Google Play link.** Could not extract a package ID (`id=...`).")
            return

        info = await asyncio.to_thread(GooglePlayDownloader.get_app_info, package_id)
        if not info:
            await status_msg.edit("❌ **Could not retrieve app details.** Please check the link or package ID.")
            return

        pending_gplay_tasks[task_id] = info

        rating_str = info.get("rating") or "N/A"
        version_str = info.get("version") or "Latest"
        genre_str = info.get("genre") or "Application"
        dev_str = info.get("developer") or "Google Play Developer"

        caption = (
            f"🏪 **{info['title']}**\n\n"
            f"👤 **Developer:** `{dev_str}`\n"
            f"📦 **Package ID:** `{info['package_id']}`\n"
            f"🔢 **Version:** `{version_str}`\n"
            f"⭐ **Rating:** `{rating_str}`\n"
            f"📂 **Category:** `{genre_str}`\n\n"
            f"📱 **Which architecture is your device / RAM?**\n"
            f"Select the CPU & RAM architecture matching your Android device:\n\n"
            f"• 📱 **ARM64 (arm64-v8a)**: Modern 64-bit devices (4GB, 6GB, 8GB+ RAM)\n"
            f"• 📱 **ARMv7 (armeabi-v7a)**: 32-bit devices (<4GB RAM / Older phones)\n"
            f"• 💻 **x86_64 / x86**: PC Emulators (BlueStacks, LDPlayer, Nox)\n"
            f"• 🌐 **Universal**: Universal package for all devices\n\n"
            f"👉 **Tap a button below to start download:**"
        )

        buttons = [
            [
                Button.inline("📱 ARM64 (arm64-v8a | 4GB+ RAM)", data=f"gp:{task_id}:arm64-v8a"),
            ],
            [
                Button.inline("📱 ARMv7 (armeabi-v7a | <4GB RAM)", data=f"gp:{task_id}:armeabi-v7a"),
            ],
            [
                Button.inline("💻 x86_64 (64-bit PC)", data=f"gp:{task_id}:x86_64"),
                Button.inline("💻 x86 (32-bit PC)", data=f"gp:{task_id}:x86"),
            ],
            [
                Button.inline("🌐 Universal (Compatible All)", data=f"gp:{task_id}:universal"),
            ],
            [Button.inline("❌ Cancel", data=f"cancel_gp:{task_id}")],
        ]

        await status_msg.edit(caption, buttons=buttons)
    except asyncio.CancelledError:
        await status_msg.edit("❌ **Cancelled by user.**")
    except Exception as e:
        logger.error(f"Google Play request error: {e}")
        await status_msg.edit(f"❌ **Error:** {str(e)}")
    finally:
        active_jobs.pop(task_id, None)


@client.on(events.CallbackQuery(pattern=r"^gp:(.+):(.+)$"))
async def googleplay_callback(event):
    task_id = event.pattern_match.group(1).decode("utf-8")
    arch = event.pattern_match.group(2).decode("utf-8")

    task_info = pending_gplay_tasks.pop(task_id, None)
    if not task_info:
        await event.answer("Task expired. Please re-send the Google Play link.", alert=True)
        return

    job_task_id = uuid.uuid4().hex[:8]
    t = asyncio.create_task(run_googleplay_job(event, task_info, arch, job_task_id))
    active_jobs[job_task_id] = {"task": t, "chat_id": event.chat_id}


@client.on(events.CallbackQuery(pattern=r"^cancel_gp:(.+)$"))
async def cancel_gp_callback(event):
    task_id = event.pattern_match.group(1).decode("utf-8")
    pending_gplay_tasks.pop(task_id, None)
    await event.edit("❌ **Operation cancelled.**")


async def run_googleplay_job(event, task_info: Dict[str, Any], arch: str, task_id: str):
    task_dir = create_task_dir(task_id)
    if task_id in active_jobs:
        active_jobs[task_id]["task_dir"] = task_dir

    arch_labels = {
        "arm64-v8a": "ARM64 (4GB+ RAM)",
        "armeabi-v7a": "ARMv7 (<4GB RAM)",
        "x86_64": "x86_64 (64-bit PC)",
        "x86": "x86 (32-bit PC)",
        "universal": "Universal",
    }
    label = arch_labels.get(arch, arch)

    status_msg = await event.edit(
        f"⏳ **Downloading {task_info['title']} ({label})...**\nPlease wait, preparing installation package...",
        buttons=make_cancel_button(task_id)
    )

    try:
        async with active_semaphore:
            cancel_check = lambda: task_id not in active_jobs
            res = await asyncio.to_thread(
                GooglePlayDownloader.download,
                task_info["package_id"],
                arch,
                task_dir,
                cancel_check,
            )

            if not res.get("success") or not res.get("path"):
                await event.edit(f"❌ **Download failed:** {res.get('error', 'Unknown error')}")
                return

            file_path = res["path"]
            thumb_path = res.get("thumb_path")
            file_type = res.get("file_type", "apk").upper()
            size_str = human_bytes(res.get("size_bytes", file_path.stat().st_size))

            progress = TelegramProgress(event, action=f"Uploading {file_type} to Telegram", buttons=make_cancel_button(task_id))
            caption = (
                f"🏪 **{res.get('title', task_info['title'])}**\n"
                f"👤 Developer: `{res.get('developer', task_info.get('developer', 'Unknown'))}`\n"
                f"📦 Package: `{task_info['package_id']}`\n"
                f"⚙️ Architecture: `{label}`\n"
                f"🔢 Version: `{res.get('version', 'Latest')}`\n"
                f"💾 Size: `{size_str}`\n"
                f"📁 Format: `{file_type}`"
            )

            await client.send_file(
                event.chat_id,
                file_path,
                caption=caption,
                thumb=thumb_path if thumb_path and thumb_path.exists() else None,
                force_document=True,
                progress_callback=progress.callback,
            )

            await event.delete()

    except asyncio.CancelledError:
        logger.info(f"Google Play job {task_id} cancelled by user.")
    except Exception as e:
        logger.error(f"Google Play download/upload error: {e}")
        await event.edit(f"❌ **Error:** {str(e)}")
    finally:
        cleanup_dir(task_dir)
        active_jobs.pop(task_id, None)


# -------------------------------------------------------------
# YouTube Handler
# -------------------------------------------------------------
async def handle_youtube_request(event, url: str, task_id: str):
    status_msg = await event.reply("🔍 **Fetching YouTube video information...**", buttons=make_cancel_button(task_id))

    try:
        info = await asyncio.to_thread(YouTubeDownloader.get_info, url)
        if not info:
            await status_msg.edit("❌ **Could not retrieve video details.** Please verify the link is public.")
            return

        video_id = info["id"]
        pending_yt_tasks[video_id] = {
            "url": url,
            "title": info["title"],
            "duration": info["duration"],
            "uploader": info["uploader"],
        }

        dur_mins = f"{info['duration']//60}:{info['duration']%60:02d}" if info["duration"] else "N/A"
        caption = (
            f"📺 **{info['title']}**\n\n"
            f"👤 **Channel:** `{info['uploader']}`\n"
            f"⏱️ **Duration:** `{dur_mins}`\n\n"
            f"👉 **Tap a button to choose download quality:**"
        )

        buttons = [
            [
                Button.inline("🎬 1080p FHD", data=f"yt:1080:{video_id}"),
                Button.inline("🎬 720p HD", data=f"yt:720:{video_id}"),
            ],
            [
                Button.inline("🎬 480p SD", data=f"yt:480:{video_id}"),
                Button.inline("🎬 360p Low", data=f"yt:360:{video_id}"),
            ],
            [
                Button.inline("🎵 Audio (MP3 320k)", data=f"yt:mp3:{video_id}"),
                Button.inline("🎞️ Convert to GIF", data=f"yt:gif:{video_id}"),
            ],
            [Button.inline("❌ Cancel", data=f"cancel:{video_id}")],
        ]

        await status_msg.edit(caption, buttons=buttons)
    except asyncio.CancelledError:
        await status_msg.edit("❌ **Cancelled by user.**")
    except Exception as e:
        await status_msg.edit(f"❌ **Error:** {str(e)}")
    finally:
        active_jobs.pop(task_id, None)


@client.on(events.CallbackQuery(pattern=r"^yt:(.+):(.+)$"))
async def youtube_callback(event):
    quality = event.pattern_match.group(1).decode("utf-8")
    video_id = event.pattern_match.group(2).decode("utf-8")

    task = pending_yt_tasks.get(video_id)
    if not task:
        await event.answer("Task expired. Please re-send the YouTube link.", alert=True)
        return

    task_id = uuid.uuid4().hex[:8]
    t = asyncio.create_task(run_youtube_job(event, task, quality, task_id))
    active_jobs[task_id] = {"task": t, "chat_id": event.chat_id}


async def run_youtube_job(event, task: Dict[str, Any], quality: str, task_id: str):
    url = task["url"]
    label = "Audio MP3" if quality == "mp3" else ("GIF" if quality == "gif" else f"{quality}p")
    task_dir = create_task_dir(task_id)

    if task_id in active_jobs:
        active_jobs[task_id]["task_dir"] = task_dir

    status_msg = await event.edit(
        f"⏳ **Downloading YouTube ({label})...**\nPlease wait.",
        buttons=make_cancel_button(task_id)
    )

    try:
        async with active_semaphore:
            target_quality = "720" if quality == "gif" else quality
            media_path, thumb_path, meta = await asyncio.to_thread(
                YouTubeDownloader.download, url, target_quality, task_dir
            )

            if not media_path or not media_path.exists():
                await event.edit(f"❌ **Download failed:** {meta.get('error', 'Unknown error')}")
                return

            if quality == "gif":
                await event.edit("🔄 **Converting video to animated GIF...**", buttons=make_cancel_button(task_id))
                gif_path = await asyncio.to_thread(video_to_gif, media_path)
                if gif_path and gif_path.exists():
                    media_path = gif_path

            progress = TelegramProgress(event, action="Uploading to Telegram", buttons=make_cancel_button(task_id))
            caption = f"📺 **{task['title']}**\n👤 Channel: `{task['uploader']}`"

            if quality == "mp3":
                dur = int(task.get("duration", 0) or get_audio_duration_seconds(media_path))
                await client.send_file(
                    event.chat_id,
                    media_path,
                    caption=caption,
                    thumb=thumb_path,
                    attributes=[DocumentAttributeAudio(duration=dur, title=task["title"], performer=task["uploader"])],
                    progress_callback=progress.callback,
                )
            elif quality == "gif":
                await client.send_file(
                    event.chat_id,
                    media_path,
                    caption=caption,
                    progress_callback=progress.callback,
                )
            else:
                await client.send_file(
                    event.chat_id,
                    media_path,
                    caption=caption,
                    thumb=thumb_path,
                    supports_streaming=True,
                    progress_callback=progress.callback,
                )

            await event.delete()

    except asyncio.CancelledError:
        logger.info(f"YouTube job {task_id} cancelled by user.")
    except Exception as e:
        logger.error(f"YouTube download/upload error: {e}")
        await event.edit(f"❌ **Error:** {str(e)}")
    finally:
        cleanup_dir(task_dir)
        active_jobs.pop(task_id, None)


# -------------------------------------------------------------
# Instagram Job
# -------------------------------------------------------------
async def run_instagram_job(event, url: str, task_id: str):
    task_dir = create_task_dir(task_id)
    if task_id in active_jobs:
        active_jobs[task_id]["task_dir"] = task_dir

    status_msg = await event.reply(
        "⏳ **Fetching Instagram media (including all carousel items)...**",
        buttons=make_cancel_button(task_id)
    )

    try:
        async with active_semaphore:
            res = await InstagramDownloader.download(url, task_dir)
            if not res.get("success") or not res.get("media_items"):
                error_msg = res.get("error", "Failed to retrieve Instagram media.")
                await status_msg.edit(f"❌ **{error_msg}**")
                return

            media_items = res["media_items"]
            caption = res.get("caption", "")
            if len(caption) > 800:
                caption = caption[:800] + "..."
            
            author = res.get("author", "").strip()
            if author and author.lower() not in ["instagram", "instagram user"]:
                header = f"📸 **Instagram by {author}**"
            else:
                header = "📸 **Instagram**"
            full_caption = f"{header}\n\n{caption}".strip() if caption else header

            await status_msg.edit(f"📤 **Uploading {len(media_items)} media item(s)...**", buttons=make_cancel_button(task_id))
            file_paths = [item["path"] for item in media_items]

            if len(file_paths) == 1:
                item_path = file_paths[0]
                is_vid = item_path.suffix.lower() in [".mp4", ".mkv", ".webm", ".mov"]
                attrs, thumb = prepare_video_attributes(item_path) if is_vid else (None, None)
                progress = TelegramProgress(status_msg, action="Uploading", buttons=make_cancel_button(task_id))
                await client.send_file(
                    event.chat_id,
                    item_path,
                    caption=full_caption,
                    thumb=thumb,
                    attributes=attrs,
                    supports_streaming=is_vid,
                    progress_callback=progress.callback,
                )
            else:
                chunk_size = config.MAX_ALBUM_SIZE
                for i in range(0, len(file_paths), chunk_size):
                    chunk = file_paths[i:i + chunk_size]
                    chunk_caption = full_caption if i == 0 else ""
                    await client.send_file(
                        event.chat_id,
                        chunk,
                        caption=chunk_caption,
                        album=True,
                    )

            await status_msg.delete()

    except asyncio.CancelledError:
        logger.info(f"Instagram job {task_id} cancelled by user.")
    except Exception as e:
        logger.error(f"Instagram error: {e}")
        await status_msg.edit(f"❌ **Error downloading Instagram media:** {str(e)}")
    finally:
        cleanup_dir(task_dir)
        active_jobs.pop(task_id, None)


# -------------------------------------------------------------
# TikTok Job
# -------------------------------------------------------------
async def run_tiktok_job(event, url: str, task_id: str):
    task_dir = create_task_dir(task_id)
    if task_id in active_jobs:
        active_jobs[task_id]["task_dir"] = task_dir

    status_msg = await event.reply(
        "⏳ **Fetching TikTok media (HD & no watermark)...**",
        buttons=make_cancel_button(task_id)
    )

    try:
        async with active_semaphore:
            res = await TikTokDownloader.download(url, task_dir)
            if not res.get("success"):
                await status_msg.edit(f"❌ **{res.get('error', 'This TikTok video is unavailable or deleted.')}**")
                return

            media_type = res.get("type", "video")
            media_files = res.get("media_files", [])
            audio_file = res.get("audio_file")
            title = res.get("title", "")
            author = res.get("author", "TikTok User")
            caption = f"🎵 **TikTok by {author}**\n\n{title}".strip()

            if media_type == "photos":
                await status_msg.edit(f"📤 **Uploading {len(media_files)} slideshow photo(s)...**", buttons=make_cancel_button(task_id))
                for i in range(0, len(media_files), config.MAX_ALBUM_SIZE):
                    chunk = media_files[i:i + config.MAX_ALBUM_SIZE]
                    c_text = caption if i == 0 else ""
                    await client.send_file(event.chat_id, chunk, caption=c_text, album=True)
                
                if audio_file and audio_file.exists():
                    dur = get_audio_duration_seconds(audio_file)
                    await client.send_file(
                        event.chat_id,
                        audio_file,
                        caption="🎵 **TikTok Background Music**",
                        attributes=[DocumentAttributeAudio(duration=dur, title=title, performer=author)],
                    )
            else:
                vid_file = media_files[0]
                attrs, thumb = prepare_video_attributes(vid_file)
                progress = TelegramProgress(status_msg, action="Uploading TikTok video", buttons=make_cancel_button(task_id))
                await client.send_file(
                    event.chat_id,
                    vid_file,
                    caption=caption,
                    thumb=thumb,
                    attributes=attrs,
                    supports_streaming=True,
                    progress_callback=progress.callback,
                )
                if audio_file and audio_file.exists():
                    dur = get_audio_duration_seconds(audio_file)
                    await client.send_file(
                        event.chat_id,
                        audio_file,
                        caption="🎵 **Original Audio Track (MP3)**",
                        attributes=[DocumentAttributeAudio(duration=dur, title=title, performer=author)],
                    )

            await status_msg.delete()

    except asyncio.CancelledError:
        logger.info(f"TikTok job {task_id} cancelled by user.")
    except Exception as e:
        logger.error(f"TikTok error: {e}")
        await status_msg.edit(f"❌ **Error downloading TikTok media:** {str(e)}")
    finally:
        cleanup_dir(task_dir)
        active_jobs.pop(task_id, None)


# -------------------------------------------------------------
# Twitter / X Job
# -------------------------------------------------------------
async def run_twitter_job(event, url: str, task_id: str):
    task_dir = create_task_dir(task_id)
    if task_id in active_jobs:
        active_jobs[task_id]["task_dir"] = task_dir

    status_msg = await event.reply(
        "⏳ **Fetching Twitter / X media...**",
        buttons=make_cancel_button(task_id)
    )

    try:
        async with active_semaphore:
            res = await TwitterDownloader.download(url, task_dir)
            if not res.get("success") or not res.get("media_items"):
                await status_msg.edit(f"❌ **{res.get('error', 'No media found or download failed.')}**")
                return

            items = res["media_items"]
            caption = res.get("text", "")
            author = res.get("author", "Twitter")
            full_caption = f"🐦 **{author}**\n\n{caption}".strip()

            await status_msg.edit(f"📤 **Uploading {len(items)} item(s)...**", buttons=make_cancel_button(task_id))
            all_images = all(item["type"] == "image" for item in items)

            if len(items) == 1:
                item = items[0]
                progress = TelegramProgress(status_msg, action="Uploading", buttons=make_cancel_button(task_id))
                if item["type"] in ["video", "gif"]:
                    is_gif = (item["type"] == "gif")
                    attrs, thumb = prepare_video_attributes(
                        item["path"],
                        thumb_path=item.get("thumb"),
                        duration=item.get("duration"),
                        width=item.get("width"),
                        height=item.get("height"),
                        is_gif=is_gif,
                    )
                    await client.send_file(
                        event.chat_id,
                        item["path"],
                        caption=full_caption,
                        thumb=thumb,
                        attributes=attrs,
                        supports_streaming=True,
                        progress_callback=progress.callback,
                    )
                else:
                    await client.send_file(
                        event.chat_id,
                        item["path"],
                        caption=full_caption,
                        progress_callback=progress.callback,
                    )
            elif all_images:
                paths = [it["path"] for it in items]
                for i in range(0, len(paths), config.MAX_ALBUM_SIZE):
                    chunk = paths[i:i + config.MAX_ALBUM_SIZE]
                    c_text = full_caption if i == 0 else ""
                    await client.send_file(event.chat_id, chunk, caption=c_text, album=True)
            else:
                for idx, it in enumerate(items):
                    c_text = full_caption if idx == 0 else ""
                    if it["type"] in ["video", "gif"]:
                        is_gif = (it["type"] == "gif")
                        attrs, thumb = prepare_video_attributes(
                            it["path"],
                            thumb_path=it.get("thumb"),
                            duration=it.get("duration"),
                            width=it.get("width"),
                            height=it.get("height"),
                            is_gif=is_gif,
                        )
                        await client.send_file(
                            event.chat_id,
                            it["path"],
                            caption=c_text,
                            thumb=thumb,
                            attributes=attrs,
                            supports_streaming=True,
                        )
                    else:
                        await client.send_file(
                            event.chat_id,
                            it["path"],
                            caption=c_text,
                        )

            await status_msg.delete()

    except asyncio.CancelledError:
        logger.info(f"Twitter job {task_id} cancelled by user.")
    except Exception as e:
        logger.error(f"Twitter error: {e}")
        await status_msg.edit(f"❌ **Error downloading Twitter media:** {str(e)}")
    finally:
        cleanup_dir(task_dir)
        active_jobs.pop(task_id, None)


# -------------------------------------------------------------
# Spotify Job
# -------------------------------------------------------------
async def run_spotify_job(event, url: str, task_id: str):
    task_dir = create_task_dir(task_id)
    if task_id in active_jobs:
        active_jobs[task_id]["task_dir"] = task_dir

    status_msg = await event.reply(
        "⏳ **Searching & downloading Spotify track(s)...**",
        buttons=make_cancel_button(task_id)
    )

    try:
        async with active_semaphore:
            res = await SpotifyDownloader.download(url, task_dir)
            if not res.get("success") or not res.get("tracks"):
                await status_msg.edit(f"❌ **{res.get('error', 'Failed to download Spotify audio.')}**")
                return

            tracks = res["tracks"]
            await status_msg.edit(f"📤 **Uploading {len(tracks)} audio track(s)...**", buttons=make_cancel_button(task_id))

            for t in tracks:
                track_path = t["path"]
                title = t.get("title", track_path.stem)
                thumb_path = t.get("thumb")
                dur = get_audio_duration_seconds(track_path)
                progress = TelegramProgress(status_msg, action=f"Uploading {title[:20]}", buttons=make_cancel_button(task_id))
                await client.send_file(
                    event.chat_id,
                    track_path,
                    caption=f"🎧 `{title}`",
                    thumb=thumb_path if thumb_path and thumb_path.exists() else None,
                    attributes=[DocumentAttributeAudio(duration=dur, title=title, performer="Spotify")],
                    progress_callback=progress.callback,
                )

            await status_msg.delete()

    except asyncio.CancelledError:
        logger.info(f"Spotify job {task_id} cancelled by user.")
    except Exception as e:
        logger.error(f"Spotify error: {e}")
        await status_msg.edit(f"❌ **Error downloading Spotify track:** {str(e)}")
    finally:
        cleanup_dir(task_dir)
        active_jobs.pop(task_id, None)


# -------------------------------------------------------------
# Pinterest Job
# -------------------------------------------------------------
async def run_pinterest_job(event, url: str, task_id: str):
    task_dir = create_task_dir(task_id)
    if task_id in active_jobs:
        active_jobs[task_id]["task_dir"] = task_dir

    status_msg = await event.reply(
        "⏳ **Fetching Pinterest media (HD video / original image)...**",
        buttons=make_cancel_button(task_id)
    )

    try:
        async with active_semaphore:
            res = await PinterestDownloader.download(url, task_dir)
            if not res.get("success") or not res.get("path"):
                await status_msg.edit(f"❌ **{res.get('error', 'Could not download this Pinterest pin.')}**")
                return

            media_type = res.get("type", "image")
            file_path = res["path"]
            title = res.get("title", "Pinterest Pin")
            caption = f"📌 **Pinterest**\n\n{title}".strip()

            progress = TelegramProgress(status_msg, action="Uploading to Telegram", buttons=make_cancel_button(task_id))
            if media_type == "video":
                attrs, thumb = prepare_video_attributes(file_path)
                await client.send_file(
                    event.chat_id,
                    file_path,
                    caption=caption,
                    thumb=thumb,
                    attributes=attrs,
                    supports_streaming=True,
                    progress_callback=progress.callback,
                )
            else:
                await client.send_file(
                    event.chat_id,
                    file_path,
                    caption=caption,
                    progress_callback=progress.callback,
                )

            await status_msg.delete()

    except asyncio.CancelledError:
        logger.info(f"Pinterest job {task_id} cancelled by user.")
    except Exception as e:
        logger.error(f"Pinterest error: {e}")
        await status_msg.edit(f"❌ **Error downloading Pinterest pin:** {str(e)}")
    finally:
        cleanup_dir(task_dir)
        active_jobs.pop(task_id, None)


# -------------------------------------------------------------
# Reddit Job
# -------------------------------------------------------------
async def run_reddit_job(event, url: str, task_id: str):
    task_dir = create_task_dir(task_id)
    if task_id in active_jobs:
        active_jobs[task_id]["task_dir"] = task_dir

    status_msg = await event.reply(
        "⏳ **Fetching Reddit media (with audio & galleries)...**",
        buttons=make_cancel_button(task_id)
    )

    try:
        async with active_semaphore:
            res = await RedditDownloader.download(url, task_dir)
            if not res.get("success") or not res.get("media_items"):
                await status_msg.edit(f"❌ **{res.get('error', 'Failed to retrieve Reddit media.')}**")
                return

            items = res["media_items"]
            title = res.get("title", "")
            author = res.get("author", "Reddit User")
            caption = f"🤖 **Reddit (r/{author})**\n\n{title}".strip()

            await status_msg.edit(f"📤 **Uploading {len(items)} Reddit media item(s)...**", buttons=make_cancel_button(task_id))

            if len(items) == 1:
                it = items[0]
                is_vid = (it.get("type") == "video")
                attrs, thumb = prepare_video_attributes(it["path"]) if is_vid else (None, None)
                progress = TelegramProgress(status_msg, action="Uploading", buttons=make_cancel_button(task_id))
                await client.send_file(
                    event.chat_id,
                    it["path"],
                    caption=caption,
                    thumb=thumb,
                    attributes=attrs,
                    supports_streaming=is_vid,
                    progress_callback=progress.callback,
                )
            else:
                paths = [it["path"] for it in items]
                for i in range(0, len(paths), config.MAX_ALBUM_SIZE):
                    chunk = paths[i:i + config.MAX_ALBUM_SIZE]
                    c_text = caption if i == 0 else ""
                    await client.send_file(event.chat_id, chunk, caption=c_text, album=True)

            await status_msg.delete()

    except asyncio.CancelledError:
        logger.info(f"Reddit job {task_id} cancelled by user.")
    except Exception as e:
        logger.error(f"Reddit error: {e}")
        await status_msg.edit(f"❌ **Error downloading Reddit media:** {str(e)}")
    finally:
        cleanup_dir(task_dir)
        active_jobs.pop(task_id, None)


# -------------------------------------------------------------
# SoundCloud Job
# -------------------------------------------------------------
async def run_soundcloud_job(event, url: str, task_id: str):
    task_dir = create_task_dir(task_id)
    if task_id in active_jobs:
        active_jobs[task_id]["task_dir"] = task_dir

    status_msg = await event.reply(
        "⏳ **Fetching SoundCloud audio (320kbps MP3 + cover art)...**",
        buttons=make_cancel_button(task_id)
    )

    try:
        async with active_semaphore:
            res = await SoundCloudDownloader.download(url, task_dir)
            if not res.get("success") or not res.get("tracks"):
                await status_msg.edit(f"❌ **{res.get('error', 'Failed to download SoundCloud audio.')}**")
                return

            tracks = res["tracks"]
            await status_msg.edit(f"📤 **Uploading {len(tracks)} audio track(s)...**", buttons=make_cancel_button(task_id))

            for t in tracks:
                track_path = t["path"]
                title = t.get("title", track_path.stem)
                author = t.get("author", "SoundCloud Artist")
                thumb_path = t.get("thumb")
                dur = get_audio_duration_seconds(track_path)
                progress = TelegramProgress(status_msg, action=f"Uploading {title[:20]}", buttons=make_cancel_button(task_id))
                await client.send_file(
                    event.chat_id,
                    track_path,
                    caption=f"☁️ **SoundCloud**\n🎵 `{title}`",
                    thumb=thumb_path if thumb_path and thumb_path.exists() else None,
                    attributes=[DocumentAttributeAudio(duration=dur, title=title, performer=author)],
                    progress_callback=progress.callback,
                )

            await status_msg.delete()

    except asyncio.CancelledError:
        logger.info(f"SoundCloud job {task_id} cancelled by user.")
    except Exception as e:
        logger.error(f"SoundCloud error: {e}")
        await status_msg.edit(f"❌ **Error downloading SoundCloud audio:** {str(e)}")
    finally:
        cleanup_dir(task_dir)
        active_jobs.pop(task_id, None)


# -------------------------------------------------------------
# Facebook Job
# -------------------------------------------------------------
async def run_facebook_job(event, url: str, task_id: str):
    task_dir = create_task_dir(task_id)
    if task_id in active_jobs:
        active_jobs[task_id]["task_dir"] = task_dir

    status_msg = await event.reply(
        "⏳ **Fetching Facebook video / Reel...**",
        buttons=make_cancel_button(task_id)
    )

    try:
        async with active_semaphore:
            res = await FacebookDownloader.download(url, task_dir)
            if not res.get("success") or not res.get("path"):
                await status_msg.edit(f"❌ **{res.get('error', 'Could not download Facebook video.')}**")
                return

            vid_path = res["path"]
            title = res.get("title", "Facebook Video")
            author = res.get("author", "Facebook")
            caption = f"📘 **Facebook by {author}**\n\n{title}".strip()

            attrs, thumb = prepare_video_attributes(vid_path)
            progress = TelegramProgress(status_msg, action="Uploading Facebook video", buttons=make_cancel_button(task_id))
            await client.send_file(
                event.chat_id,
                vid_path,
                caption=caption,
                thumb=thumb,
                attributes=attrs,
                supports_streaming=True,
                progress_callback=progress.callback,
            )

            await status_msg.delete()

    except asyncio.CancelledError:
        logger.info(f"Facebook job {task_id} cancelled by user.")
    except Exception as e:
        logger.error(f"Facebook error: {e}")
        await status_msg.edit(f"❌ **Error downloading Facebook video:** {str(e)}")
    finally:
        cleanup_dir(task_dir)
        active_jobs.pop(task_id, None)


# -------------------------------------------------------------
# Direct File Download Job
# -------------------------------------------------------------
async def run_direct_job(event, url: str, task_id: str):
    task_dir = create_task_dir(task_id)
    if task_id in active_jobs:
        active_jobs[task_id]["task_dir"] = task_dir

    status_msg = await event.reply(
        "⏳ **Connecting to direct download link...**\nPlease wait.",
        buttons=make_cancel_button(task_id)
    )

    try:
        async with active_semaphore:
            loop = asyncio.get_running_loop()

            async def _safe_status_edit(text: str):
                try:
                    await status_msg.edit(text, buttons=make_cancel_button(task_id))
                except Exception:
                    pass

            def on_progress(current: int, total: Optional[int]):
                cur_str = human_bytes(current)
                if total and total > 0:
                    percent = min(100.0, (current / total) * 100)
                    tot_str = human_bytes(total)
                    bar = create_bar(percent)
                    text = (
                        f"⏳ **Downloading file from server...**\n\n"
                        f"{bar} `{percent:.1f}%`\n"
                        f"📦 **Downloaded:** `{cur_str} / {tot_str}`"
                    )
                else:
                    text = (
                        f"⏳ **Downloading file from server...**\n\n"
                        f"📦 **Downloaded:** `{cur_str}`"
                    )
                asyncio.run_coroutine_threadsafe(_safe_status_edit(text), loop)

            res = await DirectDownloader.download(url, task_dir, progress_callback=on_progress)
            if not res.get("success") or not res.get("path"):
                err = res.get("error", "Could not download file from direct link.")
                await status_msg.edit(f"❌ **{err}**")
                return

            file_path = res["path"]
            filename = res["filename"]
            file_type = res["type"]
            size_bytes = res["size_bytes"]
            size_str = human_bytes(size_bytes)
            thumb = res.get("thumb")
            duration = res.get("duration", 0)

            await status_msg.edit(
                f"📤 **Uploading `{filename}` ({size_str}) to Telegram...**",
                buttons=make_cancel_button(task_id)
            )

            progress = TelegramProgress(status_msg, action=f"Uploading {file_type.title()}", buttons=make_cancel_button(task_id))

            if file_type == "video":
                caption = f"🎬 **{filename}**\n📊 Size: `{size_str}`"
                attrs, thumb = prepare_video_attributes(file_path, thumb_path=thumb)
                await client.send_file(
                    event.chat_id,
                    file_path,
                    caption=caption,
                    thumb=thumb,
                    attributes=attrs,
                    supports_streaming=True,
                    progress_callback=progress.callback,
                )
            elif file_type == "audio":
                caption = f"🎵 **{filename}**\n📊 Size: `{size_str}`"
                await client.send_file(
                    event.chat_id,
                    file_path,
                    caption=caption,
                    thumb=thumb if thumb and thumb.exists() else None,
                    attributes=[DocumentAttributeAudio(duration=duration, title=file_path.stem, performer="Direct Download")],
                    progress_callback=progress.callback,
                )
            elif file_type == "photo" and size_bytes < 10 * 1024 * 1024:
                caption = f"🖼️ **{filename}**\n📊 Size: `{size_str}`"
                await client.send_file(
                    event.chat_id,
                    file_path,
                    caption=caption,
                    progress_callback=progress.callback,
                )
            else:
                caption = f"📁 **{filename}**\n📊 Size: `{size_str}`"
                await client.send_file(
                    event.chat_id,
                    file_path,
                    caption=caption,
                    force_document=True,
                    progress_callback=progress.callback,
                )

            await status_msg.delete()

    except asyncio.CancelledError:
        logger.info(f"Direct download job {task_id} cancelled by user.")
    except Exception as e:
        logger.error(f"Direct download error: {e}")
        await status_msg.edit(f"❌ **Direct download error:** {str(e)}")
    finally:
        cleanup_dir(task_dir)
        active_jobs.pop(task_id, None)


# -------------------------------------------------------------
# Converter Callback Handlers
# -------------------------------------------------------------
@client.on(events.CallbackQuery(pattern=r"^conv:(.+):(.+)$"))
async def converter_callback(event):
    conv_type = event.pattern_match.group(1).decode("utf-8")
    file_id = event.pattern_match.group(2).decode("utf-8")
    task_id = uuid.uuid4().hex[:8]

    t = asyncio.create_task(run_converter_job(event, conv_type, file_id, task_id))
    active_jobs[task_id] = {"task": t, "chat_id": event.chat_id}


async def run_converter_job(event, conv_type: str, file_id: str, task_id: str):
    task_dir = create_task_dir(task_id)
    if task_id in active_jobs:
        active_jobs[task_id]["task_dir"] = task_dir

    await event.edit(
        f"⏳ **Processing conversion ({conv_type})...**\nDownloading original file...",
        buttons=make_cancel_button(task_id)
    )

    try:
        orig_msg = await event.get_reply_message()
        if not orig_msg or not orig_msg.media:
            f_info = pending_file_ops.get(file_id)
            if f_info:
                orig_msg = await client.get_messages(f_info["chat_id"], ids=f_info["message_id"])
        if not orig_msg or not orig_msg.media:
            await event.edit("❌ **Original media message not found.**")
            return

        input_path = task_dir / "input_media.mp4"
        dl_prog = TelegramProgress(event, action="Downloading original video", buttons=make_cancel_button(task_id))
        await client.download_media(orig_msg.media, file=str(input_path), progress_callback=dl_prog.callback)

        output_file = None
        if conv_type == "mp3":
            await event.edit("🔄 **Extracting audio (MP3 320kbps)...**", buttons=make_cancel_button(task_id))
            output_file = await asyncio.to_thread(video_to_mp3, input_path)
        elif conv_type == "gif":
            await event.edit("🔄 **Generating high-quality GIF...**", buttons=make_cancel_button(task_id))
            output_file = await asyncio.to_thread(video_to_gif, input_path)
        elif conv_type in ["720", "480", "360"]:
            height = int(conv_type)
            await event.edit(f"🔄 **Converting resolution to {height}p...**", buttons=make_cancel_button(task_id))
            output_file = await asyncio.to_thread(convert_video_quality, input_path, height)

        if not output_file or not output_file.exists():
            await event.edit("❌ **Conversion failed.**")
            return

        progress = TelegramProgress(event, action="Uploading converted file", buttons=make_cancel_button(task_id))
        if conv_type == "mp3":
            dur = get_audio_duration_seconds(output_file)
            media_name = getattr(orig_msg.file, 'name', 'Converted Audio') or "Converted Audio"
            await client.send_file(
                event.chat_id,
                output_file,
                caption="🎵 **Extracted Audio (320kbps MP3)**",
                attributes=[DocumentAttributeAudio(duration=dur, title=media_name, performer="AllDLBot")],
                progress_callback=progress.callback,
            )
        else:
            await client.send_file(
                event.chat_id,
                output_file,
                caption=f"🎬 **Converted Media ({conv_type})**",
                supports_streaming=True,
                progress_callback=progress.callback,
            )

        await event.delete()

    except asyncio.CancelledError:
        logger.info(f"Converter job {task_id} cancelled by user.")
    except Exception as e:
        logger.error(f"Converter error: {e}")
        await event.edit(f"❌ **Conversion error:** {str(e)}")
    finally:
        cleanup_dir(task_dir)
        active_jobs.pop(task_id, None)


# -------------------------------------------------------------
# File Operations: Rename & Format Conversion Handlers
# -------------------------------------------------------------
def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\x00\\/:*?"<>|]', '_', name.strip())
    cleaned = cleaned.strip(". ")
    return cleaned or "file"


@client.on(events.CallbackQuery(pattern=r"^fop:rename:(.+)$"))
async def file_rename_callback(event):
    file_id = event.pattern_match.group(1).decode("utf-8")
    file_info = pending_file_ops.get(file_id)
    if not file_info:
        await event.edit("⚠️ **Session expired or file not found.** Please send or forward the file again.")
        return

    waiting_rename_tasks[event.chat_id] = file_id
    waiting_custom_ext_tasks.pop(event.chat_id, None)

    buttons = [
        [Button.inline("⬅️ Back", data=f"fop:back:{file_id}")],
        [Button.inline("❌ Cancel", data=b"cancel_menu")],
    ]
    ext_hint = f"`{file_info['ext']}`" if file_info.get("ext") else "the original extension"
    msg_text = (
        f"✏️ **Rename File:** `{file_info['original_name']}`\n\n"
        f"Please send the **new name** for this file in your next message.\n"
        f"_(Example: `My Presentation` or `Report_2026.pdf`)_\n\n"
        f"💡 *Tip: If you omit the extension, {ext_hint} will be preserved automatically.*"
    )
    await event.edit(msg_text, buttons=buttons)


@client.on(events.CallbackQuery(pattern=r"^fop:fmt_menu:(.+)$"))
async def file_format_menu_callback(event):
    file_id = event.pattern_match.group(1).decode("utf-8")
    file_info = pending_file_ops.get(file_id)
    if not file_info:
        await event.edit("⚠️ **Session expired or file not found.** Please send or forward the file again.")
        return

    waiting_rename_tasks.pop(event.chat_id, None)
    waiting_custom_ext_tasks.pop(event.chat_id, None)

    ext = (file_info.get("ext") or "").lower()
    mime = (file_info.get("mime_type") or "").lower()

    if mime.startswith("video") or ext in [".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv"]:
        buttons = [
            [
                Button.inline("🎬 MP4", data=f"fop:conv:{file_id}:.mp4"),
                Button.inline("🎬 MKV", data=f"fop:conv:{file_id}:.mkv"),
            ],
            [
                Button.inline("🎬 WEBM", data=f"fop:conv:{file_id}:.webm"),
                Button.inline("🎬 AVI", data=f"fop:conv:{file_id}:.avi"),
            ],
            [
                Button.inline("🎵 MP3 Audio", data=f"fop:conv:{file_id}:.mp3"),
                Button.inline("🎞️ GIF Animation", data=f"fop:conv:{file_id}:.gif"),
            ],
            [
                Button.inline("⚙️ Custom Extension", data=f"fop:custom:{file_id}"),
            ],
            [
                Button.inline("⬅️ Back", data=f"fop:back:{file_id}"),
                Button.inline("❌ Cancel", data=b"cancel_menu"),
            ],
        ]
    elif mime.startswith("audio") or ext in [".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"]:
        buttons = [
            [
                Button.inline("🎵 MP3 (320k)", data=f"fop:conv:{file_id}:.mp3"),
                Button.inline("🎵 M4A (AAC)", data=f"fop:conv:{file_id}:.m4a"),
            ],
            [
                Button.inline("🎵 WAV (Lossless)", data=f"fop:conv:{file_id}:.wav"),
                Button.inline("🎵 FLAC (Hi-Fi)", data=f"fop:conv:{file_id}:.flac"),
            ],
            [
                Button.inline("🎵 OGG (Vorbis)", data=f"fop:conv:{file_id}:.ogg"),
                Button.inline("🎵 OPUS", data=f"fop:conv:{file_id}:.opus"),
            ],
            [
                Button.inline("⚙️ Custom Extension", data=f"fop:custom:{file_id}"),
            ],
            [
                Button.inline("⬅️ Back", data=f"fop:back:{file_id}"),
                Button.inline("❌ Cancel", data=b"cancel_menu"),
            ],
        ]
    elif mime.startswith("image") or ext in [".jpg", ".jpeg", ".png", ".webp", ".bmp", ".pdf"]:
        buttons = [
            [
                Button.inline("🖼️ PNG", data=f"fop:conv:{file_id}:.png"),
                Button.inline("🖼️ JPG", data=f"fop:conv:{file_id}:.jpg"),
            ],
            [
                Button.inline("🖼️ WEBP", data=f"fop:conv:{file_id}:.webp"),
                Button.inline("📄 PDF Document", data=f"fop:conv:{file_id}:.pdf"),
            ],
            [
                Button.inline("⚙️ Custom Extension", data=f"fop:custom:{file_id}"),
            ],
            [
                Button.inline("⬅️ Back", data=f"fop:back:{file_id}"),
                Button.inline("❌ Cancel", data=b"cancel_menu"),
            ],
        ]
    else:
        buttons = [
            [
                Button.inline("📄 PDF Document", data=f"fop:conv:{file_id}:.pdf"),
                Button.inline("📝 Plain Text (TXT)", data=f"fop:conv:{file_id}:.txt"),
            ],
            [
                Button.inline("📦 ZIP Archive", data=f"fop:conv:{file_id}:.zip"),
                Button.inline("⚙️ Custom Extension", data=f"fop:custom:{file_id}"),
            ],
            [
                Button.inline("⬅️ Back", data=f"fop:back:{file_id}"),
                Button.inline("❌ Cancel", data=b"cancel_menu"),
            ],
        ]

    msg_text = (
        f"🔄 **Convert File Format**\n\n"
        f"📄 File: `{file_info['original_name']}`\n"
        f"Current Format: `{ext or 'None'}`\n\n"
        f"Select a target format or enter a custom extension:"
    )
    await event.edit(msg_text, buttons=buttons)


@client.on(events.CallbackQuery(pattern=r"^fop:custom:(.+)$"))
async def file_custom_ext_callback(event):
    file_id = event.pattern_match.group(1).decode("utf-8")
    file_info = pending_file_ops.get(file_id)
    if not file_info:
        await event.edit("⚠️ **Session expired or file not found.** Please send or forward the file again.")
        return

    waiting_custom_ext_tasks[event.chat_id] = file_id
    waiting_rename_tasks.pop(event.chat_id, None)

    buttons = [
        [Button.inline("⬅️ Back", data=f"fop:fmt_menu:{file_id}")],
        [Button.inline("❌ Cancel", data=b"cancel_menu")],
    ]
    msg_text = (
        f"⚙️ **Custom File Format / Extension**\n\n"
        f"File: `{file_info['original_name']}`\n\n"
        f"Please send the **target extension** in your next message.\n"
        f"_(Example: `mp3`, `pdf`, `mp4`, `txt`, `docx`, `mkv`, `wav`, `zip`)_"
    )
    await event.edit(msg_text, buttons=buttons)


@client.on(events.CallbackQuery(pattern=r"^fop:back:(.+)$"))
async def file_back_callback(event):
    file_id = event.pattern_match.group(1).decode("utf-8")
    file_info = pending_file_ops.get(file_id)
    if not file_info:
        await event.edit("⚠️ **Session expired or file not found.** Please send or forward the file again.")
        return

    waiting_rename_tasks.pop(event.chat_id, None)
    waiting_custom_ext_tasks.pop(event.chat_id, None)

    ext = (file_info.get("ext") or "").lower()
    mime_type = (file_info.get("mime_type") or "").lower()
    is_video = mime_type.startswith("video") or ext in [".mp4", ".mkv", ".webm", ".avi", ".mov"]

    buttons = [
        [
            Button.inline("✏️ Rename File", data=f"fop:rename:{file_id}"),
            Button.inline("🔄 Change Format", data=f"fop:fmt_menu:{file_id}"),
        ],
    ]
    if is_video:
        buttons.append([
            Button.inline("🎵 Extract MP3", data=f"conv:mp3:{file_id}"),
            Button.inline("🎞️ Convert to GIF", data=f"conv:gif:{file_id}"),
        ])
    buttons.append([Button.inline("❌ Cancel", data=b"cancel_menu")])

    size_display = human_bytes(file_info['size']) if file_info.get('size') else "Unknown"
    format_display = ext.upper().lstrip(".") if ext else "File"
    prompt_text = (
        f"📁 **File Received:** `{file_info['original_name']}`\n"
        f"📊 **Size:** `{size_display}` | **Type:** `{format_display}`\n\n"
        f"Select what you would like to do with this file:"
    )
    await event.edit(prompt_text, buttons=buttons)


@client.on(events.CallbackQuery(pattern=r"^fop:conv:(.+):(.+)$"))
async def file_convert_direct_callback(event):
    file_id = event.pattern_match.group(1).decode("utf-8")
    target_ext = event.pattern_match.group(2).decode("utf-8")
    file_info = pending_file_ops.get(file_id)
    if not file_info:
        await event.edit("⚠️ **Session expired or file not found.** Please send or forward the file again.")
        return

    task_id = uuid.uuid4().hex[:8]
    t = asyncio.create_task(run_convert_file_job(event, file_info, target_ext, task_id))
    active_jobs[task_id] = {"task": t, "chat_id": event.chat_id}


async def run_rename_file_job(event, file_info: Dict[str, Any], new_name_raw: str, task_id: str):
    task_dir = create_task_dir(task_id)
    if task_id in active_jobs:
        active_jobs[task_id]["task_dir"] = task_dir

    clean_name = sanitize_filename(new_name_raw)
    orig_ext = (file_info.get("ext") or "").lower()
    if not Path(clean_name).suffix and orig_ext:
        clean_name = f"{clean_name}{orig_ext}"

    status_msg = await event.reply(
        f"⏳ **Renaming file to:** `{clean_name}`\nDownloading original file...",
        buttons=make_cancel_button(task_id)
    )

    try:
        orig_msg = await client.get_messages(file_info["chat_id"], ids=file_info["message_id"])
        if not orig_msg or not orig_msg.media:
            await status_msg.edit("❌ **Original file not found. It may have been deleted.**")
            return

        input_path = task_dir / clean_name
        dl_prog = TelegramProgress(status_msg, action="Downloading original file", buttons=make_cancel_button(task_id))
        await client.download_media(orig_msg.media, file=str(input_path), progress_callback=dl_prog.callback)

        if not input_path.exists() or input_path.stat().st_size == 0:
            await status_msg.edit("❌ **Failed to download original file.**")
            return

        up_prog = TelegramProgress(status_msg, action="Uploading renamed file", buttons=make_cancel_button(task_id))
        file_ext = input_path.suffix.lower()
        is_audio = file_ext in [".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"]
        is_video = file_ext in [".mp4", ".mkv", ".webm", ".avi", ".mov"]

        caption = f"✏️ **Renamed File:** `{clean_name}`\n📊 **Size:** `{human_bytes(input_path.stat().st_size)}`\n\nDownloaded with @AllDLBot"

        if is_audio:
            dur = get_audio_duration_seconds(input_path) if file_ext == ".mp3" else 0
            await client.send_file(
                file_info["chat_id"],
                input_path,
                caption=caption,
                attributes=[DocumentAttributeAudio(duration=dur, title=input_path.stem, performer="AllDLBot")],
                progress_callback=up_prog.callback,
            )
        elif is_video:
            await client.send_file(
                file_info["chat_id"],
                input_path,
                caption=caption,
                supports_streaming=True,
                progress_callback=up_prog.callback,
            )
        else:
            await client.send_file(
                file_info["chat_id"],
                input_path,
                caption=caption,
                force_document=True,
                progress_callback=up_prog.callback,
            )

        await status_msg.delete()
        pending_file_ops.pop(f"{file_info['chat_id']}_{file_info['message_id']}", None)

    except asyncio.CancelledError:
        logger.info(f"Rename job {task_id} cancelled by user.")
    except Exception as e:
        logger.error(f"Rename error: {e}")
        await status_msg.edit(f"❌ **Error while renaming:** {str(e)}")
    finally:
        cleanup_dir(task_dir)
        active_jobs.pop(task_id, None)


async def run_convert_file_job(event, file_info: Dict[str, Any], target_ext: str, task_id: str):
    task_dir = create_task_dir(task_id)
    if task_id in active_jobs:
        active_jobs[task_id]["task_dir"] = task_dir

    target_ext = ("." + target_ext.lstrip(".")).lower()
    orig_stem = Path(file_info.get("original_name", "file")).stem
    clean_target_name = f"{sanitize_filename(orig_stem)}{target_ext}"

    prompt = f"⏳ **Converting to {target_ext.upper()}...**\nDownloading original file..."
    if isinstance(event, events.CallbackQuery.Event):
        status_msg = await event.edit(prompt, buttons=make_cancel_button(task_id))
    else:
        status_msg = await event.reply(prompt, buttons=make_cancel_button(task_id))

    try:
        orig_msg = await client.get_messages(file_info["chat_id"], ids=file_info["message_id"])
        if not orig_msg or not orig_msg.media:
            await status_msg.edit("❌ **Original file not found. It may have been deleted.**")
            return

        input_path = task_dir / f"input_file{file_info.get('ext') or ''}"
        dl_prog = TelegramProgress(status_msg, action="Downloading original file", buttons=make_cancel_button(task_id))
        await client.download_media(orig_msg.media, file=str(input_path), progress_callback=dl_prog.callback)

        if not input_path.exists() or input_path.stat().st_size == 0:
            await status_msg.edit("❌ **Failed to download original file.**")
            return

        await status_msg.edit(
            f"🔄 **Converting to {target_ext.upper()}...**\nPlease wait, this may take a few moments.",
            buttons=make_cancel_button(task_id)
        )

        desired_output_path = task_dir / clean_target_name
        output_file = await asyncio.to_thread(convert_file_format, input_path, target_ext, desired_output_path)

        if not output_file or not output_file.exists() or output_file.stat().st_size == 0:
            await status_msg.edit(f"❌ **Conversion to {target_ext.upper()} failed.** Format may not be supported for this file type.")
            return

        up_prog = TelegramProgress(status_msg, action=f"Uploading converted {target_ext.upper()} file", buttons=make_cancel_button(task_id))
        caption = f"🔄 **Converted File:** `{output_file.name}`\n📦 **Format:** `{target_ext.upper()}` | **Size:** `{human_bytes(output_file.stat().st_size)}`\n\nDownloaded with @AllDLBot"

        is_audio = target_ext in [".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"]
        is_video = target_ext in [".mp4", ".mkv", ".webm", ".avi", ".mov"]

        if is_audio:
            dur = get_audio_duration_seconds(output_file) if target_ext == ".mp3" else 0
            await client.send_file(
                file_info["chat_id"],
                output_file,
                caption=caption,
                attributes=[DocumentAttributeAudio(duration=dur, title=output_file.stem, performer="AllDLBot")],
                progress_callback=up_prog.callback,
            )
        elif is_video:
            await client.send_file(
                file_info["chat_id"],
                output_file,
                caption=caption,
                supports_streaming=True,
                progress_callback=up_prog.callback,
            )
        else:
            await client.send_file(
                file_info["chat_id"],
                output_file,
                caption=caption,
                force_document=True,
                progress_callback=up_prog.callback,
            )

        await status_msg.delete()
        pending_file_ops.pop(f"{file_info['chat_id']}_{file_info['message_id']}", None)

    except asyncio.CancelledError:
        logger.info(f"Convert job {task_id} cancelled by user.")
    except Exception as e:
        logger.error(f"Convert error: {e}")
        await status_msg.edit(f"❌ **Error while converting:** {str(e)}")
    finally:
        cleanup_dir(task_dir)
        active_jobs.pop(task_id, None)


async def main():
    logger.info("Purging any previous leftover files in downloads...")
    purge_all_downloads()

    # Start periodic background garbage collector (sweeps any abandoned files every 5 mins)
    asyncio.create_task(periodic_cleaner_task())

    logger.info("Starting All-In-One Media Downloader Telegram Bot...")
    await client.start(bot_token=config.TELEGRAM_BOT_TOKEN)
    me = await client.get_me()
    logger.info(f"Bot connected successfully as @{me.username} ({me.first_name})")
    print(f"\n=========================================")
    print(f" ALL-IN-ONE DOWNLOADER BOT IS RUNNING!   ")
    print(f" Username: @{me.username}                ")
    print(f" Zero-cache policy: Active               ")
    print(f" Cancel button on all tasks: Active      ")
    print(f"=========================================\n")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())

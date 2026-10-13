import os
import uuid
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional

from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeAudio

import config
from utils.cleanup import (
    create_task_dir,
    cleanup_dir,
    get_file_size_mb,
    purge_all_downloads,
    periodic_cleaner_task,
)
from utils.progress import TelegramProgress, human_bytes
from utils.detector import extract_url, detect_platform
from utils.converter import video_to_mp3, video_to_gif, convert_video_quality, extract_thumbnail
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
)

# Setup Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("AllDLBot")

# In-memory storage for active download tasks and pending YouTube menus
pending_yt_tasks: Dict[str, Dict[str, Any]] = {}
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
        Button.inline("🔄 Media Converter", data=b"plat:converter"),
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
        "• 📌 **Pinterest**: Original High-Res Images & HD Video Pins\n"
        "• 🤖 **Reddit**: Videos with audio merged, Multi-Image Galleries, GIFs\n"
        "• ☁️ **SoundCloud**: 320kbps MP3 with 500x500 Cover Art & Tags\n"
        "• 📘 **Facebook**: High Quality Reels & Watch Videos\n"
        "• 🔄 **Media Converter**: Convert any video to MP3, GIF, or change resolution!\n\n"
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
        "• `tiktok.com/...` (Watermark-free video, audio, or slideshows)\n"
        "• `instagram.com/...` (Reels, posts, carousels with all media)\n"
        "• `x.com/...` or `twitter.com/...` (Videos, GIFs, images)\n"
        "• `open.spotify.com/...` (Tracks, albums, playlists)\n"
        "• `youtube.com/...` or `youtu.be/...` (Videos, Shorts, MP3)\n"
        "• `pinterest.com/...` or `pin.it/...` (Original photos & videos)\n"
        "• `reddit.com/...` or `redd.it/...` (Videos with sound & galleries)\n"
        "• `soundcloud.com/...` (320kbps MP3 with cover artwork)\n"
        "• `facebook.com/...` or `fb.watch/...` (Reels & Watch videos)\n\n"
        "👇 **Paste your link here now:**"
    )
    await event.respond(text, buttons=MAIN_KEYBOARD, parse_mode="md")


@client.on(events.NewMessage(pattern=r"(?i)^(🔄 Media Converter|converter|تبدیل)"))
async def converter_menu_button(event):
    text = (
        "🔄 **Media Converter & Video Tools**\n\n"
        "Send or forward **any video or audio file** directly into this chat.\n\n"
        "Once you send a file, interactive buttons will appear with options to:\n"
        "• 🎵 **Extract MP3 Audio** (High Quality 320kbps)\n"
        "• 🎞️ **Convert to Animated GIF** (High Quality 2-pass)\n"
        "• 🎬 **Convert Resolution** (720p HD, 480p SD, 360p Low)\n\n"
        "👇 **Send or forward your video/audio file now:**"
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
        "1️⃣ **Downloading Media:**\n"
        "   Just copy a link from TikTok, Instagram, Twitter, Spotify, or YouTube and send it in this chat.\n\n"
        "2️⃣ **YouTube Quality Selector:**\n"
        "   When you paste a YouTube link, buttons appear for `1080p`, `720p`, `480p`, `360p`, or `Audio MP3`. Tap your choice to download!\n\n"
        "3️⃣ **Instagram Multiple Media (Carousels):**\n"
        "   If a post contains multiple photos and videos, the bot downloads **ALL of them** and delivers them as an organized album!\n\n"
        "4️⃣ **Cancel Button on Tasks:**\n"
        "   Every active task has an inline `[❌ Cancel]` button so you can stop it anytime.\n\n"
        "5️⃣ **Converting Videos:**\n"
        "   Send any video directly to the bot and tap `Extract MP3` or `Convert to GIF`."
    )
    await event.respond(help_text, buttons=MAIN_KEYBOARD, parse_mode="md")


@client.on(events.CallbackQuery(pattern=r"^plat:(.+)$"))
async def platform_info_callback(event):
    plat = event.pattern_match.group(1).decode("utf-8")
    
    details = {
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
    await event.edit("❌ **Operation cancelled.**")


@client.on(events.NewMessage)
async def message_handler(event):
    # Ignore commands or menu button text triggers that are already handled
    text_content = event.raw_text.strip()
    if text_content.startswith("/") or text_content in [
        "📥 Download Media", "🔄 Media Converter", "ℹ️ Supported Platforms", "📖 Help & Instructions"
    ]:
        return

    # Check for direct media upload (for converter)
    if event.message.video or event.message.document:
        if event.message.video or (event.message.document and getattr(event.message.document, 'mime_type', '').startswith("video")):
            file_id = f"{event.chat_id}_{event.message.id}"
            buttons = [
                [
                    Button.inline("🎵 Extract MP3 Audio", data=f"conv:mp3:{file_id}"),
                    Button.inline("🎞️ Convert to GIF", data=f"conv:gif:{file_id}"),
                ],
                [
                    Button.inline("🎬 Convert to 720p", data=f"conv:720:{file_id}"),
                    Button.inline("🎬 Convert to 480p", data=f"conv:480:{file_id}"),
                ],
                [
                    Button.inline("❌ Cancel", data=b"cancel_menu"),
                ],
            ]
            await event.reply("🔄 **Select a conversion option for this video:**", buttons=buttons)
            return

    # Extract URL from message
    url = extract_url(event.raw_text)
    if not url:
        return

    platform, normalized_url = detect_platform(url)
    if not platform:
        return

    logger.info(f"Detected platform: {platform} for URL: {normalized_url}")

    if platform == "youtube":
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
            
            author = res.get("author", "Instagram")
            full_caption = f"📸 **Instagram by {author}**\n\n{caption}".strip()

            await status_msg.edit(f"📤 **Uploading {len(media_items)} media item(s)...**", buttons=make_cancel_button(task_id))
            file_paths = [item["path"] for item in media_items]

            if len(file_paths) == 1:
                progress = TelegramProgress(status_msg, action="Uploading", buttons=make_cancel_button(task_id))
                await client.send_file(
                    event.chat_id,
                    file_paths[0],
                    caption=full_caption,
                    supports_streaming=True,
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
                progress = TelegramProgress(status_msg, action="Uploading TikTok video", buttons=make_cancel_button(task_id))
                await client.send_file(
                    event.chat_id,
                    vid_file,
                    caption=caption,
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
                if item["type"] == "gif":
                    await client.send_file(
                        event.chat_id,
                        item["path"],
                        caption=full_caption,
                        progress_callback=progress.callback,
                    )
                else:
                    await client.send_file(
                        event.chat_id,
                        item["path"],
                        caption=full_caption,
                        supports_streaming=(item["type"] == "video"),
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
                    await client.send_file(
                        event.chat_id,
                        it["path"],
                        caption=c_text,
                        supports_streaming=(it["type"] == "video"),
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
                await client.send_file(
                    event.chat_id,
                    file_path,
                    caption=caption,
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
                progress = TelegramProgress(status_msg, action="Uploading", buttons=make_cancel_button(task_id))
                await client.send_file(
                    event.chat_id,
                    it["path"],
                    caption=caption,
                    supports_streaming=(it.get("type") == "video"),
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

            progress = TelegramProgress(status_msg, action="Uploading Facebook video", buttons=make_cancel_button(task_id))
            await client.send_file(
                event.chat_id,
                vid_path,
                caption=caption,
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

# 🚀 All-In-One Telegram Media Downloader Bot

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Telethon](https://img.shields.io/badge/library-Telethon-orange.svg)](https://github.com/LonamiWebs/Telethon)
[![FFmpeg](https://img.shields.io/badge/multimedia-FFmpeg-green.svg)](https://ffmpeg.org/)
[![Status](https://img.shields.io/badge/status-active-brightgreen.svg)]()

An advanced, asynchronous Telegram Bot built with Python and Telethon (MTProto) that downloads and converts media from **TikTok, Instagram, Twitter / X, Spotify, YouTube, Google Play Store (APKs), Pinterest, Reddit, SoundCloud, and Facebook** directly inside Telegram with **zero slash commands required**.

Designed with a **button-first interface**, **interactive cancellation on all tasks**, and a strict **zero-waste server storage policy** with no disk caching.

---

## 🌟 Key Features

### 🎵 1. TikTok
- **Watermark-Free HD Videos**: Direct high-definition download without watermarks.
- **Photo Slideshows**: Automatically extracts and uploads **all photos** in slideshow posts as an organized Telegram album.
- **Background Music**: Extracts the original audio track as an MP3 file with duration metadata.

### 📸 2. Instagram
- **Full Carousels (All Media)**: Automatically downloads **every photo and video** in multi-media carousel posts (sent in albums of up to 10 items).
- **Reels & Video Posts**: High quality video extraction.
- **Single Photos**: Full resolution download.
- Supports optional `INSTAGRAM_SESSIONID` for high-volume or private content.

### 🐦 3. Twitter / X
- **Videos & Clips**: Maximum available video resolution.
- **Animated GIFs**: Correctly identified and converted into native Telegram looping GIFs.
- **Single & Multi-Image Posts**: Downloads all images from multi-photo tweets into a Telegram album.
- Handles API fallbacks and suspended/deleted tweet notifications.

### 🎧 4. Spotify
- **Tracks, Albums & Playlists**: Fast oEmbed metadata resolution with high-fidelity audio extraction.
- **Full ID3 Metadata & Artwork**: Automatically embeds artist name, title, album, and high-resolution cover artwork into MP3 tags.
- Accurate track duration embedded to ensure seamless native Telegram player playback.

### 📺 5. YouTube & Shorts
- **Quality Selector Buttons**: Interactive inline buttons for **1080p Full HD**, **720p HD**, **480p SD**, and **360p Low**.
- **Audio Converter**: Direct 320kbps MP3 extraction with embedded video thumbnail.
- **GIF Converter**: Turn YouTube clips into animated GIFs.
- Works seamlessly on both regular videos and YouTube Shorts.

### 🏪 6. Google Play Store (APKs & XAPKs)
- **Interactive Architecture & RAM Selector**:
  - 📱 **ARM64 (arm64-v8a)**: Recommended for modern 64-bit phones (4GB, 6GB, 8GB+ RAM).
  - 📱 **ARMv7 (armeabi-v7a)**: 32-bit architecture for older devices or low-RAM devices (<4GB RAM).
  - 💻 **x86_64 / x86**: For PC Android emulators (BlueStacks, LDPlayer, Nox) & Chromebooks.
  - 🌐 **Universal**: Universal package compatible across all devices.
- **Rich Metadata & Cover Art**: Automatically pulls the official app title, developer, current version, star rating, category, and real app icon thumbnail.
- **Ready-To-Install Delivery**: Sends as a direct Telegram document for 1-tap installation on Android.

### 📌 7. Pinterest
- **Original Full-Resolution Images**: Bypasses downscaling to extract the raw maximum-resolution image files.
- **HD Video Pins**: High-definition MP4 downloads without watermarks.
- **GIFs & Idea Pins**: Animated and multi-media support.
- Supports both `pinterest.com` pins and `pin.it` shortlinks.

### 🤖 8. Reddit
- **Videos with Audio (Merged)**: Fixes Reddit's split audio/video stream issue by automatically muxing DASH video and audio using FFmpeg into a single high-quality MP4.
- **Multi-Image Galleries**: Downloads all images in gallery posts into a Telegram album.
- **Native GIFs & Single Images**: Supports `i.redd.it`, `v.redd.it`, and direct image links.

### ☁️ 9. SoundCloud
- **High Quality 320kbps MP3**: Downloads tracks and sets.
- **500x500 Cover Artwork**: Automatically embeds high-resolution album artwork into MP3 tags.
- **Full ID3 Metadata**: Artist, title, and track duration embedded for native Telegram audio playback.

### 📘 10. Facebook
- **Reels & Watch Videos**: Fast MP4 streaming video extraction.
- **Public Posts**: High definition 1080p/720p downloads.
- Supports `facebook.com` and `fb.watch` shortlinks.

### 🔄 11. Built-in Media Converter & Video Tools
- Send or forward **any video file** directly to the bot:
  - 🎵 **Extract MP3 Audio**: 320kbps high-quality audio extraction.
  - 🎞️ **Convert to GIF**: 2-pass palette-generated high-fidelity animated GIF.
  - 🎬 **Change Resolution**: Fast re-encoding to 720p, 480p, or 360p.

---

## 🕹️ Interactive Button-First Interface (No Slash Commands)

Users don't need to memorize or type slash commands:
- **Persistent Bottom Menu**:
  - `📥 Download Media`: Instant instructions and link prompt.
  - `🔄 Media Converter`: Video processing options.
  - `ℹ️ Supported Platforms`: Platform capability browser.
  - `📖 Help & Instructions`: Quick guide and tips.
- **Interactive Inline Buttons**: Platform information cards and quality selection menus appear automatically.

---

## ❌ Real-Time Task Cancellation

Every active operation features an inline `[❌ Cancel Download]` button:
- **During URL Lookup & Metadata Fetching**: Stop requests before downloading starts.
- **During Media Download & Conversion**: Abort long-running downloads or video conversions instantly.
- **During Telegram Upload**: Real-time upload progress (`TelegramProgress`) displays percent, speed, and ETA while keeping the Cancel button active.
- **Instant Cleanup**: Clicking Cancel terminates the background task and purges the temporary files immediately.

---

## 🧹 Zero-Waste Server Storage Policy

To prevent disk fill-ups and caching issues on low-cost VPS instances:
1. **Disabled `yt-dlp` Cache**: All downloaders strictly enforce `"cachedir": False, "no_cache_dir": True, "nopart": True`.
2. **Immediate Task Cleanup**: Temporary directories (`downloads/<task_id>/`) are deleted inside `finally:` blocks immediately upon delivery, failure, or cancellation.
3. **Startup Purge**: Wipes any orphaned files upon bot launch (`purge_all_downloads()`).
4. **Periodic Garbage Collector**: An asynchronous background cleaner (`periodic_cleaner_task()`) runs every 2 minutes, removing any abandoned files older than 120 seconds.

---

## 📁 Project Architecture

```
allDlbot/
├── .env.example             # Template environment variables
├── .gitignore               # Ignores secrets, sessions, caches, and downloads
├── requirements.txt         # Project dependencies
├── config.py                # Configuration and environment setup
├── bot.py                   # Main bot entry point, UI keyboards, handlers
├── downloaders/
│   ├── __init__.py
│   ├── youtube.py           # YouTube quality selector and downloader
│   ├── googleplay.py        # Google Play APK downloader with RAM/Arch selector
│   ├── instagram.py         # Instagram reels, carousels (all media items)
│   ├── tiktok.py            # TikTok HD video & photo slideshows
│   ├── twitter.py           # Twitter/X video, animated GIFs, multi-images
│   ├── spotify.py           # Spotify metadata resolver & 320kbps MP3
│   ├── pinterest.py         # Pinterest original images & HD video pins
│   ├── reddit.py            # Reddit video+audio muxer & galleries
│   ├── soundcloud.py        # SoundCloud 320kbps MP3 & 500x500 cover art
│   └── facebook.py          # Facebook reels & watch videos
├── utils/
│   ├── __init__.py
│   ├── converter.py         # FFmpeg video-to-audio, GIF, and resolution scaler
│   ├── detector.py          # URL parser and platform router (10 platforms)
│   ├── progress.py          # Throttled upload progress bar with cancel button
│   └── cleanup.py           # Directory lifecycle, disk purger, garbage collector
└── deploy/
    ├── deploy.py            # 1-click automated VPS deployer
    └── alldlbot.service     # Linux systemd background service
```

---

## ⚙️ Configuration (`.env`)

Create a `.env` file based on `.env.example`:

```ini
# Telegram API Credentials (from https://my.telegram.org)
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=your_api_hash_here

# Bot Token (from @BotFather)
TELEGRAM_BOT_TOKEN=123456789:ABCDEF_your_bot_token_here
TELEGRAM_DL_BOT_TOKEN=123456789:ABCDEF_your_bot_token_here

# Optional: Admin Chat ID
TELEGRAM_CHAT_ID=123456789

# Optional: Instagram session cookie for private or high-volume posts
INSTAGRAM_SESSIONID=

# Optional: Remote VPS credentials for automated deployment
IP_address=your_vps_ip
LOGIN=root
PASSWORD=your_vps_password
```

---

## 🚀 Installation & Running

### Prerequisites
- **Python 3.10+**
- **FFmpeg**: Must be installed and available in system `PATH`.
  - Windows: `winget install Gyan.FFmpeg` or download from [ffmpeg.org](https://ffmpeg.org/)
  - Linux (Ubuntu/Debian): `sudo apt update && sudo apt install -y ffmpeg`
  - macOS: `brew install ffmpeg`

### Run Locally

1. **Clone the repository:**
   ```bash
   git clone https://github.com/MuhammadFrz/allDlbot.git
   cd allDlbot
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env with your credentials
   ```

4. **Start the bot:**
   ```bash
   python bot.py
   ```

---

### ☁️ 1-Click Automated VPS Deployment

The project includes an automated deployment script that connects to your remote Ubuntu server via SSH, installs system dependencies (FFmpeg, Python, Pip), uploads files, configures a systemd service, and starts the bot 24/7:

```bash
python deploy/deploy.py
```

To view live service logs on your VPS:
```bash
journalctl -u alldlbot -f
```

To manage the service:
```bash
systemctl restart alldlbot
systemctl status alldlbot
```

---

## 📄 License

This project is licensed under the MIT License.

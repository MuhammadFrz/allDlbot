import re
from typing import Tuple, Optional

URL_REGEX = re.compile(r'(?:https?://|www\.|market://)[^\s<>"]+')

PLATFORM_PATTERNS = {
    "youtube": re.compile(r'(?:youtube\.com/(?:watch\?|shorts/|embed/|live/|v/|.*[?&]v=)|youtu\.be/|music\.youtube\.com/)', re.IGNORECASE),
    "instagram": re.compile(r'(?:instagram\.com/(?:p/|reel/|reels/|stories/|tv/|share/))', re.IGNORECASE),
    "tiktok": re.compile(r'(?:tiktok\.com/|vt\.tiktok\.com/|vm\.tiktok\.com/)', re.IGNORECASE),
    "twitter": re.compile(r'(?:(?:twitter\.com|x\.com)/[^/]+/status/|t\.co/)', re.IGNORECASE),
    "spotify": re.compile(r'(?:open\.spotify\.com/(?:track|album|playlist|artist|episode)/|spotify\.link/)', re.IGNORECASE),
    "pinterest": re.compile(r'(?:(?:[a-z]{2}\.)?pinterest\.(?:com|co\.[a-z]{2}|[a-z]{2})/pin/|pin\.it/)', re.IGNORECASE),
    "reddit": re.compile(r'(?:(?:(?:www|old|new)\.)?reddit\.com/(?:r/[^/]+/comments/|gallery/|user/[^/]+/comments/)|redd\.it/|v\.redd\.it/)', re.IGNORECASE),
    "soundcloud": re.compile(r'(?:soundcloud\.com/[^/]+/(?:[^/]+|sets/[^/]+)|on\.soundcloud\.com/)', re.IGNORECASE),
    "facebook": re.compile(r'(?:(?:www|m|web)\.)?(?:facebook\.com/(?:watch\?|reel/|reels/|.*[?&]v=|share/|videos/|story\.php|permalink\.php)|fb\.watch/|fb\.com/)', re.IGNORECASE),
    "googleplay": re.compile(r'(?:(?:play\.google\.com/store/apps/details\?|market://details\?)|(?:[a-z]{2,}\.)?google\.com/store/apps/details\?)', re.IGNORECASE),
}

def extract_url(text: str) -> Optional[str]:
    """Find the first URL in message text."""
    if not text:
        return None
    match = URL_REGEX.search(text)
    if match:
        url = match.group(0)
        # Clean trailing punctuation
        url = url.rstrip('.,;!?)]}')
        return url
    return None

def detect_platform(url: str) -> Tuple[Optional[str], str]:
    """
    Returns (platform_name, normalized_url).
    Platform can be 'youtube', 'instagram', 'tiktok', 'twitter', 'spotify', or None.
    """
    if not url:
        return None, ""
    
    # Normalize URL if missing schema
    if not url.startswith("http://") and not url.startswith("https://") and not url.startswith("market://"):
        url = "https://" + url

    for platform, pattern in PLATFORM_PATTERNS.items():
        if pattern.search(url):
            return platform, url
            
    # Generic direct HTTP/HTTPS link fallback
    if url.startswith("http://") or url.startswith("https://"):
        return "direct", url

    return None, url

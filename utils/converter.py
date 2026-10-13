import os
import subprocess
import json
from pathlib import Path
from typing import Optional, Tuple

def get_media_info(input_path: Path) -> dict:
    """Extract metadata using ffprobe."""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(input_path)
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(res.stdout)
    except Exception as e:
        print(f"[FFprobe error] {e}")
        return {}

def get_video_duration(input_path: Path) -> float:
    """Return duration of video in seconds."""
    info = get_media_info(input_path)
    try:
        return float(info.get("format", {}).get("duration", 0))
    except (ValueError, TypeError):
        return 0.0

def extract_thumbnail(video_path: Path, output_thumb: Optional[Path] = None) -> Optional[Path]:
    """Extract a frame from video as JPEG thumbnail."""
    if output_thumb is None:
        output_thumb = video_path.with_suffix(".thumb.jpg")
    
    cmd = [
        "ffmpeg", "-y",
        "-ss", "00:00:01",
        "-i", str(video_path),
        "-vframes", "1",
        "-q:v", "2",
        str(output_thumb)
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True)
        if output_thumb.exists():
            return output_thumb
    except Exception as e:
        print(f"[Extract Thumbnail Error] {e}")
    return None

def video_to_mp3(input_path: Path, output_path: Optional[Path] = None, bitrate: str = "320k") -> Optional[Path]:
    """Convert any video or audio file to MP3."""
    if output_path is None:
        output_path = input_path.with_suffix(".mp3")
        
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-vn",
        "-c:a", "libmp3lame",
        "-b:a", bitrate,
        str(output_path)
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True)
        if output_path.exists():
            return output_path
    except Exception as e:
        print(f"[Video to MP3 Error] {e}")
    return None

def video_to_gif(input_path: Path, output_path: Optional[Path] = None, max_duration: int = 15, scale: int = 480) -> Optional[Path]:
    """Convert video to high quality animated GIF using 2-pass palette generation."""
    if output_path is None:
        output_path = input_path.with_suffix(".gif")

    palette_path = input_path.with_name(f"{input_path.stem}_palette.png")
    
    # 2-pass filter with lanczos scaling
    vf_palette = f"fps=15,scale={scale}:-1:flags=lanczos,palettegen"
    vf_apply = f"fps=15,scale={scale}:-1:flags=lanczos[x];[x][1:v]paletteuse"
    
    try:
        # Step 1: Generate optimal palette
        subprocess.run([
            "ffmpeg", "-y",
            "-t", str(max_duration),
            "-i", str(input_path),
            "-vf", vf_palette,
            str(palette_path)
        ], capture_output=True, check=True)
        
        # Step 2: Render GIF using palette
        subprocess.run([
            "ffmpeg", "-y",
            "-t", str(max_duration),
            "-i", str(input_path),
            "-i", str(palette_path),
            "-filter_complex", vf_apply,
            "-loop", "0",
            str(output_path)
        ], capture_output=True, check=True)
        
        if palette_path.exists():
            palette_path.unlink()

        if output_path.exists():
            return output_path
    except Exception as e:
        print(f"[Video to GIF Error] {e}")
        if palette_path.exists():
            palette_path.unlink()
    return None

def convert_video_quality(input_path: Path, target_height: int = 720, output_path: Optional[Path] = None) -> Optional[Path]:
    """Re-encode video to specific vertical resolution (e.g. 720, 480, 360)."""
    if output_path is None:
        output_path = input_path.parent / f"{input_path.stem}_{target_height}p.mp4"

    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-vf", f"scale=-2:{target_height}",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        str(output_path)
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True)
        if output_path.exists():
            return output_path
    except Exception as e:
        print(f"[Convert Quality Error] {e}")
    return None

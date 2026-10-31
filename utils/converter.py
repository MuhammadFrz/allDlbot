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
        dur = float(info.get("format", {}).get("duration", 0))
        if dur > 0:
            return dur
    except (ValueError, TypeError):
        pass
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            try:
                dur = float(s.get("duration", 0))
                if dur > 0:
                    return dur
            except (ValueError, TypeError):
                pass
    return 0.0

def get_video_params(input_path: Path) -> Tuple[int, int, int]:
    """
    Return (duration_seconds, width, height) of video using ffprobe.
    Handles rotated videos properly (e.g. mobile portrait videos).
    """
    info = get_media_info(input_path)
    duration = 0
    width = 0
    height = 0
    try:
        format_info = info.get("format", {})
        duration = int(float(format_info.get("duration", 0)))
    except (ValueError, TypeError):
        pass

    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            if not duration:
                try:
                    duration = int(float(s.get("duration", 0)))
                except (ValueError, TypeError):
                    pass
            w = s.get("width")
            h = s.get("height")
            if w and h:
                # Check for rotation tag or side_data
                rotation = 0
                tags = s.get("tags") or {}
                if "rotate" in tags:
                    try:
                        rotation = int(tags["rotate"])
                    except (ValueError, TypeError):
                        pass
                else:
                    for side_data in s.get("side_data_list", []):
                        if "rotation" in side_data:
                            try:
                                rotation = int(side_data["rotation"])
                            except (ValueError, TypeError):
                                pass
                if abs(rotation) in (90, 270):
                    w, h = h, w
                width = int(w)
                height = int(h)
                break

    return duration, width, height

def extract_thumbnail(video_path: Path, output_thumb: Optional[Path] = None, timestamp: str = "00:00:01") -> Optional[Path]:
    """Extract a frame from video as JPEG thumbnail scaled for Telegram preview."""
    if output_thumb is None:
        output_thumb = video_path.with_suffix(".thumb.jpg")
    
    # Try preferred timestamp, then fallback to earlier positions for short clips
    for ss in [timestamp, "00:00:00.1", "00:00:00"]:
        cmd = [
            "ffmpeg", "-y",
            "-ss", ss,
            "-i", str(video_path),
            "-vframes", "1",
            "-vf", "scale=min(320\\,iw):-2",
            "-q:v", "2",
            str(output_thumb)
        ]
        try:
            res = subprocess.run(cmd, capture_output=True)
            if res.returncode == 0 and output_thumb.exists() and output_thumb.stat().st_size > 200:
                return output_thumb
        except Exception as e:
            print(f"[Extract Thumbnail Error on {ss}] {e}")

    return output_thumb if output_thumb.exists() and output_thumb.stat().st_size > 200 else None

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

def convert_file_format(input_path: Path, target_ext: str, output_path: Optional[Path] = None) -> Optional[Path]:
    """Convert any file to the specified target extension (e.g. mp4, mp3, mkv, webm, wav, png, jpg, pdf)."""
    import shutil
    from PIL import Image

    target_ext = ("." + target_ext.lstrip(".")).lower()
    if output_path is None:
        output_path = input_path.parent / f"{input_path.stem}{target_ext}"

    # If already the same extension, just return copy
    if input_path.suffix.lower() == target_ext:
        if input_path != output_path:
            shutil.copy2(input_path, output_path)
        return output_path

    # GIF conversion
    if target_ext == ".gif":
        return video_to_gif(input_path, output_path)

    # Audio conversions
    audio_codecs = {
        ".mp3": ["-vn", "-c:a", "libmp3lame", "-b:a", "320k"],
        ".m4a": ["-vn", "-c:a", "aac", "-b:a", "256k"],
        ".aac": ["-vn", "-c:a", "aac", "-b:a", "256k"],
        ".wav": ["-vn", "-c:a", "pcm_s16le"],
        ".flac": ["-vn", "-c:a", "flac"],
        ".ogg": ["-vn", "-c:a", "libvorbis", "-q:a", "6"],
        ".opus": ["-vn", "-c:a", "libopus", "-b:a", "192k"],
    }
    if target_ext in audio_codecs:
        cmd = ["ffmpeg", "-y", "-i", str(input_path)] + audio_codecs[target_ext] + [str(output_path)]
        try:
            subprocess.run(cmd, capture_output=True, check=True)
            if output_path.exists():
                return output_path
        except Exception as e:
            print(f"[Audio Convert Error] {e}")
        return None

    # Video conversions
    video_codecs = {
        ".mp4": ["-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart"],
        ".mkv": ["-c:v", "libx264", "-c:a", "aac"],
        ".webm": ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "30", "-c:a", "libopus"],
        ".avi": ["-c:v", "mpeg4", "-q:v", "3", "-c:a", "libmp3lame"],
        ".mov": ["-c:v", "libx264", "-c:a", "aac"],
    }
    if target_ext in video_codecs:
        cmd = ["ffmpeg", "-y", "-i", str(input_path)] + video_codecs[target_ext] + [str(output_path)]
        try:
            subprocess.run(cmd, capture_output=True, check=True)
            if output_path.exists():
                return output_path
        except Exception as e:
            print(f"[Video Convert Error] {e}")
        return None

    # Image conversions (Pillow)
    image_exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".pdf"}
    if target_ext in image_exts:
        try:
            img = Image.open(input_path)
            if target_ext in [".jpg", ".jpeg", ".pdf"] and img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGB")
            if target_ext == ".pdf":
                img.save(str(output_path), "PDF", resolution=100.0)
            else:
                img.save(str(output_path))
            if output_path.exists():
                return output_path
        except Exception as e:
            print(f"[Image Convert Error] {e}")

    # Fallback for documents or generic file extension changes: simple file copy with new extension
    try:
        shutil.copy2(input_path, output_path)
        if output_path.exists():
            return output_path
    except Exception as e:
        print(f"[File Copy/Rename Error] {e}")

    return None


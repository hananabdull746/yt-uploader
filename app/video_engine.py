from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

from app.integrations import IntegrationError

# Global mutex ensuring only 1 FFmpeg encode executes at any time on 1 GB VPS
_FFMPEG_LOCK = threading.Lock()


class VideoEngine:
    """Low-memory FFmpeg video processor for vertical 9:16 Shorts with kinetic subtitles."""

    @classmethod
    def get_ffmpeg_binary(cls) -> str:
        bin_path = shutil.which("ffmpeg")
        if not bin_path:
            raise IntegrationError("ffmpeg is not installed on this system. Install via 'sudo apt-get install ffmpeg'")
        return bin_path

    @classmethod
    def extract_audio(cls, video_path: str | Path, output_audio_path: str | Path) -> Path:
        """Extract lightweight MP3 audio from a video file for transcription."""
        ffmpeg = cls.get_ffmpeg_binary()
        out = Path(output_audio_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.unlink(missing_ok=True)

        cmd = [
            ffmpeg, "-y",
            "-threads", "2",
            "-i", str(video_path),
            "-vn",
            "-acodec", "libmp3lame",
            "-b:a", "64k",  # 64kbps is perfect and tiny for speech recognition
            "-ar", "16000",
            "-ac", "1",
            str(out),
        ]

        with _FFMPEG_LOCK:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode != 0 or not out.is_file():
                raise IntegrationError(f"Audio extraction failed: {res.stderr[-300:]}")
        return out

    @classmethod
    def render_short_clip(cls, input_video: str | Path, output_path: str | Path,
                          start_seconds: float, end_seconds: float,
                          layout: str = "blur", subtitles_ass_path: str | Path | None = None) -> Path:
        """Slices exactly start_seconds -> end_seconds and transforms into 9:16 vertical Short."""
        ffmpeg = cls.get_ffmpeg_binary()
        src = Path(input_video).resolve()
        out = Path(output_path).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.unlink(missing_ok=True)

        duration = max(1.0, end_seconds - start_seconds)

        # Build FFmpeg filtergraph based on layout choice
        sub_filter = ""
        if subtitles_ass_path:
            sub_file = Path(subtitles_ass_path).resolve()
            if sub_file.is_file():
                # Escape path for FFmpeg filter on Windows and Linux
                escaped_sub = str(sub_file).replace("\\", "/").replace(":", "\\:")
                sub_filter = f",subtitles='{escaped_sub}'"

        if layout == "crop":
            # Smart center 9:16 crop
            vf = f"crop=ih*9/16:ih:iw/2-(ih*9/16)/2:0,scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2{sub_filter}"
        else:
            # Blurred background stack (Industry standard for YouTube Shorts)
            # [0:v] split into bg (blurred 1080x1920) and fg (crisp 16:9 centered)
            vf = (
                "[0:v]split=2[bg_src][fg_src];"
                "[bg_src]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=22:5,eq=brightness=-0.12[bg];"
                "[fg_src]scale=1080:-1:force_original_aspect_ratio=decrease[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2{sub_filter}"
            )

        cmd = [
            ffmpeg, "-y",
            "-threads", "2",  # Restrict threads to keep RAM < 140MB
            "-ss", f"{start_seconds:.2f}",
            "-t", f"{duration:.2f}",
            "-i", str(src),
            "-filter_complex" if layout != "crop" else "-vf", vf,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "22",
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-movflags", "+faststart",
            str(out),
        ]

        with _FFMPEG_LOCK:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
                raise IntegrationError(f"Shorts video rendering failed: {res.stderr[-400:]}")

        return out

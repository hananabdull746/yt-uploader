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
    """Low-memory FFmpeg video processor for vertical 9:16 Shorts with face tracking and kinetic subtitles."""

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
    def detect_face_center_ratio(cls, video_path: str | Path, start_seconds: float, duration: float) -> float:
        """Samples frames across the clip window to locate the speaker's horizontal center (0.0 to 1.0).
        Downscales frames to 360p to keep memory usage under 60 MB."""
        try:
            import cv2
            cascade_cls = getattr(cv2, "CascadeClassifier", None)
            if not cascade_cls:
                return 0.5

            haarcascade_dir = getattr(cv2.data, "haarcascades", "")
            cascade_path = os.path.join(haarcascade_dir, "haarcascade_frontalface_default.xml")
            if not os.path.isfile(cascade_path):
                return 0.5

            cascade = cascade_cls(cascade_path)
            if cascade.empty():
                return 0.5

            cap = cv2.VideoCapture(str(video_path))
            if not cap.isOpened():
                return 0.5

            # Sample 5 frames across the clip duration
            sample_offsets = [start_seconds + (i * duration / 6.0) for i in range(1, 6)]
            detected_x: list[float] = []

            for offset in sample_offsets:
                cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, offset * 1000.0))
                ret, frame = cap.read()
                if not ret or frame is None:
                    continue

                h, w = frame.shape[:2]
                if h <= 0 or w <= 0:
                    continue

                # Downscale to 360p height for high-speed detection and ultra-low RAM
                scale = 360.0 / float(h)
                small_w = max(1, int(w * scale))
                small = cv2.resize(frame, (small_w, 360), interpolation=cv2.INTER_AREA)
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

                faces = cascade.detectMultiScale(gray, scaleFactor=1.2, minNeighbors=4, minSize=(30, 30))
                if len(faces) > 0:
                    # Pick largest face (main speaker closest to camera)
                    largest = max(faces, key=lambda f: f[2] * f[3])
                    fx, fy, fw, fh = largest
                    norm_center = (fx + (fw / 2.0)) / float(small_w)
                    detected_x.append(norm_center)

            cap.release()

            if detected_x:
                detected_x.sort()
                median_x = detected_x[len(detected_x) // 2]
                return float(max(0.18, min(0.82, median_x)))
        except Exception:
            pass

        return 0.5

    @classmethod
    def render_short_clip(cls, input_video: str | Path, output_path: str | Path,
                          start_seconds: float, end_seconds: float,
                          layout: str = "facetrack", subtitles_ass_path: str | Path | None = None) -> Path:
        """Slices exactly start_seconds -> end_seconds and transforms into 9:16 vertical Short."""
        ffmpeg = cls.get_ffmpeg_binary()
        src = Path(input_video).resolve()
        out = Path(output_path).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.unlink(missing_ok=True)

        duration = max(1.0, end_seconds - start_seconds)

        # Build FFmpeg filtergraph based on layout choice and subtitles
        sub_filter = ""
        if subtitles_ass_path:
            sub_file = Path(subtitles_ass_path).resolve()
            if sub_file.is_file():
                escaped_sub = str(sub_file).replace("\\", "/").replace(":", "\\:")
                sub_filter = f",subtitles='{escaped_sub}'"

        if layout == "facetrack":
            # Smart face-tracking reframing centered on speaker
            center_x = cls.detect_face_center_ratio(src, start_seconds, duration)
            vf = (
                f"crop=w=ih*9/16:h=ih:x='max(0,min(iw-ow,{center_x:.3f}*iw-ow/2))':y=0,"
                f"scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2{sub_filter}"
            )
        elif layout == "crop":
            # Static center 9:16 crop
            vf = f"crop=ih*9/16:ih:iw/2-(ih*9/16)/2:0,scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2{sub_filter}"
        else:
            # Blurred background stack
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
            "-filter_complex" if layout == "blur" else "-vf", vf,
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

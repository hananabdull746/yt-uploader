from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import requests

from app.integrations import IntegrationError


class DeepgramClient:
    """Client for Deepgram Nova-2 speech-to-text API with word-level timestamps."""

    def __init__(self, api_key: str, timeout: int = 60):
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.base_url = "https://api.deepgram.com/v1/listen"

    def transcribe_audio_file(self, audio_path: str | Path) -> dict[str, Any]:
        path = Path(audio_path)
        if not path.is_file() or path.stat().st_size == 0:
            raise IntegrationError("Audio file for transcription is missing or empty")

        headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": "audio/mp3",
        }
        params = {
            "model": "nova-2",
            "smart_format": "true",
            "punctuate": "true",
            "utterances": "true",
        }

        try:
            with open(path, "rb") as f:
                response = requests.post(
                    self.base_url,
                    headers=headers,
                    params=params,
                    data=f,
                    timeout=self.timeout,
                )
        except requests.RequestException as exc:
            raise IntegrationError(f"Deepgram transcription request failed: {exc}", retryable=True) from exc

        if response.status_code == 429:
            raise IntegrationError("Deepgram rate limit reached. Retry in a few minutes.", retryable=True)
        if response.status_code == 401 or response.status_code == 403:
            raise IntegrationError("Deepgram API key is invalid or unauthorized.")
        if not response.ok:
            raise IntegrationError(f"Deepgram returned HTTP {response.status_code}: {response.text[:300]}")

        try:
            data = response.json()
        except ValueError as exc:
            raise IntegrationError("Deepgram returned invalid JSON response", retryable=True) from exc

        channels = (data.get("results") or {}).get("channels") or []
        if not channels:
            raise IntegrationError("Deepgram returned no audio channels in response")

        alt = (channels[0].get("alternatives") or [{}])[0]
        full_transcript = alt.get("transcript") or ""
        raw_words = alt.get("words") or []

        words = []
        for w in raw_words:
            word_text = str(w.get("punctuated_word") or w.get("word") or "").strip()
            start = float(w.get("start") or 0.0)
            end = float(w.get("end") or start + 0.2)
            if word_text:
                words.append({
                    "word": word_text,
                    "start": round(start, 2),
                    "end": round(end, 2),
                })

        return {
            "transcript": full_transcript,
            "words": words,
            "duration": float((data.get("metadata") or {}).get("duration") or (words[-1]["end"] if words else 0.0)),
        }


class ViralHookDetector:
    """Finds high-retention 30-58s Shorts moments using Groq Llama 3.3 with Gemini Flash fallback.
    Divides transcripts into 3 parts to strictly prevent token/minute rate limits."""

    def __init__(self, groq_key: str = "", gemini_key: str = "", timeout: int = 30):
        self.groq_key = groq_key.strip()
        self.gemini_key = gemini_key.strip()
        self.timeout = timeout

    def split_into_three_parts(self, words: list[dict[str, Any]], total_duration: float) -> list[dict[str, Any]]:
        """Divides word-timestamped transcript into 3 chronological segments (0-33%, 33-66%, 66-100%)."""
        if not words:
            return []

        part_len = max(total_duration / 3.0, 30.0)
        p1_end = part_len
        p2_end = part_len * 2.0

        p1_words = [w for w in words if w["start"] < p1_end]
        p2_words = [w for w in words if p1_end <= w["start"] < p2_end]
        p3_words = [w for w in words if w["start"] >= p2_end]

        parts = []
        for idx, (label, w_list) in enumerate([
            ("Part 1 (Beginning / Hook)", p1_words),
            ("Part 2 (Middle / Peak)", p2_words),
            ("Part 3 (Climax / Takeaway)", p3_words),
        ], start=1):
            if w_list:
                text_slice = " ".join(f"[{w['start']}s]{w['word']}" for w in w_list)
                parts.append({
                    "part_num": idx,
                    "label": label,
                    "start": w_list[0]["start"],
                    "end": w_list[-1]["end"],
                    "formatted_text": text_slice[:4000],  # Bound tokens to avoid TPM limits
                    "words": w_list,
                })
        return parts

    def detect_best_short(self, words: list[dict[str, Any]], total_duration: float, video_title: str = "") -> dict[str, Any]:
        if not words:
            raise IntegrationError("Cannot detect Shorts hooks from empty transcript words")

        # 1. Divide into 3 parts
        parts = self.split_into_three_parts(words, total_duration)
        if not parts:
            raise IntegrationError("Transcript could not be segmented into parts")

        candidates: list[dict[str, Any]] = []

        # 2. Evaluate each of the 3 parts with Groq (or fallback to Gemini)
        for part in parts:
            try:
                candidate = self._evaluate_segment(part, video_title)
                if candidate and candidate.get("duration", 0) >= 25:
                    candidates.append(candidate)
            except Exception as err:
                # Log or keep trying other parts
                continue

        if not candidates:
            # Fallback heuristic: choose best available 45-second window from first half
            start_sec = words[0]["start"]
            target_end = min(start_sec + 45.0, words[-1]["end"])
            # snap to nearest word
            matching = [w for w in words if w["start"] <= target_end]
            end_sec = matching[-1]["end"] if matching else target_end
            return {
                "start": start_sec,
                "end": end_sec,
                "duration": round(end_sec - start_sec, 1),
                "title": f"{video_title[:50]} #Shorts",
                "description": f"Highlight clip from: {video_title}\n\n#Shorts #Viral",
                "hook_reason": "Heuristic selection: opening high-engagement segment",
                "virality_score": 75,
            }

        # Select highest virality score
        candidates.sort(key=lambda c: c.get("virality_score", 0), reverse=True)
        return candidates[0]

    def _evaluate_segment(self, part: dict[str, Any], video_title: str) -> dict[str, Any]:
        prompt = (
            f"You are a viral YouTube Shorts editor. Analyze this timestamped transcript segment from video '{video_title}'.\n"
            f"Segment: {part['label']} (From {part['start']}s to {part['end']}s).\n"
            f"Transcript with timestamps:\n{part['formatted_text']}\n\n"
            "Task: Identify the single best, most viral 30 to 58 second clip.\n"
            "Requirements:\n"
            "1. Duration (end_seconds - start_seconds) MUST be between 30 and 58 seconds.\n"
            "2. Start and end timestamps MUST match real timestamp markers in the transcript.\n"
            "3. MUST begin on an attention-grabbing sentence and end on a punchline, revelation, or complete thought (NO cutting mid-sentence).\n"
            "4. Generate a high-CTR title (under 60 chars) ending with #Shorts.\n"
            "5. Respond ONLY with valid JSON in this schema:\n"
            "{\n"
            '  "start_seconds": float,\n'
            '  "end_seconds": float,\n'
            '  "title": "Catchy Title #Shorts",\n'
            '  "description": "Short description #Shorts #Viral",\n'
            '  "hook_reason": "Why this moment is viral",\n'
            '  "virality_score": 1-100\n'
            "}"
        )

        # Try Groq first if key is configured
        if self.groq_key:
            try:
                res = self._call_groq(prompt)
                if res:
                    return res
            except Exception:
                # Fall through to Gemini fallback
                pass

        # Try Gemini fallback if configured
        if self.gemini_key:
            return self._call_gemini(prompt)

        raise IntegrationError("Neither Groq nor Gemini API key is configured for AI Shorts hook detection")

    def _call_groq(self, prompt: str) -> dict[str, Any]:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.groq_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [
                {"role": "system", "content": "You are a professional video hook analyzer that returns only strict JSON."},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.3,
            "max_tokens": 500,
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        if resp.status_code == 429:
            raise IntegrationError("Groq TPM rate limit reached, falling back to Gemini", retryable=True)
        if not resp.ok:
            raise IntegrationError(f"Groq API returned HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return self._sanitize_candidate(parsed)

    def _call_gemini(self, prompt: str) -> dict[str, Any]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={self.gemini_key}"
        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "generationConfig": {
                "temperature": 0.3,
                "response_mime_type": "application/json",
            }
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        if not resp.ok:
            raise IntegrationError(f"Gemini API returned HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise IntegrationError("Gemini returned empty candidate response")
        text = candidates[0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        return self._sanitize_candidate(parsed)

    def _sanitize_candidate(self, parsed: dict[str, Any]) -> dict[str, Any]:
        start = float(parsed.get("start_seconds") or 0.0)
        end = float(parsed.get("end_seconds") or start + 45.0)
        dur = end - start
        if dur > 59.0:
            end = start + 58.0
        elif dur < 25.0:
            end = start + 35.0

        title = str(parsed.get("title") or "Viral Moment #Shorts").strip()
        if not title.endswith("#Shorts") and "#shorts" not in title.lower():
            title = f"{title} #Shorts"

        return {
            "start": round(start, 2),
            "end": round(end, 2),
            "duration": round(end - start, 1),
            "title": title[:95],
            "description": str(parsed.get("description") or f"{title}\n\n#Shorts #Viral"),
            "hook_reason": str(parsed.get("hook_reason") or "High audience retention moment"),
            "virality_score": int(parsed.get("virality_score") or 80),
        }


class KineticSubtitleGenerator:
    """Generates ASS subtitles with Hormozi-style active word pop animations and viral typography."""

    STYLES = {
        "hormozi_yellow": {
            "active_color": "&H0000E6FF&",  # ASS BGR: Electric Yellow #FFE600
            "inactive_color": "&H00FFFFFF&", # White
            "outline_color": "&H00000000&",  # Pure Black
        },
        "viral_mint": {
            "active_color": "&H0081B910&",  # ASS BGR: Mint Green #10B981
            "inactive_color": "&H00FFFFFF&",
            "outline_color": "&H00051B12&",
        },
        "neon_cyan": {
            "active_color": "&H00FFFF00&",  # ASS BGR: Electric Cyan #00FFFF
            "inactive_color": "&H00FFFFFF&",
            "outline_color": "&H00000000&",
        },
    }

    @classmethod
    def generate_ass(cls, words: list[dict[str, Any]], clip_start: float, clip_end: float,
                      output_path: str | Path, font_name: str = "Montserrat",
                      style_name: str = "hormozi_yellow") -> Path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        palette = cls.STYLES.get(style_name, cls.STYLES["hormozi_yellow"])
        active_col = palette["active_color"]
        inactive_col = palette["inactive_color"]
        outline_col = palette["outline_color"]

        # Filter words within the clip range
        clip_words = [
            w for w in words
            if w["start"] >= (clip_start - 0.2) and w["end"] <= (clip_end + 0.5)
        ]

        # Shift timestamps relative to clip start
        shifted = []
        for w in clip_words:
            shifted.append({
                "word": w["word"].upper(),  # Viral uppercase captions
                "start": max(0.0, w["start"] - clip_start),
                "end": max(0.1, w["end"] - clip_start),
            })

        # Group words into chunks of 3-4 words for dynamic reading rhythm
        chunks = []
        chunk_size = 3
        for i in range(0, len(shifted), chunk_size):
            group = shifted[i:i + chunk_size]
            if group:
                chunks.append(group)

        # Build ASS script
        ass_lines = [
            "[Script Info]",
            "ScriptType: v4.00+",
            "PlayResX: 1080",
            "PlayResY: 1920",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            f"Style: Kinetic,{font_name},52,{inactive_col},&H000000FF&,{outline_col},&H80000000&,-1,0,0,0,100,100,2,0,1,5.0,2.0,2,80,80,380,1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]

        def fmt_time(sec: float) -> str:
            m = int(sec // 60)
            s = int(sec % 60)
            cs = int(round((sec - int(sec)) * 100))
            return f"{m:01d}:{s:02d}.{cs:02d}"

        # Generate each dialogue line where active word pops up in scale and color
        for group in chunks:
            for active_idx, active_word in enumerate(group):
                start_str = fmt_time(active_word["start"])
                end_str = fmt_time(active_word["end"])

                line_parts = []
                for idx, w in enumerate(group):
                    if idx == active_idx:
                        # Animated pop/zoom on active word
                        line_parts.append(r"{\c" + active_col + r"\t(0,60,\fscx112\fscy112)}" + w["word"] + r"{\r}")
                    else:
                        line_parts.append(r"{\c" + inactive_col + r"\fscx100\fscy100}" + w["word"] + r"{\r}")

                dialogue_text = " ".join(line_parts)
                ass_lines.append(f"Dialogue: 0,{start_str},{end_str},Kinetic,,0,0,0,,{dialogue_text}")

        out.write_text("\n".join(ass_lines), encoding="utf-8")
        return out

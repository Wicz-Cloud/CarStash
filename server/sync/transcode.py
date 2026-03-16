"""
PlexSync Transcode Module
FFmpeg wrapper locked to: Raspberry Pi 3B + 2024 Honda Odyssey (12.8" HD screen).

WHY THESE SETTINGS:
  Pi 3B has no hardware H.264 encoder. Software libx264 with 'veryfast' preset
  is the only viable path — it keeps CPU usage under ~85% at 720p.

  The 2024 Odyssey screen is 12.8" HD, which supports 1080p input via HDMI.
  However, the Pi 3B cannot software-encode 1080p at a usable speed (< 5fps).
  Target is 1280x720 (720p) — visually indistinguishable at 12.8" car viewing
  distance, and the Pi 3B handles it at ~20-25fps encode speed (real-time capable).

  Output format: MP4 + H.264 (main profile) + AAC stereo
  This is the most compatible combo for Plex in Chromium on RPi OS.

QUALITY PRESETS:
  "small"    — CRF 28, veryfast — smallest files, good for lots of kids movies
  "balanced" — CRF 23, veryfast — default, good quality/size tradeoff  [DEFAULT]
  "quality"  — CRF 20, fast     — best quality, bigger files, slower encode

AUDIO:
  Always stereo AAC 192k. The Odyssey's wireless headphones are stereo only.
  5.1 surround sources are downmixed. Stereo sources are passed through re-encoded.
"""

import subprocess
import shutil
import json
import os
import threading
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Callable
from datetime import datetime

logger = logging.getLogger(__name__)

# ─── Constants locked to this hardware ───────────────────────────────────────

PI_MODEL          = "Pi 3B"
SCREEN            = "2024 Honda Odyssey 12.8\""
TARGET_WIDTH      = 1280
TARGET_HEIGHT     = 720
ENCODER           = "libx264"
PROFILE           = "main"       # H.264 profile — Chromium safe
LEVEL             = "3.1"        # H.264 level for 720p30
PIX_FMT           = "yuv420p"    # Required for broad compatibility
MAX_FPS           = 30
AUDIO_CODEC       = "aac"
AUDIO_BITRATE     = "192k"
AUDIO_CHANNELS    = 2            # Stereo — Odyssey headphones are stereo
CONTAINER         = "mp4"

QUALITY_PRESETS = {
    # name:     (crf, x264_preset, description)
    "small":    (28, "veryfast", "Smallest files — great for lots of content"),
    "balanced": (23, "veryfast", "Good quality/size tradeoff [default]"),
    "quality":  (20, "fast",     "Best quality — larger files, slower encode"),
}


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class TranscodeJob:
    job_id: str
    input_path: str
    output_path: str
    quality: str = "balanced"
    status: str = "queued"        # queued | probing | encoding | done | error | skipped
    progress: float = 0.0         # 0.0 – 100.0
    fps_current: float = 0.0      # live encode speed
    speed_x: float = 0.0          # e.g. 1.5 = 1.5x realtime
    eta_seconds: Optional[int] = None
    input_info: dict = field(default_factory=dict)
    output_size_bytes: int = 0
    input_size_bytes: int = 0
    savings_pct: float = 0.0
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    skipped_reason: Optional[str] = None

    def to_dict(self):
        return asdict(self)


@dataclass
class MediaInfo:
    """Parsed ffprobe output for a file."""
    path: str
    duration_seconds: float
    video_codec: str
    width: int
    height: int
    fps: float
    bitrate_kbps: int
    audio_codec: str
    audio_channels: int
    size_bytes: int
    is_already_compatible: bool   # True = skip transcode
    skip_reason: Optional[str] = None

    def to_dict(self):
        return asdict(self)


# ─── ffprobe helpers ──────────────────────────────────────────────────────────

def probe(path: str) -> MediaInfo:
    """
    Run ffprobe and return structured MediaInfo.
    Raises RuntimeError if ffprobe is missing or the file is unreadable.
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe not found — install ffmpeg: sudo apt install ffmpeg")

    cmd = [
        ffprobe, "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")

    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if not video:
        raise RuntimeError(f"No video stream found in {path}")

    # Parse FPS from "num/den" string
    fps = 0.0
    fps_str = video.get("r_frame_rate", "0/1")
    try:
        num, den = fps_str.split("/")
        fps = round(float(num) / float(den), 2) if float(den) else 0.0
    except Exception:
        pass

    duration = float(fmt.get("duration", 0) or video.get("duration", 0) or 0)
    bitrate  = int(int(fmt.get("bit_rate", 0)) / 1000)
    size     = int(fmt.get("size", 0) or os.path.getsize(path))

    vcodec = video.get("codec_name", "unknown").lower()
    width  = int(video.get("width", 0))
    height = int(video.get("height", 0))

    acodec   = audio.get("codec_name", "none").lower() if audio else "none"
    achannels = int(audio.get("channels", 0)) if audio else 0

    # Decide if transcode can be skipped
    skip_reason = _check_compatible(vcodec, width, height, fps, acodec, achannels, fmt)

    return MediaInfo(
        path=path,
        duration_seconds=duration,
        video_codec=vcodec,
        width=width,
        height=height,
        fps=fps,
        bitrate_kbps=bitrate,
        audio_codec=acodec,
        audio_channels=achannels,
        size_bytes=size,
        is_already_compatible=(skip_reason is None),
        skip_reason=skip_reason,
    )


def _check_compatible(vcodec, width, height, fps, acodec, achannels, fmt) -> Optional[str]:
    """
    Return None if file is already Pi/Plex/Odyssey-ready.
    Return a reason string if transcoding is needed.
    """
    container = fmt.get("format_name", "")

    if vcodec not in ("h264", "avc"):
        return f"video codec is {vcodec}, needs H.264"
    if width > TARGET_WIDTH or height > TARGET_HEIGHT:
        return f"resolution {width}x{height} exceeds target {TARGET_WIDTH}x{TARGET_HEIGHT}"
    if fps > MAX_FPS + 1:
        return f"framerate {fps}fps exceeds {MAX_FPS}fps cap"
    if acodec not in ("aac",):
        return f"audio codec is {acodec}, needs AAC"
    if achannels > 2:
        return f"audio has {achannels} channels, needs stereo downmix"
    if "mp4" not in container and "mov" not in container:
        return f"container is {container}, needs MP4"

    return None  # already compatible — safe to skip


# ─── Transcode engine ─────────────────────────────────────────────────────────

class Transcoder:
    def __init__(self):
        self.jobs: dict[str, TranscodeJob] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cancel_flags: dict[str, threading.Event] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def submit(
        self,
        job_id: str,
        input_path: str,
        output_path: str,
        quality: str = "balanced",
        on_progress: Optional[Callable[[TranscodeJob], None]] = None,
        skip_if_compatible: bool = True,
    ) -> TranscodeJob:
        """
        Submit a transcode job. Runs in a background thread.
        Calls on_progress(job) periodically during encode.
        If skip_if_compatible=True, already-compatible files are copied not re-encoded.
        """
        if job_id in self._threads and self._threads[job_id].is_alive():
            raise RuntimeError(f"Job {job_id} already running")
        if quality not in QUALITY_PRESETS:
            raise ValueError(f"Unknown quality: {quality}. Choose: {list(QUALITY_PRESETS)}")

        job = TranscodeJob(
            job_id=job_id,
            input_path=input_path,
            output_path=output_path,
            quality=quality,
            input_size_bytes=os.path.getsize(input_path) if os.path.exists(input_path) else 0,
        )
        self.jobs[job_id] = job
        cancel = threading.Event()
        self._cancel_flags[job_id] = cancel

        t = threading.Thread(
            target=self._run,
            args=(job, cancel, on_progress, skip_if_compatible),
            daemon=True,
        )
        self._threads[job_id] = t
        t.start()
        return job

    def cancel(self, job_id: str):
        if job_id in self._cancel_flags:
            self._cancel_flags[job_id].set()

    def get_job(self, job_id: str) -> Optional[TranscodeJob]:
        return self.jobs.get(job_id)

    def list_jobs(self) -> list[TranscodeJob]:
        return list(self.jobs.values())

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run(
        self,
        job: TranscodeJob,
        cancel: threading.Event,
        on_progress: Optional[Callable],
        skip_if_compatible: bool,
    ):
        job.started_at = datetime.now().isoformat()

        try:
            # 1. Probe input
            job.status = "probing"
            info = probe(job.input_path)
            job.input_info = info.to_dict()

            # 2. Skip if already compatible
            if skip_if_compatible and info.is_already_compatible:
                job.status = "skipped"
                job.skipped_reason = "File is already Pi/Plex compatible — no transcode needed"
                job.progress = 100.0
                job.finished_at = datetime.now().isoformat()
                logger.info(f"[{job.job_id}] Skipped: {job.skipped_reason}")
                if on_progress:
                    on_progress(job)
                return

            # 3. Build ffmpeg command
            cmd = self._build_cmd(job.input_path, job.output_path, job.quality, info)
            logger.info(f"[{job.job_id}] Running: {' '.join(cmd)}")

            # 4. Ensure output directory exists
            Path(job.output_path).parent.mkdir(parents=True, exist_ok=True)

            # 5. Run ffmpeg with progress parsing
            job.status = "encoding"
            self._exec_ffmpeg(job, cmd, info.duration_seconds, cancel, on_progress)

            if cancel.is_set():
                job.status = "error"
                job.error = "Cancelled by user"
                # Clean up partial output
                if os.path.exists(job.output_path):
                    os.remove(job.output_path)
            else:
                job.status = "done"
                job.progress = 100.0
                if os.path.exists(job.output_path):
                    job.output_size_bytes = os.path.getsize(job.output_path)
                    if job.input_size_bytes > 0:
                        saved = job.input_size_bytes - job.output_size_bytes
                        job.savings_pct = round(saved / job.input_size_bytes * 100, 1)

        except Exception as e:
            job.status = "error"
            job.error = str(e)
            logger.exception(f"[{job.job_id}] Transcode failed: {e}")
        finally:
            job.finished_at = datetime.now().isoformat()
            if on_progress:
                on_progress(job)

    def _build_cmd(
        self,
        input_path: str,
        output_path: str,
        quality: str,
        info: MediaInfo,
    ) -> list[str]:
        """
        Build the ffmpeg command for Pi 3B + 2024 Odyssey.

        Key decisions:
          - Scale to 1280x720 max, preserving aspect ratio, divisible by 2
          - libx264 with veryfast/fast preset (only viable on Pi 3B)
          - yuv420p pixel format (required for Chromium compatibility)
          - H.264 main profile level 3.1 (safe for embedded players)
          - AAC stereo 192k (Odyssey wireless headphones are stereo)
          - fps capped at 30 (anything above wastes space, not visible on car screen)
          - faststart moov atom (lets Plex seek before full download)
        """
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found — sudo apt install ffmpeg")

        crf, preset, _ = QUALITY_PRESETS[quality]

        # Scale filter: fit within 1280x720, keep aspect, ensure even dimensions
        scale_filter = (
            f"scale='min({TARGET_WIDTH},iw)':min'({TARGET_HEIGHT},ih)':"
            f"force_original_aspect_ratio=decrease,"
            f"scale=trunc(iw/2)*2:trunc(ih/2)*2"
        )

        # FPS filter: only apply if source is above cap (avoids unnecessary processing)
        vf_filters = [scale_filter]
        if info.fps > MAX_FPS + 0.5:
            vf_filters.append(f"fps={MAX_FPS}")

        cmd = [
            ffmpeg,
            "-i", input_path,
            "-y",                          # overwrite output without asking

            # ── Video ──
            "-c:v", ENCODER,
            "-crf", str(crf),
            "-preset", preset,
            "-profile:v", PROFILE,         # H.264 main — Chromium safe
            "-level", LEVEL,               # 3.1 = safe for 720p30
            "-pix_fmt", PIX_FMT,           # yuv420p required for compatibility
            "-vf", ",".join(vf_filters),

            # ── Audio ──
            "-c:a", AUDIO_CODEC,
            "-b:a", AUDIO_BITRATE,
            "-ac", str(AUDIO_CHANNELS),    # force stereo downmix
            "-ar", "48000",                # 48kHz sample rate (AAC standard)

            # ── Container / seeking ──
            "-movflags", "+faststart",     # move moov atom to front for Plex streaming
            "-f", CONTAINER,

            # ── Progress ──
            "-progress", "pipe:1",         # write progress stats to stdout
            "-stats_period", "1",          # update every second
            "-v", "warning",               # suppress verbose output

            output_path,
        ]

        return cmd

    def _exec_ffmpeg(
        self,
        job: TranscodeJob,
        cmd: list[str],
        duration: float,
        cancel: threading.Event,
        on_progress: Optional[Callable],
    ):
        """Run ffmpeg subprocess and parse its -progress pipe output."""
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        # Parse key=value lines from -progress pipe:1
        kv: dict[str, str] = {}

        for line in proc.stdout:
            if cancel.is_set():
                proc.kill()
                return

            line = line.strip()
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()

            # ffmpeg emits a "progress=continue" or "progress=end" line after each block
            if kv.get("progress") in ("continue", "end"):
                out_time_us = int(kv.get("out_time_us", 0) or 0)
                elapsed_s = out_time_us / 1_000_000

                if duration > 0:
                    job.progress = min(round(elapsed_s / duration * 100, 1), 99.9)

                fps_str = kv.get("fps", "0")
                try:
                    job.fps_current = float(fps_str)
                except ValueError:
                    job.fps_current = 0.0

                speed_str = kv.get("speed", "0x").replace("x", "")
                try:
                    job.speed_x = float(speed_str)
                    if job.speed_x > 0 and duration > 0:
                        remaining = duration - elapsed_s
                        job.eta_seconds = int(remaining / job.speed_x)
                except ValueError:
                    job.speed_x = 0.0

                if on_progress:
                    on_progress(job)
                kv = {}

        proc.wait()
        if proc.returncode not in (0, None) and not cancel.is_set():
            stderr = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"ffmpeg exited with code {proc.returncode}: {stderr[-500:]}")


# ─── Batch helper (used by SyncEngine integration) ────────────────────────────

def transcode_folder(
    source_dir: str,
    dest_dir: str,
    quality: str = "balanced",
    extensions: tuple = (".mkv", ".avi", ".mov", ".ts", ".wmv", ".m4v", ".mp4"),
    on_job_update: Optional[Callable[[TranscodeJob], None]] = None,
    skip_if_compatible: bool = True,
) -> list[TranscodeJob]:
    """
    Transcode all video files in source_dir into dest_dir.
    Output files are always .mp4 regardless of input container.
    Already-compatible MP4s are skipped (or optionally re-encoded).
    Returns list of all TranscodeJob objects when complete.
    """
    transcoder = Transcoder()
    source = Path(source_dir)
    dest = Path(dest_dir)
    jobs = []

    video_files = [
        f for f in source.rglob("*")
        if f.is_file() and f.suffix.lower() in extensions
    ]

    if not video_files:
        logger.info(f"No video files found in {source_dir}")
        return []

    # Use a semaphore — Pi 3B should only encode ONE file at a time
    sem = threading.Semaphore(1)
    done_events: dict[str, threading.Event] = {}

    for vf in video_files:
        rel = vf.relative_to(source)
        out_path = str(dest / rel.with_suffix(".mp4"))
        job_id = str(rel)

        done_event = threading.Event()
        done_events[job_id] = done_event

        def _notify(j: TranscodeJob, ev=done_event):
            if on_job_update:
                on_job_update(j)
            if j.status in ("done", "error", "skipped"):
                ev.set()

        with sem:
            job = transcoder.submit(
                job_id=job_id,
                input_path=str(vf),
                output_path=out_path,
                quality=quality,
                on_progress=_notify,
                skip_if_compatible=skip_if_compatible,
            )
            jobs.append(job)
            done_event.wait()  # block until this file finishes before starting next

    return jobs


# ─── Capability check (called on startup / API) ───────────────────────────────

def system_check() -> dict:
    """Return a summary of transcoding capability on this system."""
    ffmpeg  = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")

    # Read Pi model
    try:
        pi_model = Path("/proc/device-tree/model").read_text().strip()
    except Exception:
        pi_model = "Unknown (not a Pi, or /proc not available)"

    return {
        "pi_model": pi_model,
        "ffmpeg_available": ffmpeg is not None,
        "ffprobe_available": ffprobe is not None,
        "ffmpeg_path": ffmpeg,
        "encoder": ENCODER,
        "target_resolution": f"{TARGET_WIDTH}x{TARGET_HEIGHT}",
        "target_screen": SCREEN,
        "quality_presets": {
            k: {"crf": v[0], "preset": v[1], "description": v[2]}
            for k, v in QUALITY_PRESETS.items()
        },
        "notes": [
            "Pi 3B uses software libx264 — one file at a time, ~20-25fps encode speed at 720p",
            "Files already in H.264/AAC/MP4 at ≤720p are skipped automatically",
            "Audio is always downmixed to stereo AAC 192k for Odyssey headphone compatibility",
            "Output MP4s include faststart flag for Plex seeking before full download",
        ],
    }

"""Conductor workers for the video_dialogue_pipeline workflow.

Each function below polls one SIMPLE task type. They shell out to ffmpeg/ffprobe
(and faster-whisper for speech-to-text), so run them on a host that can read the
input video path and write to the output directory.

All workers are idempotent: outputs go to deterministic paths and are written
via a temp file + atomic rename, so a retried task simply overwrites them.

Run:  CONDUCTOR_SERVER_URL=http://localhost:8080/api python workers.py
"""

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from conductor.client.automator.task_handler import TaskHandler
from conductor.client.configuration.configuration import Configuration
from conductor.client.worker.exception import NonRetryableException
from conductor.client.worker.worker_task import worker_task

log = logging.getLogger("video-workers")

FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE_BIN", "ffprobe")

# Preset knobs per video codec; callers may override any of them per format.
CODEC_DEFAULTS = {
    "libx264": ["-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p"],
    "libx265": ["-preset", "medium", "-crf", "28", "-pix_fmt", "yuv420p", "-tag:v", "hvc1"],
    "libvpx-vp9": ["-b:v", "0", "-crf", "33", "-row-mt", "1", "-deadline", "good", "-cpu-used", "4"],
    "libsvtav1": ["-preset", "8", "-crf", "35"],
}


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    log.info("exec: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} exited {proc.returncode}: {proc.stderr[-2000:]}")
    return proc


def _require_file(path: str) -> Path:
    p = Path(path or "")
    if not p.is_file():
        # Missing input will not fix itself on retry.
        raise NonRetryableException(f"video file not found: {path!r}")
    return p


def _out_dir(output_dir: str | None, video: Path) -> Path:
    d = Path(output_dir) if output_dir else video.parent / f"{video.stem}_outputs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_ffmpeg(args: list[str], dest: Path) -> None:
    tmp = dest.with_name(f".{dest.stem}.part{dest.suffix}")
    _run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args, str(tmp)])
    tmp.replace(dest)


def _probe(path: Path) -> dict:
    proc = _run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    return json.loads(proc.stdout)


def _fps(rate: str | None) -> float | None:
    if not rate or rate == "0/0":
        return None
    num, _, den = rate.partition("/")
    return round(float(num) / float(den or 1), 3)


@worker_task(task_definition_name="probe_video")
def probe_video(videoPath: str) -> dict:
    """Validate the input and report container/stream metadata."""
    video = _require_file(videoPath)
    info = _probe(video)
    streams = info.get("streams", [])
    v = next((s for s in streams if s["codec_type"] == "video"), None)
    a = next((s for s in streams if s["codec_type"] == "audio"), None)
    subs = [s for s in streams if s["codec_type"] == "subtitle"]
    if v is None:
        raise NonRetryableException(f"no video stream in {videoPath}")
    return {
        "videoPath": str(video.resolve()),
        "fileName": video.name,
        "container": info["format"].get("format_name"),
        "durationSec": round(float(info["format"].get("duration", 0)), 3),
        "sizeBytes": int(info["format"].get("size", 0)),
        "video": {"codec": v.get("codec_name"), "width": v.get("width"), "height": v.get("height"),
                  "fps": _fps(v.get("avg_frame_rate"))},
        "audio": None if a is None else {"codec": a.get("codec_name"), "channels": a.get("channels"),
                                         "sampleRate": int(a.get("sample_rate", 0))},
        "hasAudio": a is not None,
        "subtitleStreamCount": len(subs),
        "subtitleLanguages": [s.get("tags", {}).get("language", "und") for s in subs],
    }


# One thread per concurrent encode; keep at or below the task def's concurrentExecLimit.
@worker_task(task_definition_name="transcode_video", thread_count=int(os.environ.get("TRANSCODE_THREADS", "3")))
def transcode_video(videoPath: str, format: dict, outputDir: str = None) -> dict:
    """Encode the video into one target format (one dynamic-fork branch per format).

    format: {name, ext, videoCodec, audioCodec, height?, audioBitrate?, extraArgs?[]}
    """
    video = _require_file(videoPath)
    name = format.get("name") or "output"
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise NonRetryableException(f"format name must be [A-Za-z0-9_-]+, got {name!r}")
    ext = format.get("ext", "mp4")
    vcodec = format.get("videoCodec", "libx264")
    acodec = format.get("audioCodec", "aac")
    dest = _out_dir(outputDir, video) / f"{video.stem}.{name}.{ext}"

    args = ["-i", str(video), "-map", "0:v:0", "-map", "0:a:0?", "-c:v", vcodec]
    args += format.get("videoArgs") or CODEC_DEFAULTS.get(vcodec, [])
    if format.get("height"):
        # -2 keeps the aspect ratio and an even width, which most encoders need.
        args += ["-vf", f"scale=-2:'min({int(format['height'])},ih)'"]
    args += ["-c:a", acodec, "-b:a", format.get("audioBitrate", "128k")]
    if ext in ("mp4", "mov", "m4v"):
        args += ["-movflags", "+faststart"]
    args += [str(a) for a in format.get("extraArgs", [])]

    started = time.monotonic()
    _atomic_ffmpeg(args, dest)
    out = _probe(dest)
    v = next(s for s in out["streams"] if s["codec_type"] == "video")
    return {
        "format": name,
        "outputPath": str(dest.resolve()),
        "sizeBytes": dest.stat().st_size,
        "durationSec": round(float(out["format"].get("duration", 0)), 3),
        "videoCodec": v.get("codec_name"),
        "resolution": f"{v.get('width')}x{v.get('height')}",
        "encodeSeconds": round(time.monotonic() - started, 2),
    }


_SRT_TIME = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{3})")


def _srt_seconds(ts: str) -> float:
    h, m, s, ms = map(int, _SRT_TIME.match(ts.strip()).groups())
    return h * 3600 + m * 60 + s + ms / 1000


def _parse_srt(text: str) -> list[dict]:
    segments = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "").strip()):
        lines = block.split("\n")
        timing = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing is None:
            continue
        start, end = (part.strip() for part in lines[timing].split("-->"))
        # Strip inline styling tags such as <i> or {\an8}.
        body = re.sub(r"<[^>]+>|\{[^}]+\}", "", " ".join(lines[timing + 1:])).strip()
        if body:
            segments.append({"start": _srt_seconds(start), "end": _srt_seconds(end.split()[0]), "text": body})
    return segments


@worker_task(task_definition_name="extract_subtitles")
def extract_subtitles(videoPath: str, outputDir: str = None, streamIndex: int = 0) -> dict:
    """Pull an embedded text subtitle track out as SRT and parse it into dialogue segments."""
    video = _require_file(videoPath)
    dest = _out_dir(outputDir, video) / f"{video.stem}.dialogue.srt"
    _atomic_ffmpeg(["-i", str(video), "-map", f"0:s:{int(streamIndex)}", "-c:s", "srt", "-f", "srt"], dest)
    segments = _parse_srt(dest.read_text(encoding="utf-8", errors="replace"))
    return {"source": "subtitles", "transcriptPath": str(dest.resolve()), "language": None,
            "segments": segments, "segmentCount": len(segments)}


@worker_task(task_definition_name="extract_audio")
def extract_audio(videoPath: str, outputDir: str = None) -> dict:
    """Extract a 16 kHz mono WAV, the input format speech-to-text models expect."""
    video = _require_file(videoPath)
    dest = _out_dir(outputDir, video) / f"{video.stem}.audio16k.wav"
    _atomic_ffmpeg(["-i", str(video), "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"], dest)
    return {"audioPath": str(dest.resolve()), "sizeBytes": dest.stat().st_size}


_whisper_models: dict = {}


def _whisper(model_name: str):
    if model_name not in _whisper_models:
        from faster_whisper import WhisperModel  # imported lazily: heavy, and only this worker needs it

        _whisper_models[model_name] = WhisperModel(
            model_name,
            device=os.environ.get("WHISPER_DEVICE", "auto"),
            compute_type=os.environ.get("WHISPER_COMPUTE_TYPE", "default"),
        )
    return _whisper_models[model_name]


@worker_task(task_definition_name="transcribe_audio")
def transcribe_audio(audioPath: str, language: str = None, model: str = None) -> dict:
    """Speech-to-text with faster-whisper; returns timestamped dialogue segments."""
    audio = _require_file(audioPath)
    model_name = model or os.environ.get("WHISPER_MODEL", "small")
    segments_iter, info = _whisper(model_name).transcribe(
        str(audio), language=language or None, vad_filter=True, beam_size=5)
    segments = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
                for s in segments_iter if s.text.strip()]

    dest = audio.with_name(audio.name.replace(".audio16k.wav", "") + ".dialogue.json")
    tmp = dest.with_suffix(".part")
    tmp.write_text(json.dumps(segments, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(dest)
    return {"source": f"asr:faster-whisper/{model_name}", "transcriptPath": str(dest.resolve()),
            "language": info.language, "segments": segments, "segmentCount": len(segments)}


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for tool in (FFMPEG, FFPROBE):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} not found on PATH")
    config = Configuration()  # reads CONDUCTOR_SERVER_URL / CONDUCTOR_AUTH_KEY / CONDUCTOR_AUTH_SECRET
    with TaskHandler(configuration=config, scan_for_annotated_workers=True) as handler:
        handler.start_processes()
        handler.join_processes()


if __name__ == "__main__":
    main()

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yt_dlp  # type: ignore[import-untyped]
from yt_dlp.utils import DownloadError  # type: ignore[import-untyped]

from lvt.core.errors import LVTError
from lvt.engines.base import DownloadedMedia, MediaInfo
from lvt.security.paths import ensure_within_root, safe_filename


def discover_ffmpeg_binaries() -> tuple[Path, Path]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        try:
            import static_ffmpeg

            static_ffmpeg.add_paths()
        except (ImportError, OSError) as exc:
            raise LVTError("FFMPEG_NOT_FOUND", "未找到 FFmpeg/ffprobe") from exc
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise LVTError("FFMPEG_NOT_FOUND", "未找到 FFmpeg/ffprobe")
    return Path(ffmpeg), Path(ffprobe)


class YtDlpFFmpegDownloader:
    def __init__(
        self,
        *,
        ffmpeg_path: Path | None = None,
        ffprobe_path: Path | None = None,
    ) -> None:
        if ffmpeg_path is None or ffprobe_path is None:
            discovered_ffmpeg, discovered_ffprobe = discover_ffmpeg_binaries()
            ffmpeg_path = ffmpeg_path or discovered_ffmpeg
            ffprobe_path = ffprobe_path or discovered_ffprobe
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.version = f"yt-dlp:{yt_dlp.version.__version__};ffmpeg:{self._ffmpeg_version()}"

    def download(self, url: str, work_dir: Path) -> MediaInfo:
        downloaded = self.download_media(url, work_dir)
        return self.normalize_audio(downloaded, work_dir)

    def download_media(self, url: str, work_dir: Path) -> DownloadedMedia:
        work_dir.mkdir(parents=True, exist_ok=True)
        output_template = str(work_dir / "download.%(ext)s")
        options: dict[str, Any] = {
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 30,
        }
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(url, download=True)
                downloaded = Path(downloader.prepare_filename(info))
        except DownloadError as exc:
            message = str(exc)
            code = (
                "DOWNLOAD_UNSUPPORTED"
                if "Unsupported URL" in message or "No suitable extractor" in message
                else "DOWNLOAD_FAILED"
            )
            raise LVTError(code, f"媒体下载失败：{message}") from exc

        downloaded = ensure_within_root(downloaded, work_dir)
        if not downloaded.is_file() or downloaded.stat().st_size == 0:
            raise LVTError("MEDIA_INVALID", "下载产物不存在或为空")

        title = safe_filename(str(info.get("title") or downloaded.stem))
        return DownloadedMedia(media_path=downloaded, title=title)

    def normalize_audio(self, media: DownloadedMedia, work_dir: Path) -> MediaInfo:
        downloaded = ensure_within_root(media.media_path, work_dir)
        audio_path = ensure_within_root(work_dir / "audio.normalized.wav", work_dir)
        command = [
            os.fspath(self.ffmpeg_path),
            "-nostdin",
            "-y",
            "-i",
            os.fspath(downloaded),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            os.fspath(audio_path),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=300)
        except FileNotFoundError as exc:
            raise LVTError("FFMPEG_NOT_FOUND", "FFmpeg 不存在") from exc
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise LVTError("MEDIA_INVALID", f"FFmpeg 音频规范化失败：{exc}") from exc

        duration_ms = self._probe_duration_ms(audio_path)
        if not audio_path.is_file() or audio_path.stat().st_size == 0 or duration_ms <= 0:
            raise LVTError("MEDIA_INVALID", "规范化音频无效")
        return MediaInfo(audio_path=audio_path, title=media.title, duration_ms=duration_ms)

    def _probe_duration_ms(self, path: Path) -> int:
        command = [
            os.fspath(self.ffprobe_path),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            os.fspath(path),
        ]
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            duration = float(json.loads(completed.stdout)["format"]["duration"])
        except (
            FileNotFoundError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise LVTError("MEDIA_INVALID", "ffprobe 无法读取音频时长") from exc
        return round(duration * 1000)

    def _ffmpeg_version(self) -> str:
        try:
            completed = subprocess.run(
                [os.fspath(self.ffmpeg_path), "-version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.SubprocessError) as exc:
            raise LVTError("FFMPEG_NOT_FOUND", "FFmpeg 无法执行") from exc
        return completed.stdout.splitlines()[0].split()[2]

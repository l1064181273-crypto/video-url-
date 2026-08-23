from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import yt_dlp  # type: ignore[import-untyped]

from lvt.core.errors import LVTError
from lvt.core.processes import (
    CancellationToken,
    ProcessExecutionError,
    ProcessTimeoutError,
    SubprocessExecutor,
)
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
        process_executor: SubprocessExecutor | None = None,
    ) -> None:
        if ffmpeg_path is None or ffprobe_path is None:
            discovered_ffmpeg, discovered_ffprobe = discover_ffmpeg_binaries()
            ffmpeg_path = ffmpeg_path or discovered_ffmpeg
            ffprobe_path = ffprobe_path or discovered_ffprobe
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.process_executor = process_executor or SubprocessExecutor()
        self.downloader_version = f"yt-dlp:{yt_dlp.version.__version__}"
        self.normalizer_version = f"ffmpeg:{self._ffmpeg_version()}"
        self.version = f"{self.downloader_version};{self.normalizer_version}"

    def download(
        self,
        url: str,
        work_dir: Path,
        cancellation: CancellationToken | None = None,
    ) -> MediaInfo:
        downloaded = self.download_media(url, work_dir, cancellation)
        return self.normalize_audio(downloaded, work_dir, cancellation)

    def download_media(
        self,
        url: str,
        work_dir: Path,
        cancellation: CancellationToken | None = None,
    ) -> DownloadedMedia:
        work_dir.mkdir(parents=True, exist_ok=True)
        output_template = str(work_dir / "download.%(ext)s")
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--format",
            "bestaudio/best",
            "--output",
            output_template,
            "--no-playlist",
            "--quiet",
            "--no-warnings",
            "--retries",
            "3",
            "--fragment-retries",
            "3",
            "--socket-timeout",
            "30",
            "--print",
            "after_move:__LVT_PATH__%(filepath)s",
            "--print",
            "after_move:__LVT_TITLE__%(title)s",
            url,
        ]
        try:
            result = self.process_executor.run(
                command,
                timeout=600,
                cancellation=cancellation,
            )
        except ProcessExecutionError as exc:
            message = exc.stderr or exc.stdout
            code = (
                "DOWNLOAD_UNSUPPORTED"
                if "Unsupported URL" in message or "No suitable extractor" in message
                else "DOWNLOAD_FAILED"
            )
            raise LVTError(code, f"媒体下载失败：{message}") from exc
        except ProcessTimeoutError as exc:
            raise LVTError("DOWNLOAD_FAILED", "媒体下载超时") from exc

        path_line = next(
            (line for line in result.stdout.splitlines() if line.startswith("__LVT_PATH__")),
            "",
        )
        title_line = next(
            (line for line in result.stdout.splitlines() if line.startswith("__LVT_TITLE__")),
            "",
        )
        if not path_line:
            raise LVTError("DOWNLOAD_FAILED", "yt-dlp 未返回下载文件路径")
        downloaded = Path(path_line.removeprefix("__LVT_PATH__"))

        downloaded = ensure_within_root(downloaded, work_dir)
        if not downloaded.is_file() or downloaded.stat().st_size == 0:
            raise LVTError("MEDIA_INVALID", "下载产物不存在或为空")

        title = safe_filename(title_line.removeprefix("__LVT_TITLE__") or downloaded.stem)
        return DownloadedMedia(media_path=downloaded, title=title)

    def normalize_audio(
        self,
        media: DownloadedMedia,
        work_dir: Path,
        cancellation: CancellationToken | None = None,
    ) -> MediaInfo:
        downloaded = media.media_path
        if downloaded.is_symlink() or not downloaded.is_file():
            raise LVTError("MEDIA_INVALID", "下载阶段输入不存在或不是普通文件")
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
            self.process_executor.run(
                command,
                timeout=300,
                cancellation=cancellation,
            )
        except ProcessExecutionError as exc:
            raise LVTError("MEDIA_INVALID", f"FFmpeg 音频规范化失败：{exc}") from exc
        except ProcessTimeoutError as exc:
            raise LVTError("MEDIA_INVALID", "FFmpeg 音频规范化超时") from exc

        duration_ms = self._probe_duration_ms(audio_path, cancellation)
        if not audio_path.is_file() or audio_path.stat().st_size == 0 or duration_ms <= 0:
            raise LVTError("MEDIA_INVALID", "规范化音频无效")
        return MediaInfo(audio_path=audio_path, title=media.title, duration_ms=duration_ms)

    def probe_duration_ms(self, path: Path, cancellation: CancellationToken | None = None) -> int:
        return self._probe_duration_ms(path, cancellation)

    def _probe_duration_ms(self, path: Path, cancellation: CancellationToken | None = None) -> int:
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
            completed = self.process_executor.run(
                command,
                timeout=30,
                cancellation=cancellation,
            )
            duration = float(json.loads(completed.stdout)["format"]["duration"])
        except (
            ProcessExecutionError,
            ProcessTimeoutError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise LVTError("MEDIA_INVALID", "ffprobe 无法读取音频时长") from exc
        return round(duration * 1000)

    def _ffmpeg_version(self) -> str:
        try:
            completed = self.process_executor.run(
                [os.fspath(self.ffmpeg_path), "-version"],
                timeout=10,
            )
        except (ProcessExecutionError, ProcessTimeoutError) as exc:
            raise LVTError("FFMPEG_NOT_FOUND", "FFmpeg 无法执行") from exc
        return completed.stdout.splitlines()[0].split()[2]

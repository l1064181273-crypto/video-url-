from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol


class JobStatus(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    EXTRACTING = "extracting"
    TRANSCRIBING = "transcribing"
    DIARIZING = "diarizing"
    SEGMENTING = "segmenting"
    TRANSLATING = "translating"
    EXPORTING = "exporting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


ACTIVE_JOB_STATUSES: Final[frozenset[JobStatus]] = frozenset(
    {
        JobStatus.DOWNLOADING,
        JobStatus.EXTRACTING,
        JobStatus.TRANSCRIBING,
        JobStatus.DIARIZING,
        JobStatus.SEGMENTING,
        JobStatus.TRANSLATING,
        JobStatus.EXPORTING,
    }
)

TERMINAL_JOB_STATUSES: Final[frozenset[JobStatus]] = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
)

_REQUEUE_OR_STOP: Final[frozenset[JobStatus]] = frozenset(
    {JobStatus.QUEUED, JobStatus.FAILED, JobStatus.CANCELLING}
)

LEGAL_TRANSITIONS: Final[Mapping[JobStatus, frozenset[JobStatus]]] = MappingProxyType(
    {
        JobStatus.QUEUED: ACTIVE_JOB_STATUSES | {JobStatus.CANCELLED},
        JobStatus.DOWNLOADING: _REQUEUE_OR_STOP | {JobStatus.EXTRACTING},
        JobStatus.EXTRACTING: _REQUEUE_OR_STOP | {JobStatus.TRANSCRIBING},
        JobStatus.TRANSCRIBING: _REQUEUE_OR_STOP | {JobStatus.DIARIZING, JobStatus.SEGMENTING},
        JobStatus.DIARIZING: _REQUEUE_OR_STOP | {JobStatus.SEGMENTING},
        JobStatus.SEGMENTING: _REQUEUE_OR_STOP | {JobStatus.TRANSLATING},
        JobStatus.TRANSLATING: _REQUEUE_OR_STOP | {JobStatus.EXPORTING},
        JobStatus.EXPORTING: _REQUEUE_OR_STOP | {JobStatus.COMPLETED},
        JobStatus.COMPLETED: frozenset(),
        JobStatus.FAILED: frozenset({JobStatus.QUEUED}),
        JobStatus.CANCELLING: frozenset({JobStatus.CANCELLED}),
        JobStatus.CANCELLED: frozenset({JobStatus.QUEUED}),
    }
)


def can_transition(current: JobStatus, target: JobStatus) -> bool:
    return target in LEGAL_TRANSITIONS[current]


class JobEventType(StrEnum):
    CREATED = "created"
    CLAIMED = "claimed"
    STAGE_CHANGED = "stage_changed"
    PROGRESS = "progress"
    CHECKPOINT_PUBLISHED = "checkpoint_published"
    AUTOMATIC_REQUEUED = "automatic_requeued"
    MANUAL_RETRY = "manual_retry"
    CANCEL_REQUESTED = "cancel_requested"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ErrorCode(StrEnum):
    INVALID_URL = "INVALID_URL"
    DOWNLOAD_UNSUPPORTED = "DOWNLOAD_UNSUPPORTED"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    FFMPEG_NOT_FOUND = "FFMPEG_NOT_FOUND"
    MEDIA_INVALID = "MEDIA_INVALID"
    ASR_MODEL_MISSING = "ASR_MODEL_MISSING"
    TRANSCRIPTION_FAILED = "TRANSCRIPTION_FAILED"
    DIARIZATION_TOKEN_REQUIRED = "DIARIZATION_TOKEN_REQUIRED"
    DIARIZATION_MODEL_MISSING = "DIARIZATION_MODEL_MISSING"
    DIARIZATION_FAILED = "DIARIZATION_FAILED"
    UNSUPPORTED_SOURCE_LANGUAGE = "UNSUPPORTED_SOURCE_LANGUAGE"
    OLLAMA_UNAVAILABLE = "OLLAMA_UNAVAILABLE"
    TRANSLATION_MODEL_MISSING = "TRANSLATION_MODEL_MISSING"
    TRANSLATION_INVALID_RESPONSE = "TRANSLATION_INVALID_RESPONSE"
    TRANSLATION_FAILED = "TRANSLATION_FAILED"
    TRANSLATION_ALL_MODELS_FAILED = "TRANSLATION_ALL_MODELS_FAILED"
    EXPORT_FAILED = "EXPORT_FAILED"
    DISK_SPACE_LOW = "DISK_SPACE_LOW"
    CANCELLED_BY_USER = "CANCELLED_BY_USER"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class CacheResumePoint(StrEnum):
    NONE = "none"
    DOWNLOADED_MEDIA = "downloaded_media"
    NORMALIZED_AUDIO = "normalized_audio"
    ASR_RESULT = "asr_result"
    SOURCE_TRANSCRIPT = "source_transcript"
    TRANSLATED_TRANSCRIPT = "translated_transcript"
    LATEST_VALID_CHECKPOINT = "latest_valid_checkpoint"


@dataclass(frozen=True)
class ErrorPolicy:
    auto_requeue: bool
    manual_retry: bool
    cache_resume_point: CacheResumePoint
    user_advice: str


@dataclass(frozen=True)
class ClassifiedError:
    code: ErrorCode
    policy: ErrorPolicy


class HasErrorCode(Protocol):
    code: str


def _policy(
    *,
    auto_requeue: bool = False,
    manual_retry: bool = True,
    cache: CacheResumePoint,
    advice: str,
) -> ErrorPolicy:
    return ErrorPolicy(
        auto_requeue=auto_requeue,
        manual_retry=manual_retry,
        cache_resume_point=cache,
        user_advice=advice,
    )


ERROR_POLICIES: Final[Mapping[ErrorCode, ErrorPolicy]] = MappingProxyType(
    {
        ErrorCode.INVALID_URL: _policy(
            manual_retry=False,
            cache=CacheResumePoint.NONE,
            advice="请检查并重新提交有效的 HTTP 或 HTTPS 视频地址",
        ),
        ErrorCode.DOWNLOAD_UNSUPPORTED: _policy(
            cache=CacheResumePoint.NONE,
            advice="当前地址或站点不受支持，更新下载组件或更换地址后重试",
        ),
        ErrorCode.DOWNLOAD_FAILED: _policy(
            auto_requeue=True,
            cache=CacheResumePoint.NONE,
            advice="请检查网络、登录限制或源站状态后重试",
        ),
        ErrorCode.FFMPEG_NOT_FOUND: _policy(
            cache=CacheResumePoint.DOWNLOADED_MEDIA,
            advice="请安装或修复 FFmpeg 路径后重试",
        ),
        ErrorCode.MEDIA_INVALID: _policy(
            cache=CacheResumePoint.NONE,
            advice="媒体文件无效，请更换来源后重新提交或重试",
        ),
        ErrorCode.ASR_MODEL_MISSING: _policy(
            cache=CacheResumePoint.NORMALIZED_AUDIO,
            advice="请安装配置的转写模型后重试",
        ),
        ErrorCode.TRANSCRIPTION_FAILED: _policy(
            cache=CacheResumePoint.NORMALIZED_AUDIO,
            advice="请检查转写模型、内存和媒体音轨后重试",
        ),
        ErrorCode.DIARIZATION_TOKEN_REQUIRED: _policy(
            cache=CacheResumePoint.ASR_RESULT,
            advice="请配置说话人分离所需凭证后重试",
        ),
        ErrorCode.DIARIZATION_MODEL_MISSING: _policy(
            cache=CacheResumePoint.ASR_RESULT,
            advice="请安装或修复说话人分离模型后重试",
        ),
        ErrorCode.DIARIZATION_FAILED: _policy(
            cache=CacheResumePoint.ASR_RESULT,
            advice="请检查说话人模型、内存和音频质量后重试",
        ),
        ErrorCode.UNSUPPORTED_SOURCE_LANGUAGE: _policy(
            cache=CacheResumePoint.SOURCE_TRANSCRIPT,
            advice="当前源语言不受翻译模型支持，请更换模型或输入",
        ),
        ErrorCode.OLLAMA_UNAVAILABLE: _policy(
            auto_requeue=True,
            cache=CacheResumePoint.SOURCE_TRANSCRIPT,
            advice="请启动 Ollama 并确认本地服务可访问",
        ),
        ErrorCode.TRANSLATION_MODEL_MISSING: _policy(
            cache=CacheResumePoint.SOURCE_TRANSCRIPT,
            advice="请安装主翻译模型或备用模型后重试",
        ),
        ErrorCode.TRANSLATION_INVALID_RESPONSE: _policy(
            cache=CacheResumePoint.SOURCE_TRANSCRIPT,
            advice="模型响应未通过严格校验，请检查模型状态后重试",
        ),
        ErrorCode.TRANSLATION_FAILED: _policy(
            cache=CacheResumePoint.SOURCE_TRANSCRIPT,
            advice="翻译执行失败，请检查本地模型资源后重试",
        ),
        ErrorCode.TRANSLATION_ALL_MODELS_FAILED: _policy(
            cache=CacheResumePoint.SOURCE_TRANSCRIPT,
            advice="主模型和备用模型均失败，请检查两个模型后重试",
        ),
        ErrorCode.EXPORT_FAILED: _policy(
            cache=CacheResumePoint.TRANSLATED_TRANSCRIPT,
            advice="请检查输出目录权限和磁盘空间后重试",
        ),
        ErrorCode.DISK_SPACE_LOW: _policy(
            cache=CacheResumePoint.LATEST_VALID_CHECKPOINT,
            advice="请清理磁盘空间后重试",
        ),
        ErrorCode.CANCELLED_BY_USER: _policy(
            cache=CacheResumePoint.LATEST_VALID_CHECKPOINT,
            advice="任务已取消，可手工重新加入队列",
        ),
        ErrorCode.INTERNAL_ERROR: _policy(
            cache=CacheResumePoint.LATEST_VALID_CHECKPOINT,
            advice="请查看本地日志，确认环境后手工重试",
        ),
    }
)


def classify_error_code(code: str | ErrorCode) -> ClassifiedError:
    try:
        normalized = ErrorCode(code)
    except ValueError:
        normalized = ErrorCode.INTERNAL_ERROR
    return ClassifiedError(code=normalized, policy=ERROR_POLICIES[normalized])


def classify_exception(error: HasErrorCode) -> ClassifiedError:
    return classify_error_code(error.code)


def error_policy_for(code: str | ErrorCode) -> ErrorPolicy:
    return classify_error_code(code).policy


def error_policy_for_exception(error: HasErrorCode) -> ErrorPolicy:
    return classify_exception(error).policy

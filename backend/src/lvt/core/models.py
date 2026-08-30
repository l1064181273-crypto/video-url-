from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lvt.core.platform_runtime import default_asr_model

DEFAULT_ASR_MODEL = default_asr_model()


class JobOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asr_model: str = DEFAULT_ASR_MODEL
    translate_to: str = "zh-CN"
    diarization: bool = True

    @field_validator("asr_model", mode="before")
    @classmethod
    def resolve_default_asr_model(cls, value: object) -> object:
        if value == "default":
            return DEFAULT_ASR_MODEL
        return value


class Segment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    speaker: str = Field(pattern=r"^Speaker [1-9]\d*$")
    source_language: str = Field(min_length=2, max_length=16)
    source_text: str = Field(min_length=1)
    translated_text: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_time_range(self) -> Segment:
        if self.start_ms >= self.end_ms:
            raise ValueError("start_ms must be less than end_ms")
        return self


class Transcript(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    job_id: str
    source_url: str
    title: str
    duration_ms: int = Field(gt=0)
    detected_language: str
    engine_versions: dict[str, str] = Field(default_factory=dict)
    processing_options: dict[str, Any] = Field(default_factory=dict)
    segments: list[Segment]
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_segments(self) -> Transcript:
        expected_ids = list(range(1, len(self.segments) + 1))
        actual_ids = [segment.id for segment in self.segments]
        if actual_ids != expected_ids:
            raise ValueError("segment ids must be continuous and start at 1")
        starts = [segment.start_ms for segment in self.segments]
        if starts != sorted(starts):
            raise ValueError("segments must be sorted by start_ms")
        if any(segment.end_ms > self.duration_ms for segment in self.segments):
            raise ValueError("segment timestamp exceeds media duration")
        return self


IMMUTABLE_TRANSLATION_FIELDS = (
    "id",
    "start_ms",
    "end_ms",
    "speaker",
    "source_language",
    "source_text",
)


def apply_translations(segments: list[Segment], translations: dict[int, str]) -> list[Segment]:
    expected_ids = {segment.id for segment in segments}
    if set(translations) != expected_ids:
        raise ValueError("translation ids must exactly match segment ids")
    if any(not isinstance(text, str) or not text.strip() for text in translations.values()):
        raise ValueError("translations must be non-empty strings")

    translated: list[Segment] = []
    for segment in segments:
        before = segment.model_dump(include=set(IMMUTABLE_TRANSLATION_FIELDS))
        updated = segment.model_copy(
            deep=True, update={"translated_text": translations[segment.id].strip()}
        )
        after = updated.model_dump(include=set(IMMUTABLE_TRANSLATION_FIELDS))
        if before != after:
            raise RuntimeError("translation changed immutable segment fields")
        translated.append(updated)
    return translated

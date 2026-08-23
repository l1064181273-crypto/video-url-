from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from lvt.core.jobs import JobStatus
from lvt.db.repository import JobRepository

STAGE_WEIGHTS: Final[Mapping[JobStatus, int]] = MappingProxyType(
    {
        JobStatus.DOWNLOADING: 15,
        JobStatus.EXTRACTING: 5,
        JobStatus.TRANSCRIBING: 35,
        JobStatus.DIARIZING: 15,
        JobStatus.SEGMENTING: 5,
        JobStatus.TRANSLATING: 20,
        JobStatus.EXPORTING: 5,
    }
)


class StaleWorkerProgressError(RuntimeError):
    pass


def calculate_overall_progress(status: JobStatus, stage_progress: int) -> int:
    if not isinstance(status, JobStatus) or status not in STAGE_WEIGHTS:
        raise ValueError("progress requires an active JobStatus")
    if type(stage_progress) is not int or not 0 <= stage_progress <= 100:
        raise ValueError("stage_progress must be an integer from 0 to 100")
    base = 0
    for candidate, weight in STAGE_WEIGHTS.items():
        if candidate is status:
            return base + weight * stage_progress // 100
        base += weight
    raise ValueError("unknown progress stage")


class ProgressReporter:
    def __init__(
        self,
        repository: JobRepository,
        job_id: str,
        run_id: str,
        *,
        high_water: int,
    ) -> None:
        if type(high_water) is not int or not 0 <= high_water <= 100:
            raise ValueError("high_water must be an integer from 0 to 100")
        self.repository = repository
        self.job_id = job_id
        self.run_id = run_id
        self.high_water = high_water

    def persist(self, status: JobStatus, stage_progress: int) -> bool:
        candidate = calculate_overall_progress(status, stage_progress)
        overall = max(self.high_water, candidate)
        persisted = self.repository.update_progress(
            self.job_id,
            self.run_id,
            status,
            stage_progress=stage_progress,
            overall_progress=overall,
        )
        if persisted:
            self.high_water = overall
        return persisted

    def __call__(self, status: JobStatus, stage_progress: int) -> None:
        if not self.persist(status, stage_progress):
            raise StaleWorkerProgressError(
                f"stale progress callback for {self.job_id}/{self.run_id}/{status.value}"
            )

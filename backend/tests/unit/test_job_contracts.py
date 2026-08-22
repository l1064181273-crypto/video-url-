from itertools import product

import pytest

from lvt.core.jobs import (
    ACTIVE_JOB_STATUSES,
    ERROR_POLICIES,
    LEGAL_TRANSITIONS,
    ErrorCode,
    JobEventType,
    JobStatus,
    can_transition,
    error_policy_for_exception,
)

EXPECTED_TRANSITIONS = {
    JobStatus.QUEUED: ACTIVE_JOB_STATUSES | {JobStatus.CANCELLED},
    JobStatus.DOWNLOADING: {
        JobStatus.EXTRACTING,
        JobStatus.QUEUED,
        JobStatus.FAILED,
        JobStatus.CANCELLING,
    },
    JobStatus.EXTRACTING: {
        JobStatus.TRANSCRIBING,
        JobStatus.QUEUED,
        JobStatus.FAILED,
        JobStatus.CANCELLING,
    },
    JobStatus.TRANSCRIBING: {
        JobStatus.DIARIZING,
        JobStatus.SEGMENTING,
        JobStatus.QUEUED,
        JobStatus.FAILED,
        JobStatus.CANCELLING,
    },
    JobStatus.DIARIZING: {
        JobStatus.SEGMENTING,
        JobStatus.QUEUED,
        JobStatus.FAILED,
        JobStatus.CANCELLING,
    },
    JobStatus.SEGMENTING: {
        JobStatus.TRANSLATING,
        JobStatus.QUEUED,
        JobStatus.FAILED,
        JobStatus.CANCELLING,
    },
    JobStatus.TRANSLATING: {
        JobStatus.EXPORTING,
        JobStatus.QUEUED,
        JobStatus.FAILED,
        JobStatus.CANCELLING,
    },
    JobStatus.EXPORTING: {
        JobStatus.COMPLETED,
        JobStatus.QUEUED,
        JobStatus.FAILED,
        JobStatus.CANCELLING,
    },
    JobStatus.COMPLETED: set(),
    JobStatus.FAILED: {JobStatus.QUEUED},
    JobStatus.CANCELLING: {JobStatus.CANCELLED},
    JobStatus.CANCELLED: {JobStatus.QUEUED},
}


@pytest.mark.parametrize(
    ("current", "target"),
    [(current, target) for current, targets in EXPECTED_TRANSITIONS.items() for target in targets],
)
def test_all_declared_job_status_transitions_are_legal(
    current: JobStatus, target: JobStatus
) -> None:
    assert can_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (current, target)
        for current, target in product(JobStatus, repeat=2)
        if target not in EXPECTED_TRANSITIONS[current]
    ],
)
def test_all_undeclared_job_status_transitions_are_illegal(
    current: JobStatus, target: JobStatus
) -> None:
    assert not can_transition(current, target)


def test_queued_can_resume_at_every_active_stage() -> None:
    assert {status: set(targets) for status, targets in LEGAL_TRANSITIONS.items()} == (
        EXPECTED_TRANSITIONS
    )
    assert LEGAL_TRANSITIONS[JobStatus.QUEUED].issuperset(ACTIVE_JOB_STATUSES)


def test_interrupted_is_an_event_not_a_persisted_job_status() -> None:
    assert JobEventType.INTERRUPTED.value == "interrupted"
    assert "interrupted" not in {status.value for status in JobStatus}


def test_every_public_error_code_has_a_complete_policy() -> None:
    assert set(ERROR_POLICIES) == set(ErrorCode)
    for code, policy in ERROR_POLICIES.items():
        assert policy.cache_resume_point
        assert policy.user_advice.strip()
        assert any("\u4e00" <= character <= "\u9fff" for character in policy.user_advice), code


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.DIARIZATION_MODEL_MISSING,
        ErrorCode.UNSUPPORTED_SOURCE_LANGUAGE,
        ErrorCode.TRANSLATION_FAILED,
        ErrorCode.TRANSLATION_ALL_MODELS_FAILED,
    ],
)
def test_actual_production_error_codes_are_registered(code: ErrorCode) -> None:
    assert code in ERROR_POLICIES


def test_only_transient_job_errors_are_automatically_requeued() -> None:
    retryable = {code for code, policy in ERROR_POLICIES.items() if policy.auto_requeue}
    assert retryable == {ErrorCode.DOWNLOAD_FAILED, ErrorCode.OLLAMA_UNAVAILABLE}


def test_error_adapter_uses_structured_code_not_exception_message() -> None:
    class StructuredEngineError(RuntimeError):
        code = "TRANSLATION_FAILED"

    error = StructuredEngineError("DOWNLOAD_FAILED appears only in this message")

    policy = error_policy_for_exception(error)

    assert not policy.auto_requeue

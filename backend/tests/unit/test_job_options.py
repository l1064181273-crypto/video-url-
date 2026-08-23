from lvt.core.models import DEFAULT_ASR_MODEL, JobOptions


def test_default_asr_model_is_persisted_as_canonical_model_name() -> None:
    assert JobOptions().asr_model == DEFAULT_ASR_MODEL
    assert JobOptions(asr_model="default").asr_model == DEFAULT_ASR_MODEL


def test_custom_asr_model_is_preserved() -> None:
    assert JobOptions(asr_model="mlx-community/whisper-medium-mlx").asr_model == (
        "mlx-community/whisper-medium-mlx"
    )

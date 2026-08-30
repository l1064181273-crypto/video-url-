from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[2]


def test_installed_launcher_is_relocatable_and_uses_app_owned_paths(tmp_path: Path) -> None:
    release_root = tmp_path / "发布 目录" / "Local Video Transcriber"
    source_root = release_root / "backend" / "src"
    shutil.copytree(ROOT / "backend" / "src" / "lvt", source_root / "lvt")

    data_root = tmp_path / "用户 数据" / "LocalVideoTranscriber"
    ffmpeg_dir = data_root / "app" / "tools" / "ffmpeg" / "8.0" / "bin"
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(source_root),
            "LVT_DATA_ROOT": str(data_root),
            "LVT_FFMPEG_DIR": str(ffmpeg_dir),
            "LVT_INSTALLED_MODE": "1",
            "LVT_TOKEN": "test-token",
            "HF_HOME": "/tmp/ambient-hf-home",
            "HF_HUB_CACHE": "/tmp/ambient-hf-cache",
        }
    )
    script = dedent(
        """
        import json
        import os
        import lvt.main as main

        started = {}
        main.uvicorn.run = lambda _app, **kwargs: started.update(kwargs)
        main.main()
        print(json.dumps({
            "module": main.__file__,
            "data_root": str(main.settings.data_root),
            "model_root": str(main.settings.model_root),
            "hf_home": os.environ["HF_HOME"],
            "hf_hub_cache": os.environ["HF_HUB_CACHE"],
            "ollama_models": os.environ["OLLAMA_MODELS"],
            "segmentation": str(main.pipeline_config.segmentation_model),
            "embedding": str(main.pipeline_config.embedding_model),
            "asr_model_path": str(main.pipeline_config.asr_model_path),
            "ollama_url": main.pipeline_config.ollama_url,
            "installed_mode": main.pipeline_config.installed_mode,
            "started": started,
        }))
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    model_root = data_root / "models"
    huggingface_root = model_root / "huggingface"

    assert Path(payload["module"]).is_relative_to(release_root)
    assert payload["data_root"] == str(data_root)
    assert payload["model_root"] == str(model_root)
    assert payload["hf_home"] == str(huggingface_root)
    assert payload["hf_hub_cache"] == str(huggingface_root)
    assert payload["ollama_models"] == str(model_root / "ollama")
    assert payload["segmentation"] == str(
        model_root / "diarization" / "segmentation" / "model.onnx"
    )
    assert payload["embedding"] == str(
        model_root / "diarization" / "embedding" / "nemo_en_titanet_small.onnx"
    )
    assert payload["asr_model_path"] == str(model_root / "asr" / "whisper-small-mlx")
    assert payload["ollama_url"] == "http://127.0.0.1:11435"
    assert payload["installed_mode"] is True
    assert payload["started"] == {"host": "127.0.0.1", "port": 8765}
    assert str(ROOT) not in completed.stdout

"""本地 FunASR（SenseVoiceSmall）语音转写封装，不涉及 langgraph/deepagents。

DeepSeek 主模型不支持音频输入（见 `src/agent/main.py` 的 `profile={"audio_inputs": False}`），
所以语音消息必须先在这里转成文字，才能进入正常的 agent 处理流程。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MODELS_DIR = PROJECT_ROOT / "models"
# FunASR 默认从 ModelScope 下载模型；把缓存目录钉死在项目内，不用 ~/.cache。
os.environ.setdefault("MODELSCOPE_CACHE", str(MODELS_DIR))
_LOCAL_MODEL_DIR = MODELS_DIR / "SenseVoiceSmall"


class VoiceTranscribeError(Exception):
    """语音转写失败（ffmpeg 转码失败、模型加载/推理失败等）统一包成这个异常。"""


_model = None
_model_lock = threading.Lock()


def _get_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            from funasr import AutoModel

            model_ref = str(_LOCAL_MODEL_DIR) if _LOCAL_MODEL_DIR.exists() else "iic/SenseVoiceSmall"
            logger.info("加载 FunASR 模型：%s", model_ref)
            _model = AutoModel(model=model_ref, trust_remote_code=True, device="cpu", disable_update=True)
    return _model


def _ffmpeg_exe() -> str:
    """优先用 PATH 里的 ffmpeg；没有则用 imageio-ffmpeg 自带的二进制（随 uv sync 安装）。"""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        raise VoiceTranscribeError(
            "未找到 ffmpeg：请重新 uv sync（含 imageio-ffmpeg），或 brew install ffmpeg"
        ) from e


def _decode_to_wav16k_mono(audio_bytes: bytes) -> bytes:
    try:
        result = subprocess.run(
            [
                _ffmpeg_exe(),
                "-i",
                "pipe:0",
                "-f",
                "wav",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-loglevel",
                "error",
                "pipe:1",
            ],
            input=audio_bytes,
            capture_output=True,
            check=True,
        )
    except VoiceTranscribeError:
        raise
    except FileNotFoundError as e:
        raise VoiceTranscribeError(
            "未找到 ffmpeg：请重新 uv sync（含 imageio-ffmpeg），或 brew install ffmpeg"
        ) from e
    except subprocess.CalledProcessError as e:
        raise VoiceTranscribeError(f"ffmpeg 转码失败：{e.stderr.decode(errors='ignore')}") from e
    return result.stdout


def _transcribe_sync(audio_bytes: bytes) -> str:
    from funasr.utils.postprocess_utils import rich_transcription_postprocess

    wav_bytes = _decode_to_wav16k_mono(audio_bytes)
    model = _get_model()

    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        f.write(wav_bytes)
        f.flush()
        res = model.generate(input=f.name, cache={}, language="auto", use_itn=True, batch_size_s=60)

    if not res:
        return ""
    return rich_transcription_postprocess(res[0]["text"]).strip()


async def transcribe_voice(audio_bytes: bytes, mimetype: str) -> str:
    try:
        return await asyncio.to_thread(_transcribe_sync, audio_bytes)
    except VoiceTranscribeError:
        raise
    except Exception as e:
        raise VoiceTranscribeError(str(e)) from e

"""faster-whisper 本地转译适配器（跨平台本地识别，无需 API key/网络）。

跟 openai_audio.py 走同一份接口约定（transcribe(cfg, con, video_id,
audio_path, duration_sec, work) -> Path），方便 pipeline.py 按
transcription.primary 的取值在两者之间切换而不用改调用方代码。

为什么选 faster-whisper 而不是 whisper.cpp（config.py 里那个还没实现的
"whisper_cpp" 占位值）：
  - faster-whisper 是纯 pip 包（底层 CTranslate2），`pip install
    faster-whisper` 就能跑，不需要另外编译/下载一个平台专属的可执行文件——
    这点对"发布一个 Windows 版本"这个目标特别重要，PyInstaller 打包一个
    Python 包比打包一个还要再管理生命周期的外部 C++ 进程简单得多。
  - 真正跨平台：CPU 模式在 macOS/Windows/Linux 上都能跑，用户不需要
    Nvidia GPU（有 GPU 时也支持 device="cuda" 加速，但这版先只做 CPU
    默认，见下面 _default_device()）。
  - 免费、离线：不需要 OpenAI key，也不像 openai_audio.py 那样按分钟计费，
    只是本地算力换时间——特别适合还没配置任何云端 AI key、或者不想把
    音频上传出去的用户。

模型/设备通过 config 里 transcription.local_* 三个键控制，全部给了保守的
默认值（small + cpu + int8），首次使用时 faster-whisper 会自己下载模型
权重到 ~/.cache/huggingface（macOS/Linux）或 %USERPROFILE%\\.cache\\
huggingface（Windows），跟这个项目本身的 APP_SUPPORT 目录无关，不用在
paths.py 里额外管理。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .openai_audio import to_srt

# tiny/base/small/medium/large-v3——数字越大越准但越慢/模型文件越大。
# small 是速度和准确率之间对大多数中文财经/科普类视频比较均衡的默认档。
DEFAULT_MODEL_SIZE = "small"
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE_TYPE = "int8"  # CPU 上 int8 量化比 float32 快很多、精度损失很小


class FasterWhisperError(RuntimeError):
    def __init__(self, msg: str, transient: bool = True):
        super().__init__(msg)
        self.transient = transient


# 同一进程内按 (model_size, device, compute_type) 缓存已加载的模型——模型
# 加载本身（读取权重文件到内存）比转译一段十几分钟的音频还慢好几倍，一次
# 运行处理多个视频时不重复加载。
_MODEL_CACHE: dict[tuple[str, str, str], Any] = {}


def _get_model(model_size: str, device: str, compute_type: str) -> Any:
    key = (model_size, device, compute_type)
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise FasterWhisperError(
            f"faster-whisper not installed ({e}); pip install "
            "youtube-recorder[local-whisper] or run "
            "`pip install faster-whisper`", transient=False) from e
    try:
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
    except Exception as e:  # 模型下载失败/显存不足/不支持的 compute_type 等
        raise FasterWhisperError(
            f"failed to load faster-whisper model {model_size!r} "
            f"(device={device}, compute_type={compute_type}): {e}",
            transient=True) from e
    _MODEL_CACHE[key] = model
    return model


def transcribe(cfg, con, video_id: str, audio_path: Path,
               duration_sec: float, work: Path) -> Path:
    """转译 audio_path，输出 transcript.original.srt 到 work，返回路径。"""
    model_size = cfg.get("transcription.local_model", DEFAULT_MODEL_SIZE)
    device = cfg.get("transcription.local_device", DEFAULT_DEVICE)
    compute_type = cfg.get("transcription.local_compute_type", DEFAULT_COMPUTE_TYPE)
    language = cfg.get("transcription.language", "auto")
    lang_arg = None if language in ("auto", "", None) else language

    model = _get_model(model_size, device, compute_type)
    try:
        segments_iter, _info = model.transcribe(
            str(audio_path), language=lang_arg, vad_filter=True)
        segs = [(float(s.start), float(s.end), s.text) for s in segments_iter]
    except FasterWhisperError:
        raise
    except Exception as e:  # 转译过程本身出错（损坏的音频文件等）
        raise FasterWhisperError(f"faster-whisper transcribe failed: {e}",
                                 transient=True) from e

    if not segs:
        raise FasterWhisperError("empty transcription", transient=False)

    dest = work / "transcript.original.srt"
    tmp = dest.with_suffix(".srt.tmp")
    tmp.write_text(to_srt(segs), encoding="utf-8")
    tmp.replace(dest)

    if con is not None:
        from .db import now
        minutes = duration_sec / 60
        con.execute(
            "INSERT INTO costs(video_id,provider,model,units,unit_type,"
            "estimated_cost_usd,at) VALUES(?,?,?,?,?,?,?)",
            (video_id, "faster_whisper", model_size, round(minutes, 2),
             "audio_minutes", 0.0, now()))
        con.commit()
    return dest

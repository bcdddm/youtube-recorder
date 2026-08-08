"""Video pipeline state machine (v0.2 design §2.3).

Every status change must go through `guard_transition`. Illegal transitions
raise TransitionError so bugs surface immediately instead of corrupting state.
"""

from __future__ import annotations

# --- stages -----------------------------------------------------------------

DISCOVERED = "discovered"
METADATA_READY = "metadata_ready"
CAPTION_CHECK = "caption_check"
AUDIO_QUEUED = "audio_queued"
AWAITING_TRANSCRIPTION = "awaiting_transcription"
TRANSCRIPT_READY = "transcript_ready"
ARTICLE_READY = "article_ready"
VISUAL_PLANNED = "visual_planned"
FRAMES_READY = "frames_ready"
PACKAGE_READY = "package_ready"
WRITTEN = "written"
VERIFIED = "verified"
IGNORED = "ignored"
FAILED = "failed"
DEAD_LETTER = "dead_letter"

ALL_STAGES = (
    DISCOVERED, METADATA_READY, CAPTION_CHECK, AUDIO_QUEUED,
    AWAITING_TRANSCRIPTION, TRANSCRIPT_READY, ARTICLE_READY, VISUAL_PLANNED,
    FRAMES_READY, PACKAGE_READY, WRITTEN, VERIFIED, IGNORED, FAILED, DEAD_LETTER,
)

TERMINAL_STAGES = (VERIFIED, IGNORED, DEAD_LETTER)

# stages from which `failed` may be entered
_FAILABLE = (
    DISCOVERED, METADATA_READY, CAPTION_CHECK, AUDIO_QUEUED,
    AWAITING_TRANSCRIPTION, TRANSCRIPT_READY, ARTICLE_READY, VISUAL_PLANNED,
    FRAMES_READY, PACKAGE_READY, WRITTEN,
)

TRANSITIONS: dict[str, tuple[str, ...]] = {
    DISCOVERED: (METADATA_READY, IGNORED, FAILED),
    METADATA_READY: (CAPTION_CHECK, IGNORED, FAILED),
    CAPTION_CHECK: (TRANSCRIPT_READY, AUDIO_QUEUED, IGNORED, FAILED),
    # AWAITING_TRANSCRIPTION 是 MacWhisper 监视文件夹那条异步路径专属的中间
    # 状态；primary=openai_audio/faster_whisper 是同步直接调用（API 请求或
    # 本地模型跑完就有结果，没有"投稿等回收"这一步），处理完就该直接进
    # TRANSCRIPT_READY——这里之前漏了这条边，pipeline.py 里
    # _openai_transcribe/_local_whisper_transcribe 调用的
    # _set(con, vid, st.TRANSCRIPT_READY) 会被 guard_transition 拒绝、
    # 又被 _set() 自己的 try/except TransitionError 悄悄吞掉（返回 False
    # 但两个调用方都没检查这个返回值），导致视频卡在 audio_queued 不动，
    # 且没有任何报错——写 faster_whisper 的 pipeline 分派测试时才第一次
    # 真正跑通这条路径，暴露出这是个从 openai_audio 那条路径就一直存在、
    # 从来没被测试覆盖到的潜伏 bug。
    AUDIO_QUEUED: (AWAITING_TRANSCRIPTION, TRANSCRIPT_READY, IGNORED, FAILED),
    AWAITING_TRANSCRIPTION: (TRANSCRIPT_READY, IGNORED, FAILED),
    TRANSCRIPT_READY: (ARTICLE_READY, VISUAL_PLANNED, PACKAGE_READY, IGNORED, FAILED),
    ARTICLE_READY: (VISUAL_PLANNED, PACKAGE_READY, IGNORED, FAILED),
    VISUAL_PLANNED: (FRAMES_READY, PACKAGE_READY, FAILED),  # → PACKAGE_READY when no usable frames
    FRAMES_READY: (PACKAGE_READY, FAILED),
    PACKAGE_READY: (WRITTEN, FAILED),
    WRITTEN: (VERIFIED, FAILED),
    VERIFIED: (TRANSCRIPT_READY,),  # 用户主动"重新总结"时回到成文起点
    IGNORED: (DISCOVERED,),  # 用户主动"取消跳过"，回到发现起点重新处理
    FAILED: tuple(set(_FAILABLE)) + (DEAD_LETTER,),  # retry from a safe stage, or give up
    DEAD_LETTER: (DISCOVERED,),  # manual resurrection only
}


class TransitionError(RuntimeError):
    pass


def guard_transition(current: str, new: str) -> None:
    if current not in TRANSITIONS:
        raise TransitionError(f"unknown current stage: {current!r}")
    if new not in ALL_STAGES:
        raise TransitionError(f"unknown target stage: {new!r}")
    if new not in TRANSITIONS[current]:
        raise TransitionError(f"illegal transition {current!r} -> {new!r}")


# --- retry classification (design §12.2) -------------------------------------

RETRY_TRANSIENT = "transient"    # network timeout, 429, 5xx → backoff retry
RETRY_RESOURCE = "resource"      # disk full, MacWhisper not running → pause, human
RETRY_PERMANENT = "permanent"    # private/deleted/geo-blocked → dead_letter
RETRY_DATA = "data"              # empty SRT, bad JSON → switch adapter / human
RETRY_WRITE = "write"            # vault missing, permission → fix then retry

MAX_ATTEMPTS = {RETRY_TRANSIENT: 5, RETRY_DATA: 2, RETRY_WRITE: 3,
                RETRY_RESOURCE: 0, RETRY_PERMANENT: 0}


def backoff_seconds(attempt: int) -> int:
    """Exponential backoff with cap: 1m, 4m, 16m, 64m, 4h."""
    return min(60 * 4 ** (attempt - 1), 4 * 3600)

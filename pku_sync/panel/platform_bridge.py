"""Panel-side platform bridge: activation, quota, and metered quiz calls."""
from __future__ import annotations

import json
import time

from ..platform import PlatformError

FAKE_TRANSCRIBE_SECONDS = 3600
FAKE_LLM_POINTS = 100.0


class RealPlatformBridge:
    def __init__(self, settings):
        self._settings = settings

    def quota(self) -> dict:
        from ..platform import quota
        return quota(self._settings)

    def activate(self, code: str) -> dict:
        from ..platform import activate
        return activate(code, self._settings)

    def quiz(self, prompt: str) -> dict:
        from ..platform import llm
        return llm("quiz", prompt, self._settings)

    def grade(self, prompt: str) -> dict:
        from ..platform import llm
        return llm("grade", prompt, self._settings)


class FakePlatformBridge:
    """Seeded fake with deterministic local metering and no network.

    The ``quiz_fail_status`` / ``grade_fail_status`` / ``grade_fail_once``
    knobs are FAKE-ONLY failure injection for the browser-verification
    variants (organize-blocked / low-balance / grade-502): they default to
    None and are inert unless a ``--fake`` variant opt into them. The real
    relay bridge (``RealPlatformBridge``) never sees them.
    """

    def __init__(self, activated: bool = False, *, llm_points: float = FAKE_LLM_POINTS,
                 points_charged: float = 3.0, grade_delay: float = 0.0,
                 grade_with_wrong_answer: bool = False,
                 quiz_fail_status: int | None = None,
                 grade_fail_status: int | None = None,
                 grade_fail_once: int | None = None):
        self._activated = bool(activated)
        self._llm_points = float(llm_points)
        self._points_charged = float(points_charged)
        self._grade_delay = float(grade_delay)
        self._grade_with_wrong_answer = bool(grade_with_wrong_answer)
        self._quiz_fail_status = quiz_fail_status
        self._grade_fail_status = grade_fail_status
        self._grade_fail_once = grade_fail_once
        self.llm_calls: list[dict] = []

    @staticmethod
    def _relay_error(message: str, status: int) -> PlatformError:
        error = PlatformError(message)
        error.status_code = status
        return error

    def quota(self) -> dict:
        if not self._activated:
            return {"active": False}
        return {"active": True, "available": True,
                "transcribe_seconds_remaining": FAKE_TRANSCRIBE_SECONDS,
                "llm_points_remaining": self._llm_points}

    def activate(self, code: str) -> dict:
        if not (code or "").strip():
            raise PlatformError("请输入兑换码")
        self._activated = True
        return {"transcribe_seconds_remaining": FAKE_TRANSCRIBE_SECONDS}

    def quiz(self, prompt: str) -> dict:
        if not self._activated:
            raise PlatformError("云端登录已失效，请重新激活后重试。")
        self.llm_calls.append({"operation": "quiz"})
        if self._quiz_fail_status is not None:
            # fake-only organize failure injection (organize-blocked variant)
            raise self._relay_error(
                "AI 服务暂时没有完成整理，请稍后重试。", self._quiz_fail_status
            )
        # Relay pre-check: remaining < charge → 402, no upstream call,
        # no partial decrement.
        if self._llm_points < self._points_charged:
            raise self._relay_error("AI 点不足，请兑换新额度后重试。", 402)
        self._llm_points -= self._points_charged
        return {"content": json.dumps({"title": "第一讲 · 计算机网络练习", "questions": [
            {"type": "选择", "question": "下列哪项属于计算机网络的核心功能？A. 分层通信 B. 单机计算 C. 文件压缩 D. 图像渲染", "source": "第一讲", "answer": "A"},
            {"type": "判断", "question": "分层设计可以降低网络协议之间的耦合。", "source": "第一讲", "answer": "正确"},
            {"type": "填空", "question": "计算机网络采用分层模型，将复杂通信拆分为相互协作的____。", "source": "第一讲", "answer": "协议层"},
            {"type": "简答", "question": "请简要说明分层模型对网络协议设计的帮助。", "source": "第一讲", "answer": "分层模型通过明确每层职责和接口，隔离实现变化并简化协议设计与维护。"},
            {"type": "论述", "question": "结合端到端通信，论述分层模型如何在模块化、互操作性和故障定位之间取得平衡。", "source": "第一讲", "answer": "分层模型以清晰接口实现模块化，以统一协议促进互操作性，并通过逐层检查缩小故障定位范围；同时需要控制层间开销，避免职责重复。"},
        ]}, ensure_ascii=False), "points_charged": self._points_charged}

    def grade(self, prompt: str) -> dict:
        if not self._activated:
            raise PlatformError("云端登录已失效，请重新激活后重试。")
        self.llm_calls.append({"operation": "grade"})
        # Relay pre-check: remaining < charge → 402, no upstream call,
        # no partial decrement.
        if self._llm_points < self._points_charged:
            raise self._relay_error("AI 点不足，请兑换新额度后重试。", 402)
        if self._grade_fail_once is not None:
            # fake-only one-shot upstream failure (grade-502 variant)
            status = self._grade_fail_once
            self._grade_fail_once = None
            raise self._relay_error("AI 服务暂时没有完成请求，请稍后重试。", status)
        if self._grade_fail_status is not None:
            raise self._relay_error(
                "AI 服务暂时没有完成请求，请稍后重试。", self._grade_fail_status
            )
        if self._grade_delay:
            time.sleep(self._grade_delay)
        self._llm_points -= self._points_charged
        return {
            "content": json.dumps(
                {"score": 88, "questions": [{"number": 1, "type": "选择", "score": 0, "max_score": 2}]}
                if self._grade_with_wrong_answer else {"score": 88, "details": []},
                ensure_ascii=False,
            ),
            "points_charged": self._points_charged,
        }


__all__ = ["FAKE_TRANSCRIBE_SECONDS", "FAKE_LLM_POINTS", "RealPlatformBridge",
           "FakePlatformBridge"]

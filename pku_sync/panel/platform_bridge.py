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
    """Seeded fake with deterministic local metering and no network."""

    def __init__(self, activated: bool = False, *, llm_points: float = FAKE_LLM_POINTS,
                 points_charged: float = 3.0, grade_delay: float = 0.0):
        self._activated = bool(activated)
        self._llm_points = float(llm_points)
        self._points_charged = float(points_charged)
        self._grade_delay = float(grade_delay)
        self.llm_calls: list[dict] = []

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
        self._llm_points -= self._points_charged
        return {"content": json.dumps({"title": "第一讲 · 计算机网络练习", "questions": [
            {"type": "选择", "question": "练习题一", "source": "第一讲", "answer": "A"},
            {"type": "判断", "question": "练习题二", "source": "第一讲", "answer": "正确"},
            {"type": "填空", "question": "练习题三", "source": "第一讲", "answer": "分层"},
        ]}, ensure_ascii=False), "points_charged": self._points_charged}

    def grade(self, prompt: str) -> dict:
        if not self._activated:
            raise PlatformError("云端登录已失效，请重新激活后重试。")
        self.llm_calls.append({"operation": "grade"})
        if self._grade_delay:
            time.sleep(self._grade_delay)
        self._llm_points -= self._points_charged
        return {
            "content": json.dumps({"score": 88, "details": []}, ensure_ascii=False),
            "points_charged": self._points_charged,
        }


__all__ = ["FAKE_TRANSCRIBE_SECONDS", "FAKE_LLM_POINTS", "RealPlatformBridge",
           "FakePlatformBridge"]

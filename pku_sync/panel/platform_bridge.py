"""Panel-side platform bridge: quota read + code activation.

Injectable so the seeded-fake startup mode (``pku-sync panel --fake``) can
drive the activation screen with zero network calls — a browser-verification
run must never be able to reach the operated relay.
"""

from __future__ import annotations

from ..platform import PlatformError

FAKE_TRANSCRIBE_SECONDS = 3600
FAKE_LLM_POINTS = 100.0


class RealPlatformBridge:
    """Production bridge: the relay endpoints via ``pku_sync.platform``."""

    def __init__(self, settings):
        self._settings = settings

    def quota(self) -> dict:
        from ..platform import quota

        return quota(self._settings)

    def activate(self, code: str) -> dict:
        from ..platform import activate

        return activate(code, self._settings)


class FakePlatformBridge:
    """Seeded-fake bridge: local activation state, no relay contact at all."""

    def __init__(self, activated: bool = False):
        self._activated = bool(activated)

    def quota(self) -> dict:
        if not self._activated:
            return {"active": False}
        return {
            "active": True,
            "available": True,
            "transcribe_seconds_remaining": FAKE_TRANSCRIBE_SECONDS,
            "llm_points_remaining": FAKE_LLM_POINTS,
        }

    def activate(self, code: str) -> dict:
        if not (code or "").strip():
            raise PlatformError("请输入兑换码")
        self._activated = True
        return {"transcribe_seconds_remaining": FAKE_TRANSCRIBE_SECONDS}


__all__ = [
    "FAKE_TRANSCRIBE_SECONDS",
    "FAKE_LLM_POINTS",
    "RealPlatformBridge",
    "FakePlatformBridge",
]

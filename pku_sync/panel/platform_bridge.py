"""Panel-side platform bridge: activation, quota, and metered quiz calls."""
from __future__ import annotations

import json
import time

from ..platform import PlatformError

FAKE_TRANSCRIBE_SECONDS = 3600
FAKE_LLM_POINTS = 100.0
FAKE_AUTH_EMAIL = "demo@example.com"

class RealPlatformBridge:
    durable_metered_jobs = True
    operation_context_supported = True

    def __init__(self, settings):
        self._settings = settings
        self._profile_synced = False

    def quota(self) -> dict:
        from ..platform import quota
        return quota(self._settings)

    def activate(self, code: str) -> dict:
        from ..platform import activate
        return activate(code, self._settings)

    def redeem(self, code: str) -> dict:
        from ..platform import redeem
        return redeem(code, self._settings)

    def exchange_quota(self, hours: int = 1, *, target: str = "advanced") -> dict:
        from ..platform import exchange_quota
        return exchange_quota(hours, self._settings, target=target)

    def bind_campus(self, student_id: str) -> dict:
        from ..platform import bind_campus
        return bind_campus(student_id, self._settings)

    def register(self, email: str, password: str, *, invite_code: str = "") -> dict:
        from ..platform import register
        return register(email, password, self._settings, invite_code=invite_code)

    def login(self, email: str, password: str) -> dict:
        from ..platform import login, sync_account_profile

        result = login(email, password, self._settings)
        result["profile_sync"] = sync_account_profile(self._settings)
        result["catalog_sync"] = self._sync_catalog()
        self._profile_synced = True
        return result

    def logout(self) -> dict:
        from ..platform import logout

        result = logout(self._settings)
        from ..catalog_snapshots import activate_account
        activate_account(self._settings)
        self._profile_synced = False
        return result

    def me(self) -> dict:
        from ..platform import auth_me, sync_account_profile

        result = auth_me(self._settings)
        if (
            not self._profile_synced
            and result.get("active")
            and result.get("available")
            and result.get("email_verified")
        ):
            result["profile_sync"] = sync_account_profile(self._settings)
            result["catalog_sync"] = self._sync_catalog()
            self._profile_synced = True
        return result

    def _sync_catalog(self) -> dict:
        from .. import catalog_snapshots as catalog
        try:
            return catalog.pull(self._settings)
        except catalog.CatalogUnavailable:
            return {"available": False, "status": "read_only"}

    def forgot_password(self, email: str) -> dict:
        from ..platform import forgot_password
        return forgot_password(email, self._settings)

    def resend_verification(self, email: str) -> dict:
        from ..platform import resend_verification
        return resend_verification(email, self._settings)

    def quiz(self, prompt: str, *, operation_context=None) -> dict:
        from ..platform import llm
        return llm("quiz", prompt, self._settings, operation_context=operation_context)

    def grade(self, prompt: str, *, operation_context=None) -> dict:
        from ..platform import llm
        return llm("grade", prompt, self._settings, operation_context=operation_context)

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
                 grade_fail_once: int | None = None,
                 email: str = "",
                 email_verified: bool = False,
                 tier: str = "pro",
                 basic_seconds: int = FAKE_TRANSCRIBE_SECONDS // 2,
                 advanced_seconds: int = FAKE_TRANSCRIBE_SECONDS - (FAKE_TRANSCRIBE_SECONDS // 2)):
        self._activated = bool(activated)
        self._llm_points = float(llm_points)
        self._points_charged = float(points_charged)
        self._grade_delay = float(grade_delay)
        self._grade_with_wrong_answer = bool(grade_with_wrong_answer)
        self._quiz_fail_status = quiz_fail_status
        self._grade_fail_status = grade_fail_status
        self._grade_fail_once = grade_fail_once
        self._email = (email or "").strip().lower()
        self._email_verified = bool(email_verified)
        self._password = "password1"
        self._tier = (tier or "pro").strip().lower()
        self._basic_seconds = int(basic_seconds)
        self._advanced_seconds = int(advanced_seconds)
        self._weekly_rollover_seconds = 90000
        self._invite_code = "PKU-DEMO88"
        self._invited_by = ""
        self._invited_count = 0
        self._campus_bound = False
        self._invite_rewarded = False
        self._rewarded_students: set[str] = set()
        self.llm_calls: list[dict] = []
        self.auth_calls: list[dict] = []
        if self._activated and not self._email:
            # Fake panel defaults: logged-in verified product account.
            self._email = FAKE_AUTH_EMAIL
            self._email_verified = True

    @staticmethod
    def _relay_error(message: str, status: int) -> PlatformError:
        error = PlatformError(message)
        error.status_code = status
        return error

    def _require_verified_for_metered(self) -> None:
        if self._email and not self._email_verified:
            raise self._relay_error(
                "请先完成邮箱验证后再使用转写、AI 或兑换额度", 403
            )

    def _total_seconds(self) -> int:
        return max(0, int(self._basic_seconds) + int(self._advanced_seconds))

    def quota(self) -> dict:
        if not self._activated:
            return {"active": False}
        result = {
            "active": True,
            "available": True,
            "transcribe_seconds_remaining": self._total_seconds(),
            "basic_seconds_remaining": self._basic_seconds,
            "advanced_seconds_remaining": self._advanced_seconds,
            "weekly_rollover_seconds": self._weekly_rollover_seconds,
            "llm_points_remaining": self._llm_points,
            "tier": self._tier,
            "invite_code": self._invite_code,
            "invited_count": self._invited_count,
            "campus_bound": self._campus_bound,
        }
        if self._email:
            result["email"] = self._email
            result["email_verified"] = self._email_verified
            result["legacy_activate"] = False
        else:
            result["email"] = None
            result["email_verified"] = True
            result["legacy_activate"] = True
        return result

    def activate(self, code: str) -> dict:
        if not (code or "").strip():
            raise PlatformError("请输入兑换码")
        self._activated = True
        self._email = ""
        self._email_verified = True
        return {"transcribe_seconds_remaining": self._total_seconds()}

    def redeem(self, code: str) -> dict:
        from ..platform import _extract_code_from_input
        clean = _extract_code_from_input(code)
        if not clean:
            raise PlatformError("请输入兑换码")
        if not self._activated:
            raise PlatformError("请先登录后再兑换额度")
        self._require_verified_for_metered()
        self.auth_calls.append({"op": "redeem", "code": clean})
        if clean.upper().startswith("PKU-") or clean.upper().startswith("INV"):
            self._invited_by = clean
            return {
                "transcribe_seconds_remaining": self._total_seconds(),
                "basic_seconds_remaining": self._basic_seconds,
                "advanced_seconds_remaining": self._advanced_seconds,
                "llm_points_remaining": self._llm_points,
                "tier": self._tier,
                "kind": "invite",
                "invite_bound": True,
            }
        return {
            "transcribe_seconds_remaining": self._total_seconds(),
            "basic_seconds_remaining": self._basic_seconds,
            "advanced_seconds_remaining": self._advanced_seconds,
            "llm_points_remaining": self._llm_points,
            "tier": self._tier,
            "kind": "gift",
        }

    def exchange_quota(self, hours: int = 1, *, target: str = "advanced") -> dict:
        if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= 100:
            raise self._relay_error("兑换小时数必须是正整数", 400)
        clean_target = (target or "advanced").strip().lower()
        if clean_target not in {"basic", "advanced"}:
            raise self._relay_error("请选择兑换普通转写或高级转写时长", 400)
        if not self._activated:
            raise self._relay_error("请先登录后再兑换时长", 401)
        self._require_verified_for_metered()
        cost = float(hours * 10)
        if self._llm_points < cost:
            raise self._relay_error("AI 点不足（10 AI 点可兑换 1 小时转写时长）", 402)
        self._llm_points -= cost
        added = hours * 3600
        if clean_target == "basic":
            self._basic_seconds += added
        else:
            self._advanced_seconds += added
        self.auth_calls.append({"op": "exchange", "hours": hours, "target": clean_target})
        return {
            "transcribe_seconds_remaining": self._total_seconds(),
            "basic_seconds_remaining": self._basic_seconds,
            "advanced_seconds_remaining": self._advanced_seconds,
            "llm_points_remaining": self._llm_points,
            "points_spent": cost,
            "seconds_added": added,
            "target": clean_target,
            "tier": self._tier,
        }

    def bind_campus(self, student_id: str) -> dict:
        clean_id = (student_id or "").strip()
        if not clean_id:
            raise self._relay_error("请输入教学网账号（学号）", 400)
        if not self._activated:
            return {"bound": False, "reason": "not_signed_in"}
        self._campus_bound = True
        reward_granted = False
        if self._invited_by and not self._invite_rewarded and clean_id not in self._rewarded_students:
            self._invite_rewarded = True
            self._rewarded_students.add(clean_id)
            self._basic_seconds += 90000  # First week 25h basic transcription
            self._invited_count += 1
            reward_granted = True
        self.auth_calls.append({"op": "bind_campus", "student_id": clean_id, "reward_granted": reward_granted})
        return {
            "bound": True,
            "campus_bound": True,
            "reward_granted": reward_granted,
            "transcribe_seconds_remaining": self._total_seconds(),
            "basic_seconds_remaining": self._basic_seconds,
            "advanced_seconds_remaining": self._advanced_seconds,
            "tier": self._tier,
        }

    def register(self, email: str, password: str, *, invite_code: str = "") -> dict:
        from ..platform import _extract_code_from_input
        clean = (email or "").strip().lower()
        if not clean or "@" not in clean:
            raise self._relay_error("请输入有效的邮箱地址", 400)
        if not isinstance(password, str) or len(password) < 8:
            raise self._relay_error("密码至少 8 位", 400)
        if self._email == clean and self._activated:
            raise self._relay_error("该邮箱已注册", 409)
        self._email = clean
        self._password = password
        self._email_verified = False
        self._activated = True
        clean_invite = _extract_code_from_input(invite_code)
        if clean_invite:
            self._invited_by = clean_invite
        entry = {"op": "register", "email": clean}
        if clean_invite:
            entry["invite_code"] = clean_invite
        self.auth_calls.append(entry)
        result = {"email": clean, "email_verified": False, "active": True}
        if clean_invite:
            result["invited_by"] = clean_invite
        return result

    def login(self, email: str, password: str) -> dict:
        clean = (email or "").strip().lower()
        if clean != self._email or password != self._password:
            # Constant-ish copy matching the relay.
            if not self._email:
                self._email = clean
                self._password = password
                self._email_verified = False
            else:
                raise self._relay_error("邮箱或密码不正确", 401)
        self._activated = True
        self.auth_calls.append({"op": "login", "email": clean})
        return {
            "email": self._email,
            "email_verified": self._email_verified,
            "active": True,
        }

    def logout(self) -> dict:
        self.auth_calls.append({"op": "logout"})
        self._activated = False
        return {"ok": True}

    def me(self) -> dict:
        if not self._activated:
            return {"active": False}
        return {
            "active": True,
            "available": True,
            "email": self._email or None,
            "email_verified": self._email_verified if self._email else True,
            "legacy_activate": not bool(self._email),
            "transcribe_seconds_remaining": self._total_seconds(),
            "basic_seconds_remaining": self._basic_seconds,
            "advanced_seconds_remaining": self._advanced_seconds,
            "weekly_rollover_seconds": self._weekly_rollover_seconds,
            "llm_points_remaining": self._llm_points,
            "tier": self._tier,
            "invite_code": self._invite_code,
            "invited_count": self._invited_count,
            "campus_bound": self._campus_bound,
        }

    def forgot_password(self, email: str) -> dict:
        clean = (email or "").strip().lower()
        if not clean or "@" not in clean:
            raise self._relay_error("请输入有效的邮箱地址", 400)
        self.auth_calls.append({"op": "forgot", "email": clean})
        return {"ok": True}

    def resend_verification(self, email: str) -> dict:
        clean = (email or "").strip().lower()
        if not clean or "@" not in clean:
            raise self._relay_error("请输入有效的邮箱地址", 400)
        self.auth_calls.append({"op": "resend", "email": clean})
        return {"ok": True}

    def mark_verified(self) -> None:
        """Fake-only helper for tests that need a verified product account."""
        self._email_verified = True

    def quiz(self, prompt: str) -> dict:
        if not self._activated:
            raise PlatformError("云端登录已失效，请重新激活后重试。")
        self._require_verified_for_metered()
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
        self._require_verified_for_metered()
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

__all__ = ["FAKE_TRANSCRIBE_SECONDS", "FAKE_LLM_POINTS", "FAKE_AUTH_EMAIL",
           "RealPlatformBridge", "FakePlatformBridge"]

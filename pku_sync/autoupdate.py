"""Notify-only GitHub Releases update checks and next-start tool upgrades."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as distribution_version
from pathlib import Path
from typing import Callable

import httpx

GITHUB_RELEASE_URL = (
    "https://api.github.com/repos/Suiseiseki-2016/PKU-All-in-Notion/releases/latest"
)
UPDATE_TIMEOUT_SECONDS = 5.0
UPGRADE_TIMEOUT_SECONDS = 300.0
TOOL_NAME = "pku-course-sync"
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class UpdateCheck:
    current_version: str
    available_version: str | None
    status: str


@dataclass(frozen=True)
class ApplyOutcome:
    status: str
    version: str | None


def installed_version() -> str:
    """Read the installed package metadata without contacting a service."""
    try:
        return distribution_version(TOOL_NAME)
    except PackageNotFoundError:
        return "0.0.0"


def _normalized_version(raw: object) -> str | None:
    value = str(raw or "").strip().lstrip("v")
    parts = value.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        return None
    return ".".join(str(int(part)) for part in parts)


def _is_newer(candidate: str, current: str) -> bool:
    left = tuple(int(part) for part in candidate.split("."))
    right = tuple(int(part) for part in current.split("."))
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) > right + (0,) * (width - len(right))


def fetch_release_version() -> str:
    """Read the public GitHub Releases manifest with a bounded request."""
    response = httpx.get(
        GITHUB_RELEASE_URL,
        headers={"Accept": "application/vnd.github+json"},
        timeout=UPDATE_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    release = _normalized_version(response.json().get("tag_name"))
    if release is None:
        raise ValueError("release manifest has no valid tag_name")
    return release


class UpdateService:
    """State is local metadata only, never an in-process or silent update."""

    def __init__(
        self,
        *,
        current_version: str | None = None,
        state_path: Path,
        fetcher: Callable[[], str] = fetch_release_version,
        upgrade_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ):
        self.current_version = _normalized_version(current_version or installed_version()) or "0.0.0"
        self.state_path = state_path
        self._fetcher = fetcher
        self._upgrade_runner = upgrade_runner
        self._last_check: UpdateCheck | None = None

    @classmethod
    def from_settings(cls, settings, **kwargs) -> UpdateService:
        return cls(
            state_path=Path(settings.data_dir) / "update-pending.json",
            **kwargs,
        )

    def check(self) -> UpdateCheck:
        """Return availability, while an unreachable manifest remains invisible."""
        try:
            available = _normalized_version(self._fetcher())
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            result = UpdateCheck(self.current_version, None, "unavailable")
        else:
            newer = bool(available) and _is_newer(available, self.current_version)
            result = UpdateCheck(
                self.current_version,
                available if newer else None,
                "available" if newer else "up_to_date",
            )
        self._last_check = result
        return result

    def request_apply(self, available_version: str) -> None:
        """Queue an explicit user-requested update for a future process start."""
        clean = _normalized_version(available_version)
        if clean is None:
            raise ValueError("invalid update version")
        pending = self._read_state()
        offered = (
            self._last_check.available_version
            if self._last_check is not None
            else _normalized_version(pending.get("version"))
        )
        if clean != offered:
            raise ValueError("update version was not offered")
        self._write_state({"status": "requested", "version": clean})
        LOG.info("Update %s requested; it will apply on the next start.", clean)

    def apply_pending(self) -> ApplyOutcome:
        """Run the tool upgrade only during startup, preserving a failed request."""
        pending = self._read_state()
        if pending.get("status") != "requested":
            return ApplyOutcome(pending.get("status", "none"), pending.get("version"))
        requested = _normalized_version(pending.get("version"))
        if requested is None:
            self.state_path.unlink(missing_ok=True)
            return ApplyOutcome("none", None)
        try:
            self._upgrade_runner(
                ["uv", "tool", "upgrade", TOOL_NAME],
                check=True,
                timeout=UPGRADE_TIMEOUT_SECONDS,
            )
        except (subprocess.SubprocessError, OSError, KeyboardInterrupt):
            self._write_state({"status": "deferred", "version": requested})
            LOG.warning(
                "Update %s deferred after upgrade failure; the current version remains runnable.",
                requested,
            )
            return ApplyOutcome("deferred", requested)
        self.state_path.unlink(missing_ok=True)
        self.current_version = installed_version()
        LOG.info("Update %s applied; the restarted panel will report the installed version.", requested)
        return ApplyOutcome("applied", requested)

    def panel_state(self, *, check: bool = False) -> dict:
        """Safe browser payload, deliberately omitting error details and commands."""
        pending = self._read_state()
        if pending.get("status") in {"deferred", "requested"}:
            available = _normalized_version(pending.get("version"))
            if available is None:
                self.state_path.unlink(missing_ok=True)
            else:
                return {
                    "version": self.current_version,
                    "status": pending["status"],
                    "available_version": available,
                }
        result = self.check() if check or self._last_check is None else self._last_check
        payload = {"version": self.current_version, "status": result.status}
        if result.available_version:
            payload["available_version"] = result.available_version
        return payload

    def _read_state(self) -> dict:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_state(self, payload: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(self.state_path)


__all__ = [
    "ApplyOutcome",
    "GITHUB_RELEASE_URL",
    "TOOL_NAME",
    "UPDATE_TIMEOUT_SECONDS",
    "UPGRADE_TIMEOUT_SECONDS",
    "UpdateCheck",
    "UpdateService",
    "fetch_release_version",
    "installed_version",
]

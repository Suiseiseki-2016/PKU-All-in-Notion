"""Notion connection state for the student panel (VAL-META-020).

The panel surfaces three things about the student's Notion workspace:

- the connection state with the approved copy (已连接 / 已断开);
- 撤销连接, which CLEARS the local Notion token (from the gitignored .env and
  from the live settings) and drops the cached directory, so a disconnected
  panel can never render the index it read while connected;
- 重新连接, which runs the relay-mediated OAuth login again.

The token itself is never part of any state payload, log line, or error
message: the state is a boolean derived from the local settings, and a
failed reconnect reports the pinned generic copy only. Reconnect is
single-flight: a second trigger while one flight runs is refused with the
pinned busy message (the panel turns that into HTTP 409).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Protocol

logger = logging.getLogger(__name__)

STATE_CONNECTED = "connected"
STATE_DISCONNECTED = "disconnected"
STATE_CONNECTING = "connecting"
CONNECTION_STATES = (STATE_CONNECTED, STATE_DISCONNECTED, STATE_CONNECTING)

# Approved prototype copy (docs/UI_DESIGN.html onboarding connection row).
CONNECTED_COPY = "内容将同步至你的空间 · 已连接"
DISCONNECTED_COPY = "尚未连接 · 点击连接 Notion 开始"
REVOKE_LABEL = "撤销连接"
RECONNECT_LABEL = "连接 Notion"
DISCONNECT_TOAST = "已断开 Notion 连接，可随时重新连接。"
RECONNECT_TOAST = "已重新连接 Notion 学习空间。"

# In-flight and failure copy for the panel-triggered reconnect. The real flow
# waits for a browser authorization, so the panel needs an explicit in-flight
# label the prototype (which fakes the flip) does not carry.
CONNECTING_COPY = "正在连接 Notion · 请在浏览器中完成授权。"
CONNECT_BUSY_MESSAGE = "正在连接 Notion，请稍候。"
CONNECT_FAILED_COPY = "连接 Notion 没有成功，请稍后重试。"
CONNECT_SERVICE_UNAVAILABLE_COPY = "云端 Notion 授权服务尚未配置，请联系管理员处理后再试。"

# The dedicated disconnected directory state: distinct from the sync error
# and empty states (library/notion-workspace.md rule 10).
DISCONNECTED_DIRECTORY_TITLE = "Notion 尚未连接"
DISCONNECTED_DIRECTORY_COPY = (
    "连接 Notion 后，这里会显示讲次与资料索引。"
)
DISCONNECTED_DIRECTORY_ACTION = "连接 Notion"


class ConnectionBusy(RuntimeError):
    """A reconnect is already in flight (single-flight refusal)."""

    def __init__(self, message: str = CONNECT_BUSY_MESSAGE):
        super().__init__(message)
        self.message = message


class ConnectionProvider(Protocol):
    instant_connect: bool

    def connected(self) -> bool:
        """Whether a local Notion credential is present (never its value)."""
        ...

    def disconnect(self) -> None:
        """Clear the local credential."""
        ...

    def connect(self) -> None:
        """Acquire a credential again; raises on failure."""
        ...


class ConnectionService:
    """Connection state plus the single-flight reconnect flight."""

    def __init__(
        self,
        provider: ConnectionProvider,
        *,
        on_disconnect: Callable[[], None] | None = None,
    ):
        self.provider = provider
        self._on_disconnect = on_disconnect
        self._lock = threading.Lock()
        self._connecting = False
        self._error: str | None = None

    def snapshot(self) -> dict:
        """The connection payload: state + approved label, never a token."""
        with self._lock:
            connecting = self._connecting
            error = self._error
        connected = bool(self.provider.connected())
        if connected:
            state = STATE_CONNECTED
            label = CONNECTED_COPY
        elif connecting:
            state = STATE_CONNECTING
            label = CONNECTING_COPY
        else:
            state = STATE_DISCONNECTED
            label = DISCONNECTED_COPY
        return {
            "state": state,
            "connected": connected,
            "status_label": label,
            "error": None if connected else error,
        }

    def disconnect(self) -> dict:
        self.provider.disconnect()
        with self._lock:
            self._error = None
        if self._on_disconnect is not None:
            # a disconnected panel must not serve the index it read while
            # connected: drop the cached directory with the credential
            self._on_disconnect()
        return self.snapshot()

    def connect(self) -> dict:
        """Start (or run) one reconnect flight; refuses a concurrent one."""
        with self._lock:
            if self._connecting:
                raise ConnectionBusy()
            self._connecting = True
            self._error = None
        if getattr(self.provider, "instant_connect", False):
            self._run_connect()
        else:
            threading.Thread(target=self._run_connect, daemon=True).start()
        return self.snapshot()

    def _run_connect(self) -> None:
        try:
            self.provider.connect()
        except Exception as exc:  # noqa: BLE001 - every failure is one safe state
            status = getattr(exc, "status", None)
            with self._lock:
                self._error = (
                    CONNECT_SERVICE_UNAVAILABLE_COPY if status == 503
                    else CONNECT_FAILED_COPY
                )
            # Never log the exception message: a provider response could quote
            # the single-use authorization code. Class and HTTP status suffice.
            logger.warning(
                "panel notion connect failed: type=%s status=%s",
                type(exc).__name__, status if isinstance(status, int) else "none",
            )
        finally:
            with self._lock:
                self._connecting = False


class RealConnectionProvider:
    """Production provider: the local .env token and the relay OAuth login.

    ``instant_connect`` is False because the real flow waits for the student
    to authorize in a browser; the service therefore runs it in a background
    thread and the panel polls the state.
    """

    instant_connect = False

    def __init__(
        self,
        settings,
        *,
        env_path: Path | None = None,
        login: Callable[..., dict] | None = None,
    ):
        self._settings = settings
        self._env_path = env_path
        self._login = login

    def _env(self) -> Path:
        if self._env_path is not None:
            return Path(self._env_path)
        from ..notion_login import env_file_path

        return env_file_path(self._settings)

    def connected(self) -> bool:
        return bool((getattr(self._settings, "notion_token", "") or "").strip())

    def disconnect(self) -> None:
        from ..envfile import write_env_values

        # emptied rather than deleted: the key stays visible in the .env so a
        # later login upserts it in place
        write_env_values(self._env(), {"NOTION_TOKEN": ""})
        self._settings.notion_token = ""

    def connect(self) -> None:
        login = self._login
        if login is None:
            from ..notion_login import login_via_relay as login
        token_info = login(self._settings, open_browser=True)
        token = (token_info or {}).get("access_token", "")
        if not token:
            raise RuntimeError("relay exchange returned no access token")
        # the login already wrote the .env; the live settings must agree so
        # the panel state flips without a restart
        self._settings.notion_token = token
        template_id = (token_info or {}).get("duplicated_template_id")
        if template_id:
            from .notion_home import adopt_oauth_template
            try:
                adopt_oauth_template(Path(self._settings.data_dir), token, template_id)
            except Exception:
                logger.exception("Notion OAuth template adoption failed")
        from ..platform import sync_account_profile

        sync_account_profile(self._settings)


class FakeConnectionProvider:
    """Seeded-fake provider for ``pku-sync panel --fake``.

    Toggles in memory: no OAuth flow, no network, no .env write — so browser
    verification can drive both connection states without touching the
    student's real credentials.
    """

    instant_connect = True

    def __init__(self, connected: bool = True):
        self._connected = bool(connected)

    def connected(self) -> bool:
        return self._connected

    def disconnect(self) -> None:
        self._connected = False

    def connect(self) -> None:
        self._connected = True


def make_connection_service(
    settings=None,
    *,
    env_path: Path | None = None,
    login: Callable[..., dict] | None = None,
    directory_service=None,
) -> ConnectionService:
    """The production connection service (local .env + relay OAuth login)."""
    if settings is None:
        from ..config import settings as default_settings

        settings = default_settings
    provider = RealConnectionProvider(settings, env_path=env_path, login=login)
    return ConnectionService(
        provider,
        on_disconnect=getattr(directory_service, "invalidate", None),
    )


def build_fake_connection_service(
    *, connected: bool = True, directory_service=None
) -> ConnectionService:
    """The seeded-fake connection service used by ``panel --fake``."""
    return ConnectionService(
        FakeConnectionProvider(connected=connected),
        on_disconnect=getattr(directory_service, "invalidate", None),
    )


__all__ = [
    "STATE_CONNECTED",
    "STATE_DISCONNECTED",
    "STATE_CONNECTING",
    "CONNECTION_STATES",
    "CONNECTED_COPY",
    "DISCONNECTED_COPY",
    "REVOKE_LABEL",
    "RECONNECT_LABEL",
    "DISCONNECT_TOAST",
    "RECONNECT_TOAST",
    "CONNECTING_COPY",
    "CONNECT_BUSY_MESSAGE",
    "CONNECT_FAILED_COPY",
    "CONNECT_SERVICE_UNAVAILABLE_COPY",
    "DISCONNECTED_DIRECTORY_TITLE",
    "DISCONNECTED_DIRECTORY_COPY",
    "DISCONNECTED_DIRECTORY_ACTION",
    "ConnectionBusy",
    "ConnectionProvider",
    "ConnectionService",
    "RealConnectionProvider",
    "FakeConnectionProvider",
    "make_connection_service",
    "build_fake_connection_service",
]

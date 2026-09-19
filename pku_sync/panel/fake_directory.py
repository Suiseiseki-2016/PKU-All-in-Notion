"""Seeded-fake startup mode for the student panel (reusable M3 fixture).

Every later browser-verified M3 panel feature starts the panel against one of
these variants INSTEAD of re-deriving fake data:

- ``normal``       — the seeded workspace, honest instant responses.
- ``slow``         — same seeds with a per-call delay so the syncing/loading
                     states are reachable in the browser.
- ``fault``        — a second same-semester hub makes discovery ambiguous:
                     every directory read lands in the sync error state
                     (VAL-META-018 honest failure).
- ``fault-launch`` — the directory loads normally but launching the flagged
                     lecture id fails with the generic 打开没有成功 copy
                     (the recoverable launch-failure state).

The launch path stays identity-only in every variant: resolution uses only
the stored directory, and no variant ever performs a workspace search.
"""

from __future__ import annotations

from ..notion_meta import DirectoryData, LaunchResolver, LaunchResult, NotionDirectory
from ..notion_meta.errors import NotionRetryableError
from .directory import (
    DirectoryService,
    LAUNCH_FAILED,
)
from .fake_workspace import (
    AMBIG_HUB,
    LEARNING_CENTER,
    NOTES_HUB,
    NOISE_PAGE,
    NET_L1,
    build_panel_workspace,
    search_page,
)

# The seeded semester matching the hub title (mirrors the real workspace).
PANEL_FAKE_SEMESTER = "2026 下半学期"

# The lecture whose launch fails under the ``fault-launch`` variant.
FAULT_LAUNCH_TARGET = NET_L1

# The slow variant's per-call delay (a full directory read is ~3s then).
SLOW_DELAY = 0.25

_FAKE_VARIANTS = (
    "normal",
    "slow",
    "fault",
    "fault-launch",
    "empty",
    "usage-cap",
)


class PanelDirectoryProvider:
    """Runs the REAL M2 adapter against a seeded fake client."""

    def __init__(self, workspace, *, semester: str = PANEL_FAKE_SEMESTER, delay: float = 0.0):
        from .fake_workspace import PanelFakeClient

        self.client = PanelFakeClient(workspace, delay=delay)
        self._directory = NotionDirectory(self.client, semester=semester)

    def load(self) -> DirectoryData:
        return self._directory.load()

    def resolve_launch(
        self, data: DirectoryData, target_id: str, *, course_id: str | None = None
    ) -> LaunchResult:
        return LaunchResolver(data).resolve(target_id, course_id=course_id)


class FaultLaunchProvider(PanelDirectoryProvider):
    """Variant where one flagged lecture always fails to open."""

    def resolve_launch(
        self, data: DirectoryData, target_id: str, *, course_id: str | None = None
    ) -> LaunchResult:
        raw = (target_id or "").strip()
        if raw in (FAULT_LAUNCH_TARGET, FAULT_LAUNCH_TARGET.replace("-", "")):
            return LaunchResult(
                status=LAUNCH_FAILED,
                target_id=raw,
                url=None,
                fallback=None,
            )
        return super().resolve_launch(data, target_id, course_id=course_id)


class UsageCapProvider(PanelDirectoryProvider):
    """Variant matching a retryable Notion usage-cap/read failure."""

    def load(self) -> DirectoryData:
        raise NotionRetryableError(status=429)


def build_fake_directory(
    variant: str = "normal",
    *,
    workspace=None,
    semester: str = PANEL_FAKE_SEMESTER,
    slow_delay: float = SLOW_DELAY,
    clock=None,
) -> DirectoryService:
    """Build the seeded-fake DirectoryService for one startup variant.

    ``workspace`` overrides the seeded one (tests use custom shapes);
    every variant still wraps a real ``NotionDirectory`` so the adapter's
    metadata-only and association rules are exercised exactly as production.
    """
    if variant not in _FAKE_VARIANTS:
        raise ValueError(
            f"未知的 --fake-variant：{variant!r}（支持 {'/'.join(_FAKE_VARIANTS)}）"
        )
    ws = workspace if workspace is not None else build_panel_workspace()
    if variant == "normal":
        provider = PanelDirectoryProvider(ws, semester=semester)
    elif variant == "slow":
        provider = PanelDirectoryProvider(ws, semester=semester, delay=slow_delay)
    elif variant == "fault":
        # a second plausible current-semester hub → discovery ambiguity (like
        # the verified 'Clash Notes …' coexistence, but within one semester)
        ws.search_results.append(search_page(AMBIG_HUB, "Class Notes 2026 下半学期（备份）"))
        provider = PanelDirectoryProvider(ws, semester=semester)
    elif variant == "fault-launch":
        provider = FaultLaunchProvider(ws, semester=semester)
    elif variant == "empty":
        # Keep the hub and its special children, but remove course pages. The
        # real adapter therefore returns a successful empty directory rather
        # than treating an empty result as a read failure.
        hub_id = ws.search_results[0]["id"]
        ws.children[hub_id] = [
            child
            for child in ws.children[hub_id]
            if child.get("type") == "child_database"
            or child.get("id") in {LEARNING_CENTER, NOTES_HUB, NOISE_PAGE}
        ]
        provider = PanelDirectoryProvider(ws, semester=semester)
    else:  # usage-cap
        provider = UsageCapProvider(ws, semester=semester)
    return DirectoryService(provider, clock=clock)


def build_fake_directory_service(*args, **kwargs) -> DirectoryService:
    """Alias matching the ``make_directory_service`` factory name pattern."""
    return build_fake_directory(*args, **kwargs)


__all__ = [
    "PANEL_FAKE_SEMESTER",
    "FAULT_LAUNCH_TARGET",
    "SLOW_DELAY",
    "PanelDirectoryProvider",
    "FaultLaunchProvider",
    "UsageCapProvider",
    "build_fake_directory",
    "build_fake_directory_service",
]

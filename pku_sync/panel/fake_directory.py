"""Seeded-fake startup mode for the student panel (reusable M3 fixture).

Every later browser-verified M3 panel feature starts the panel against one of
these variants INSTEAD of re-deriving fake data:

- ``normal``       — the seeded workspace, honest instant responses.
- ``slow``         — same seeds with a per-call delay so the syncing/loading
                     states are reachable in the browser.
- ``grade-slow``   — the generated exercise carries answered blocks (待批改)
                     and the grade job is deliberately slow.
- ``grade-recovery`` — the generated exercise is 待批改 and the wrong-answer
                     preflight fails once before recovery.
- ``low-balance``  — the generated exercise is 待批改 (answer blocks seeded)
                     and the fake platform carries only a 4-point balance, so
                     every grade trigger returns relay 402 (quota blocked).
- ``grade-502``    — the generated exercise is 待批改; the FIRST grade call
                     raises a one-shot relay 502 so the retryable error state
                     is reachable, and a manual retry then completes exactly
                     one result set.
- ``organize-blocked`` — directory/course/scope load normally; every organize
                     trigger fails after starting with a representative relay
                     502 blocked result (fake-only quiz injection), so the
                     blocked+retry state is reachable without adding a row or
                     any settlement.
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
    COURSE_NET,
    EXERCISE_GENERATED,
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
    "grade-slow",
    "grade-recovery",
    "low-balance",
    "grade-502",
    "organize-blocked",
    "fault",
    "fault-launch",
    "transport-launch",
    "missing-lecture",
    "empty",
    "usage-cap",
    "exercise-fault-launch",
    "exercise-missing",
)

# The fake variants that render the generated exercise as 待批改 (answered
# blocks seeded on EXERCISE_GENERATED with the same shared seed function).
_PENDING_GRADE_VARIANTS = frozenset(
    {"grade-slow", "grade-recovery", "low-balance", "grade-502"}
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


class TransportLaunchProvider(PanelDirectoryProvider):
    """Reject the first launch transport, then allow an identity retry."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._remaining_failures = 1

    def resolve_launch(self, data, target_id, *, course_id=None):
        raw = (target_id or "").strip()
        if raw in (FAULT_LAUNCH_TARGET, FAULT_LAUNCH_TARGET.replace("-", "")) and self._remaining_failures:
            self._remaining_failures -= 1
            raise ConnectionError("simulated launch transport rejection")
        return super().resolve_launch(data, target_id, course_id=course_id)


class MissingLectureProvider(PanelDirectoryProvider):
    """Keep a populated course lecture row whose page identity is absent."""

    def load(self) -> DirectoryData:
        data = super().load()
        from .fake_workspace import NET_L2
        for lecture in data.lectures:
            if lecture.id == NET_L2:
                lecture.url = ""
        return data


class ExerciseFaultLaunchProvider(PanelDirectoryProvider):
    """Fail one exercise launch once so the browser can verify retry."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._remaining_failures = 1

    def resolve_launch(
        self, data: DirectoryData, target_id: str, *, course_id: str | None = None
    ) -> LaunchResult:
        raw = (target_id or "").strip()
        if raw == EXERCISE_GENERATED and self._remaining_failures:
            self._remaining_failures -= 1
            return LaunchResult(
                status=LAUNCH_FAILED,
                target_id=raw,
                url=None,
                fallback=None,
            )
        return super().resolve_launch(data, target_id, course_id=course_id)


class ExerciseMissingIdentityProvider(PanelDirectoryProvider):
    """Keep a directory record whose exercise page identity is absent."""

    def load(self) -> DirectoryData:
        data = super().load()
        for item in data.exercises:
            if item.id == EXERCISE_GENERATED:
                item.url = ""
        return data


class UsageCapProvider(PanelDirectoryProvider):
    """Variant matching a retryable Notion usage-cap/read failure."""

    def load(self) -> DirectoryData:
        raise NotionRetryableError(status=429)


def _seed_pending_grade_answers(ws) -> None:
    """Give the generated exercise answered question blocks -> 待批改.

    Shared by every fake variant whose exercise directory must reach the
    grade trigger (grade-slow / grade-recovery / low-balance / grade-502).
    The blocks mirror the real exercise-page shape the adapter fingerprints
    (heading + ``答案：`` paragraphs).
    """
    from .fake_workspace import EXERCISE_GENERATED, paragraph, text_piece

    def heading_3(text):
        return {
            "object": "block",
            "type": "heading_3",
            "heading_3": {"rich_text": [text_piece(text)]},
        }

    ws.children[EXERCISE_GENERATED] = [
        heading_3("\u7b2c\u4e00\u9898"), paragraph("\u7b54\u6848\uff1aA"),
        heading_3("\u7b2c\u4e8c\u9898"), paragraph("\u7b54\u6848\uff1a\u6b63\u786e"),
    ]


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
    elif variant in _PENDING_GRADE_VARIANTS:
        # The folder loads normally; only the generated exercise carries
        # answered blocks (待批改). The fake platform bridge supplies the
        # variant-specific relay 402 / 502 behavior in the CLI wiring.
        _seed_pending_grade_answers(ws)
        provider = PanelDirectoryProvider(ws, semester=semester)
    elif variant == "fault":
        # a second plausible current-semester hub → discovery ambiguity (like
        # the verified 'Clash Notes …' coexistence, but within one semester)
        ws.search_results.append(search_page(AMBIG_HUB, "Class Notes 2026 下半学期（备份）"))
        provider = PanelDirectoryProvider(ws, semester=semester)
    elif variant == "fault-launch":
        provider = FaultLaunchProvider(ws, semester=semester)
    elif variant == "transport-launch":
        provider = TransportLaunchProvider(ws, semester=semester)
    elif variant == "missing-lecture":
        provider = MissingLectureProvider(ws, semester=semester)
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
    elif variant == "usage-cap":
        provider = UsageCapProvider(ws, semester=semester)
    elif variant == "exercise-fault-launch":
        provider = ExerciseFaultLaunchProvider(ws, semester=semester)
    elif variant == "exercise-missing":
        provider = ExerciseMissingIdentityProvider(ws, semester=semester)
    else:  # organize-blocked keeps a fully normal directory; the fake relay
        # injection (quiz 502) supplies the blocked result in the CLI wiring.
        provider = PanelDirectoryProvider(ws, semester=semester)
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
    "TransportLaunchProvider",
    "MissingLectureProvider",
    "ExerciseFaultLaunchProvider",
    "ExerciseMissingIdentityProvider",
    "UsageCapProvider",
    "build_fake_directory",
    "build_fake_directory_service",
]

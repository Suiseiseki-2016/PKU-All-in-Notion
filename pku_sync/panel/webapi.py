"""Local course workspace, account and recording APIs."""

from __future__ import annotations

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from .jobs import ExerciseJobGate

def create_app(
    settings=None,
    connection_service=None,
    platform_service=None,
    exercise_job_gate: ExerciseJobGate | None = None,
    update_service=None,
    notion_opener=None,
    submission_fence_factory=None,
) -> FastAPI:
    """Build the current course workspace and account APIs."""
    if settings is None:
        from ..config import settings as default_settings

        settings = default_settings
    if connection_service is None:
        from .connection import make_connection_service

        connection_service = make_connection_service(settings)
    if platform_service is None:
        from .platform_bridge import RealPlatformBridge

        platform_service = RealPlatformBridge(settings)

    app = FastAPI(title="pku-sync panel", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.connection_service = connection_service
    app.state.platform_service = platform_service
    if exercise_job_gate is None:
        exercise_job_gate = ExerciseJobGate()
    app.state.exercise_job_gate = exercise_job_gate
    if update_service is None:
        from ..autoupdate import UpdateService

        update_service = UpdateService.from_settings(settings)
    app.state.update_service = update_service

    from .connection_api import add_connection_routes
    from .student_ui import add_student_ui_routes
    from .external_navigation import add_external_navigation_routes

    add_connection_routes(app, connection_service)
    add_student_ui_routes(app)
    add_external_navigation_routes(app, opener=notion_opener)
    from .recordings_api import RecordingWorkManager, add_recording_routes
    recording_manager = RecordingWorkManager(settings)
    app.state.recording_manager = recording_manager
    recording_manager.platform_service = platform_service
    from .connection import FakeConnectionProvider
    demo_connection = (isinstance(connection_service.provider, FakeConnectionProvider)
                       and not getattr(settings, "notion_token", ""))
    add_recording_routes(app, recording_manager, demo_connection=demo_connection)
    from .assignment_workflow import add_assignment_routes
    add_assignment_routes(app, recording_manager, fence_factory=submission_fence_factory)
    from .campus_quiz import CampusQuizService
    from .campus_quiz_api import add_campus_quiz_routes
    from .campus_quiz_grading import CampusQuizGrading
    app.state.campus_quiz_service = CampusQuizService(settings, platform_service)
    app.state.campus_quiz_grading = CampusQuizGrading(settings, platform_service, app.state.campus_quiz_service)
    add_campus_quiz_routes(app, app.state.campus_quiz_service, exercise_job_gate, app.state.campus_quiz_grading)

    @app.get("/")
    def index() -> RedirectResponse:
        return RedirectResponse("/app", status_code=307)

    def _platform_http(exc) -> HTTPException:
        status = int(getattr(exc, "status_code", None) or 400)
        if status not in (400, 401, 402, 403, 409, 429, 502, 503):
            status = 400
        return HTTPException(status_code=status, detail=str(exc))

    @app.get("/api/platform/quota")
    def platform_quota() -> dict:
        return platform_service.quota()

    @app.post("/api/platform/activate")
    def platform_activate(code: str = Body(..., embed=True)) -> dict:
        from ..platform import PlatformError

        try:
            return platform_service.activate(code)
        except PlatformError as exc:
            raise _platform_http(exc) from exc

    @app.post("/api/platform/redeem")
    def platform_redeem(code: str = Body(..., embed=True)) -> dict:
        from ..platform import PlatformError

        try:
            return platform_service.redeem(code)
        except PlatformError as exc:
            raise _platform_http(exc) from exc

    @app.post("/api/platform/exchange")
    def platform_exchange(
        hours: int = Body(1, strict=True, ge=1, le=100), target: str = Body("advanced")
    ) -> dict:
        from ..platform import PlatformError

        try:
            if hasattr(platform_service, "exchange_quota"):
                return platform_service.exchange_quota(hours, target=target)
            from ..platform import exchange_quota

            return exchange_quota(hours, settings, target=target)
        except PlatformError as exc:
            raise _platform_http(exc) from exc

    @app.post("/api/auth/bind-campus")
    def auth_bind_campus(student_id: str = Body(..., embed=True)) -> dict:
        from ..platform import PlatformError

        try:
            if hasattr(platform_service, "bind_campus"):
                return platform_service.bind_campus(student_id)
            from ..platform import bind_campus

            return bind_campus(student_id, settings)
        except PlatformError as exc:
            raise _platform_http(exc) from exc

    @app.post("/api/auth/register")
    def auth_register(
        email: str = Body(...),
        password: str = Body(...),
        invite_code: str = Body(""),
    ) -> dict:
        from ..platform import PlatformError

        try:
            if invite_code and hasattr(platform_service, "register"):
                return platform_service.register(email, password, invite_code=invite_code)
            return platform_service.register(email, password)
        except PlatformError as exc:
            raise _platform_http(exc) from exc

    @app.post("/api/auth/login")
    def auth_login(email: str = Body(...), password: str = Body(...)) -> dict:
        from ..platform import PlatformError

        try:
            return platform_service.login(email, password)
        except PlatformError as exc:
            raise _platform_http(exc) from exc

    @app.post("/api/auth/logout")
    def auth_logout() -> dict:
        from ..platform import PlatformError

        try:
            return platform_service.logout()
        except PlatformError as exc:
            raise _platform_http(exc) from exc

    @app.get("/api/auth/me")
    def auth_me() -> dict:
        return platform_service.me()

    @app.post("/api/auth/forgot")
    def auth_forgot(email: str = Body(..., embed=True)) -> dict:
        from ..platform import PlatformError

        try:
            return platform_service.forgot_password(email)
        except PlatformError as exc:
            raise _platform_http(exc) from exc

    @app.post("/api/auth/resend-verification")
    def auth_resend(email: str = Body(..., embed=True)) -> dict:
        from ..platform import PlatformError

        try:
            return platform_service.resend_verification(email)
        except PlatformError as exc:
            raise _platform_http(exc) from exc

    @app.get("/api/update")
    def update_status() -> dict:
        return update_service.panel_state()

    @app.post("/api/update/request")
    def request_update(version: str = Body(..., embed=True)) -> dict:
        try:
            update_service.request_apply(version)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid update request") from exc
        return update_service.panel_state()

    @app.get("/healthz")
    def healthz() -> dict:
        import os
        instance_id = os.environ.get("PKU_PANEL_INSTANCE_ID")
        return {"ok": True, **({"instance_id": instance_id} if instance_id else {})}

    return app

"""Register catalog APIs with the relay's existing account/session checks."""
from fastapi import Body, Header, HTTPException
from . import catalog, projection_receipts


def register(app, connect, account_for, require_verified):
    def run(authorization, action):
        db = connect(app.state.db_path)
        try:
            account = account_for(authorization, db)
            require_verified(account)
            catalog.ensure_schema(db)
            projection_receipts.ensure_schema(db)
            try:
                return action(db, int(account["id"]))
            except catalog.Conflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except (ValueError, KeyError, TypeError) as exc:
                raise HTTPException(status_code=400, detail="invalid or incomplete catalog metadata") from exc
        finally:
            db.close()

    @app.get("/v1/catalog")
    def list_catalog(authorization: str | None = Header(default=None)):
        return run(authorization, lambda db, account: catalog.list_courses(db, account))

    @app.get("/v1/catalog/projection-protocol")
    def projection_protocol(authorization: str | None = Header(default=None)):
        return run(authorization, lambda db, account: projection_receipts.PROTOCOL)

    @app.get("/v1/catalog/{course_id}/projection/requests/{client_request_id}")
    def projection_request(course_id: str, client_request_id: str, authorization: str | None = Header(default=None)):
        return run(authorization, lambda db, account: projection_receipts.read(db, account, course_id, client_request_id))

    @app.get("/v1/catalog/{course_id}")
    def get_catalog(course_id: str, authorization: str | None = Header(default=None)):
        return run(authorization, lambda db, account: catalog.get(db, account, course_id))

    @app.post("/v1/catalog/{course_id}/observations")
    def begin_observation(course_id: str, authorization: str | None = Header(default=None)):
        return run(authorization, lambda db, account: catalog.observe(db, account, course_id))

    @app.put("/v1/catalog/{course_id}")
    def update_catalog(course_id: str, body: dict = Body(...), authorization: str | None = Header(default=None)):
        return run(authorization, lambda db, account: catalog.put(db, account, course_id,
                   body["revision"], body["observation_id"], body["snapshot"],
                   removal_confirmation=body.get("removal_confirmation")))


    @app.post("/v1/catalog/{course_id}/projection")
    def acquire_projection(course_id: str, body: dict = Body(...), authorization: str | None = Header(default=None)):
        if 'client_request_id' in body:
            return run(authorization, lambda db, account: projection_receipts.acquire(db, account, course_id, body['revision'], body['client_request_id']))
        return run(authorization, lambda db, account: catalog.acquire(db, account, course_id, body["revision"]))

    @app.post("/v1/catalog/{course_id}/projection/release")
    def release_projection(course_id: str, body: dict = Body(...), authorization: str | None = Header(default=None)):
        if 'client_request_id' in body:
            return run(authorization, lambda db, account: projection_receipts.release(db, account, course_id, body['client_request_id'], body['permit_id']))
        return run(authorization, lambda db, account: catalog.release(db, account, course_id, body["permit_id"]))

    @app.post("/v1/catalog/{course_id}/projection/verify")
    def verify_projection(course_id: str, body: dict = Body(...), authorization: str | None = Header(default=None)):
        return run(authorization, lambda db, account: catalog.verify_permit(
            db, account, course_id, body["revision"], body["permit_id"]))

    @app.post('/v1/catalog/{course_id}/assignments/{content_id}/submission')
    def begin_submission(course_id: str, content_id: str, body: dict = Body(...), authorization: str | None = Header(default=None)):
        return run(authorization, lambda db, account: catalog.begin_submission(
            db, account, course_id, content_id, body['revision'], body['portal_account_sha256'],
            body['operation_id'], body['file_sha256']))

    @app.post('/v1/catalog/{course_id}/assignments/{content_id}/submission/arm')
    def arm_submission(course_id: str, content_id: str, body: dict = Body(...), authorization: str | None = Header(default=None)):
        return run(authorization, lambda db, account: catalog.mark_submission_posted(
            db, account, course_id, content_id, body['portal_account_sha256'], body['operation_id'], body['permit_id']))

    @app.get('/v1/catalog/{course_id}/assignments/{content_id}/submission/{operation_id}')
    def read_submission(course_id: str, content_id: str, operation_id: str, portal_account_sha256: str,
                        authorization: str | None = Header(default=None)):
        return run(authorization, lambda db, account: catalog.get_submission(
            db, account, course_id, content_id, portal_account_sha256, operation_id))

    @app.post('/v1/catalog/{course_id}/assignments/{content_id}/submission/release')
    def release_submission(course_id: str, content_id: str, body: dict = Body(...), authorization: str | None = Header(default=None)):
        return run(authorization, lambda db, account: catalog.release_submission(
            db, account, course_id, content_id, body['portal_account_sha256'], body['operation_id'], body['permit_id']))

    @app.post('/v1/catalog/{course_id}/assignments/{content_id}/submission/complete')
    def complete_submission(course_id: str, content_id: str, body: dict = Body(...), authorization: str | None = Header(default=None)):
        return run(authorization, lambda db, account: catalog.complete_submission(
            db, account, course_id, content_id, body['portal_account_sha256'], body['operation_id'], body['permit_id'], body['receipt']))

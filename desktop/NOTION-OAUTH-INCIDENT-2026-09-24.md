# Notion OAuth production incident (2026-09-24)

## Symptom and cause

A student completed the browser authorization callback, but the desktop panel remained disconnected. The callback only supplies a single-use authorization code. The production relay returned HTTP 503 with the safe detail `Notion OAuth is not configured` when the client tried to exchange that code.

Read-only inspection confirmed that both `NOTION_OAUTH_CLIENT_ID` and `NOTION_OAUTH_CLIENT_SECRET` were absent from `/opt/pku-server/.env` and from the running `pku-relay` container. The public client ID was already present in the desktop release. The local app's platform account remained logged in and email verified.

## Repair

With the user's explicit authorization for secret transfer and a production restart, the existing client ID and secret were added to the relay environment file. Its previous contents were retained at `/opt/pku-server/.env.before-notion-20260924` (mode 600). The old container was stopped and renamed `rollback-oauth-20260924`; a new `pku-relay` container was started from the same `pku-server` image, with the same `/opt/pku-server/data:/data` volume, port 8000, and restart policy. The existing image digest and environment values other than the two new keys were checked before recreation.

Post-repair checks: relay `/healthz` HTTP 200, authenticated `/v1/quota` HTTP 200, and `/v1/notion/exchange` with an intentionally invalid code HTTP 502 rather than the former 503. Both OAuth environment variables are present in the container; the env file and backup remain mode 600. The old container remains stopped for rollback.

The student must begin a new browser authorization because the previous authorization code cannot be reused. A successful live authorization is the remaining attended verification.

## Client change and tests

The panel now maps relay HTTP 503 to an actionable service-configuration message without showing the authorization code or provider response body. Logs record only exception class and HTTP status. The added regression test verifies that a private code does not reach the connection API payload.

Targeted auth/browser tests: 42 passed. With the desktop app using port 8791, the suite excluding port-reservation tests: 921 passed. The port-reservation tests require a free 8791 and were blocked by the user's running desktop app; they passed in the prior full Windows run before the app was opened.

## Rollback

If the new relay fails, stop and remove the new `pku-relay` container, rename `rollback-oauth-20260924` back to `pku-relay`, restore `/opt/pku-server/.env.before-notion-20260924` as `/opt/pku-server/.env`, start the old container, and verify `/healthz` and account routes. The accounts database stays on the bind-mounted volume throughout.

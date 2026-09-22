# Public package index — release runbook (user-owned deployment)

This is the exact procedure for publishing `pku-course-sync` releases to the
public PEP 503 index on the user's website. **Website deployment and release
publication are user-owned actions** — workers prepare this tree and runbook
but never deploy, push, or publish.

## URL contract (what the installer depends on)

The packaged client installs and upgrades **by package name** from:

```text
https://pku.aeoluswu.info/packages/simple/pku-course-sync/    (PEP 503 project page)
https://pku.aeoluswu.info/packages/wheels/<wheel-file>        (release wheels)
```

`installer/windows/bootstrap.ps1` defaults `-Index` to
`https://pku.aeoluswu.info/packages/simple/` and passes it as uv's `--index`
(an *additional* index on top of PyPI, so dependencies still resolve from
PyPI or the TUNA mirror). The uv receipt records the name requirement plus
this index, which is what makes a literal `uv tool upgrade pku-course-sync`
(the in-app autoupdate apply path) resolve newer published wheels — verified
locally against a loopback simple-index fixture with a 0.1.0 → 0.1.1 swap.

The route is the **user-authorized additive static location on the same
host as the deployed relay** (`pku.aeoluswu.info`): nginx serves `/packages/`
from an uploaded static tree while every existing route — `/`, `/v1/*`,
`/healthz` — keeps its existing proxy behavior unchanged. It is NOT served
by the relay application, never routes through the relay container, and
requires **no relay code, container, or database change**. (2026-09-22
note: this route replaced the earlier `aeoluswu.info` apex base, which
never served — TLS failure via proxy, no DNS direct — while the
`pku.aeoluswu.info` host answers normally. The user explicitly authorized
the additive `pku.aeoluswu.info/packages/` production route.)

## Layout

| File | Role |
|---|---|
| `make_simple_index.py` | assembles the upload tree (deterministic, stdlib-only) |
| `site/simple/pku-course-sync/index.html` | PEP 503 page, sha256-pinned anchors (committed) |
| `site/SHA256SUMS.txt` | hash manifest of every published wheel (committed) |
| `site/wheels/` | the wheels themselves — build artifacts, **not** committed |

`make_simple_index.py` output layout (what gets uploaded):

```text
site/
  simple/pku-course-sync/index.html
  wheels/pku_course_sync-<version>-py3-none-any.whl
  SHA256SUMS.txt
```

## One-time hosting setup (user action, before the first release)

The deployment host is the machine that already terminates TLS for
`pku.aeoluswu.info` in front of the relay container (per the relay repo
README: nginx terminates TLS; public traffic reaches the relay through
nginx and Cloudflare). The location below is **purely additive** — do not
touch any existing `location`, `proxy_pass`, or server block directive.

### 1. Upload the static tree

Copy the prepared tree (from this directory) to a path on that host, e.g.
`/var/www/pku-packages/` (the on-disk root in the snippet below; adapt the
path if your layout differs, and keep it OUTSIDE the relay container's
data volume):

```text
/var/www/pku-packages/
  simple/pku-course-sync/index.html
  wheels/pku_course_sync-<version>-py3-none-any.whl
  SHA256SUMS.txt
```

e.g. from a machine holding the repo:

```bash
scp -r installer/release/site/* <user>@<host>:/var/www/pku-packages/
```

If your nginx runs **in a container**, mount the uploaded directory into
that container instead (e.g. `-v /var/www/pku-packages:/var/www/pku-packages:ro`)
and use the in-container path in the snippet.

### 2. Add the additive nginx location (exact snippet)

Inside the EXISTING `server { ... }` block for `pku.aeoluswu.info` — the
one that already proxies `/v1/*` and `/healthz` to the relay — add these
three locations (and nothing else):

```nginx
# --- BEGIN PKU package index (additive static location; m5-fix-production-index-route) ---
# Serves the public PEP 503 index from the uploaded static tree. Purely
# additive: /, /v1/*, /healthz keep their existing proxy behavior.
# ^~ keeps regex locations in this server block from hijacking wheel paths.
location ^~ /packages/simple/ {
    alias /var/www/pku-packages/simple/;
    index index.html;
    autoindex off;
    # REQUIRED: hard caching hides newly published versions from
    # `uv tool upgrade` within uv's freshness window (verified locally).
    add_header Cache-Control "no-cache" always;
}
location ^~ /packages/wheels/ {
    alias /var/www/pku-packages/wheels/;
    autoindex off;
    # Wheel filenames are unique per version; caching them is safe.
    add_header Cache-Control "public, max-age=86400" always;
}
location ^~ /packages/ {
    alias /var/www/pku-packages/;
    autoindex off;
}
# --- END PKU package index ---
```

Notes:
- The longest-prefix rule picks `^~ /packages/simple/` over
  `^~ /packages/` for simple pages, so only those carry `no-cache`.
- `add_header` does not inherit from the server block into a location
  that declares its own; if you rely on server-level `add_header` values
  (e.g. HSTS) under `/packages/`, repeat them inside these locations.
- No Cloudflare change is needed: the hostname is already proxied, and
  this only adds an origin-served path under it. (Cache rules at the edge
  still see `Cache-Control: no-cache` from origin for simple pages.)
- If the host runs multiple origin servers behind the same hostname
  (primary/backup), apply the SAME snippet on each so the route is
  consistent after failover.

### 3. Validate and reload (user action)

```bash
nginx -t          # MUST report "ok / successful" before reloading
nginx -s reload   # or: systemctl reload nginx
```

(For a containerized nginx: `docker exec <nginx-container> nginx -t` then
`docker exec <nginx-container> nginx -s reload`. Never restart or modify
the relay container itself — the reload is in the TLS-terminating nginx
only, and a reload does not drop proxied traffic.)

### 4. Cache headers

`Cache-Control: no-cache` on `/packages/simple/` pages is a hard
requirement, not a preference — verified locally: with no cache header, uv
can keep using a cached copy of the project page within its freshness
window and a just-published version is invisible to `uv tool upgrade`
until that window expires. Wheel files may be cached long — their
filenames are unique per version.

### 5. No authentication

Do not add authentication: the index and wheels are public release
artifacts (no secrets, no user data — see the security notes).

## Per-release steps (hash-pinned)

1. Bump `version` in `pyproject.toml`, commit, and tag `vX.Y.Z`.
2. Build the wheel at the release commit:
   `uv build --wheel` → `dist/pku_course_sync-X.Y.Z-py3-none-any.whl`.
3. Regenerate the upload tree from the repo root:
   `py installer\release\make_simple_index.py --wheels dist --out installer\release\site`
   (default `--base-url https://pku.aeoluswu.info/packages/`). The page lists
   every wheel in `dist/` — copy previously published wheels into `dist/`
   first if you want them to remain listed (old versions should stay
   listed so downgrades and older installs keep working).
4. Verify the hash pinning matches the built wheel (recorded evidence for
   the release): the `#sha256=` fragment in `index.html` and the
   `SHA256SUMS.txt` line must equal
   `Get-FileHash dist\pku_course_sync-X.Y.Z-py3-none-any.whl -Algorithm SHA256`.
   The committed 0.1.0 entry pins
   `2a1de66498f9fb126dcece549d86bacffbe187cbaafdbe352621d6dcf5095d86`
   (the build was verified byte-reproducible for this repo; if a rebuild
   hashes differently, regenerate the tree so the committed pins match).
5. Local pre-flight (zero-deploy, before touching the website): serve the
   tree on an approved loopback port and prove install-by-name plus the
   literal upgrade swap in scratch tool dirs (`UV_TOOL_DIR`/`UV_TOOL_BIN_DIR`
   redirected to a temp dir, never the real user tool dir):

   ```powershell
   py -m http.server 8802 --bind 127.0.0.1 --directory installer\release\site
   # second shell:
   $env:UV_TOOL_DIR = "$env:TEMP\pku-idx-preflight\toolenv"
   $env:UV_TOOL_BIN_DIR = "$env:TEMP\pku-idx-preflight\toolbin"
   uv tool install --managed-python --python 3.11 --index http://127.0.0.1:8802/simple/ pku-course-sync
   uv tool list                                  # expect vX.Y.Z
   uv tool upgrade pku-course-sync               # "Nothing to upgrade"
   # publish the next version into the tree (step 3), then:
   uv tool upgrade pku-course-sync               # expect v<next>
   ```

6. Commit the regenerated `site/simple/pku-course-sync/index.html` and
   `site/SHA256SUMS.txt` (the repo tracks the published index state).
7. **Upload** (user action): copy the `site/` tree to the uploaded static
   root from the one-time hosting setup (e.g. `/var/www/pku-packages/`) so
   the two URL-contract paths above serve it; then reload nginx per that
   section (a reload is only needed when the location itself changed, not
   for routine wheel/index file updates).
8. **Post-deploy verification** (read-only; once the user confirms the
   deployment, the mission runs these checks and records them in
   `validation\m5-packaging-pilot\` — the user can run the same commands):
   - `curl.exe -s https://pku.aeoluswu.info/packages/simple/pku-course-sync/`
     shows the hash-pinned anchor;
   - the response headers on that page include `Cache-Control: no-cache`;
   - `curl.exe -s -o <scratch> https://pku.aeoluswu.info/packages/wheels/<wheel>`
     downloads the wheel and its sha256 equals the `SHA256SUMS.txt` line
     (and the `#sha256=` anchor);
   - a scratch `uv tool install --index
     https://pku.aeoluswu.info/packages/simple/ pku-course-sync`
     (scratch `UV_TOOL_DIR`/`UV_TOOL_BIN_DIR`/`UV_CACHE_DIR`, never the
     real user tool dir) succeeds by name and the receipt retains the
     public index;
   - with two versions published, a literal `uv tool upgrade pku-course-sync`
     in that scratch env swaps to the newest;
   - the relay routes are UNCHANGED: `https://pku.aeoluswu.info/healthz`
     still returns `200 {"ok":true}` and a representative authenticated
     relay route (`https://pku.aeoluswu.info/v1/quota` without a session
     token) still returns the relay's `401` — the recorded pre-deploy
     baseline (2026-09-22) for both is in
     `validation\m5-packaging-pilot\m5-fix-production-index-route\evidence\production-pre-deploy-baseline.txt`.
     A static hijack of `/v1/*` would return 404 instead of 401, so this
     pair is the regression proof.
9. Publish the GitHub release with tag `vX.Y.Z` matching the wheel version —
   the client's notify-only update check reads this manifest
   (`api.github.com/repos/Suiseiseki-2016/PKU-All-in-Notion/releases/latest`).
10. (Windows wave 1) compile the Inno installer per
    `installer/windows/README.md`; it installs by name from the public index
    and bundles the same wheel only as the offline fallback.

## Security notes

- Public release artifacts must contain **no secrets and no user data**:
   run the mission's artifact secret scan over the wheel before upload
   (the only recorded finding in 0.1.0 is the deliberate FAKE honeypot
   signed-URL placeholder in the `--fake` demo workspace, documented in the
   M5 evidence packs).
- The `#sha256=` anchors make uv verify each wheel's integrity on download.
- `pku-course-sync` is NOT published on PyPI (verified 404 on 2026-09-22).
  Because the installer records the public index as an *additional* index
  (required so a user-level mirror/`UV_DEFAULT_INDEX` cannot block literal
  upgrades — verified), a same-named package on PyPI would take precedence
  for installs and upgrades. Monitor for that; defensive options (registering
  the PyPI name, authenticated private index) are post-pilot decisions.
- Never serve the index from the deployed relay path and never put relay
  credentials anywhere near this tree.

## Pre-deployment state (recorded 2026-09-22)

`https://pku.aeoluswu.info/packages/simple/pku-course-sync/` is **not yet
deployed**: a read-only probe from the mission host returned the relay's
JSON 404 fallthrough (`{"detail":"Not Found"}`) for that path, while
`/healthz` answered `200 {"ok":true}` and `GET /v1/quota` without a token
answered the relay's `401 {"detail":"missing session token"}` — the
recorded pre-deploy baseline. (The earlier `aeoluswu.info` apex base never
served at all: TLS handshake failure via the local proxy, no DNS direct,
502 over HTTP — which is why the user authorized the additive
`pku.aeoluswu.info/packages/` route instead.) Until deployment, installs
take the installer's bundled-wheel `--find-links` fallback (still a
name-based receipt; verified locally end-to-end against the unreachable
production default). After deployment, existing fallback-installed
clients switch to the index-backed form with the one-time command:

```powershell
uv tool upgrade pku-course-sync --index https://pku.aeoluswu.info/packages/simple/
```

(verified locally: this both upgrades and rewrites the receipt to the
index-backed form, after which literal upgrades work).

## Rollback (user action; recorded per the m5-fix-production-index-route requirement)

The deployment is a self-contained additive nginx location — rolling it
back restores exactly the pre-deploy state and touches nothing else:

1. Remove the three `location` blocks added in the one-time hosting
   setup (everything between the `BEGIN PKU package index` and
   `END PKU package index` comment markers). No other nginx directive
   was changed, so nothing else needs restoring.
2. `nginx -t` — must pass — then `nginx -s reload` (or the containerized
   equivalents from the hosting section).
3. Optional: remove the uploaded tree (`/var/www/pku-packages/`) and the
   container mount if one was added.
4. Verify the rollback (read-only):
   `curl.exe -s https://pku.aeoluswu.info/packages/simple/pku-course-sync/`
   returns the relay's JSON 404 fallthrough again, while
   `https://pku.aeoluswu.info/healthz` still returns `200 {"ok":true}`.

Client-side effect of a rollback: `uv tool upgrade pku-course-sync` stops
resolving new versions (installed clients keep working — the installed
tool env is self-contained), and fresh installs fall back to the
installer's bundled wheel via `--find-links` (name-based receipt). The
relay application, its container, and its database were never involved at
any point, so they need no rollback action.

## Verified upgrade semantics (local fixture evidence, 2026-09-22)

| Receipt form | Literal `uv tool upgrade pku-course-sync` |
|---|---|
| index-backed (`--index`, normal path) | resolves newer published wheels; verified 0.1.0 → 0.1.1 swap, `.env`/data untouched; works with a user-level `UV_DEFAULT_INDEX` mirror set |
| `--find-links` fallback | safe no-op ("Nothing to upgrade"); one explicit `--index` upgrade repairs it |
| wheel-path (legacy pkg-install-artifact form) | permanent no-op — this is why the normal path is no longer wheel-path-based |
| failed/interrupted upgrade | previous version stays installed and runnable; retry after the index recovers completes the swap |

Note: `uv`'s `--find-links` expects a directory (or HTML page), not a single
wheel file — bootstrap.ps1 resolves the bundled wheel's directory.

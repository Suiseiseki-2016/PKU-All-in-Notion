# Public package index — release runbook (user-owned deployment)

This is the exact procedure for publishing `pku-course-sync` releases to the
public PEP 503 index on the user's website. **Website deployment and release
publication are user-owned actions** — workers prepare this tree and runbook
but never deploy, push, or publish.

## URL contract (what the installer depends on)

The packaged client installs and upgrades **by package name** from:

```text
https://aeoluswu.info/packages/simple/pku-course-sync/    (PEP 503 project page)
https://aeoluswu.info/packages/wheels/<wheel-file>        (release wheels)
```

`installer/windows/bootstrap.ps1` defaults `-Index` to
`https://aeoluswu.info/packages/simple/` and passes it as uv's `--index`
(an *additional* index on top of PyPI, so dependencies still resolve from
PyPI or the TUNA mirror). The uv receipt records the name requirement plus
this index, which is what makes a literal `uv tool upgrade pku-course-sync`
(the in-app autoupdate apply path) resolve newer published wheels — verified
locally against a loopback simple-index fixture with a 0.1.0 → 0.1.1 swap.

The index is an **independent static path**: it must not be served by or
routed through the deployed relay (`pku.aeoluswu.info`) and requires no
relay change.

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

1. Serve `https://aeoluswu.info/packages/` as static HTTPS files from the
   `site/` tree above (any static hosting the owner controls; NOT the relay
   service, NOT a relay route).
2. **Cache headers matter.** Serve the `/packages/simple/` pages with
   `Cache-Control: no-cache` (or `max-age` ≤ 60s). Verified locally: with no
   cache header, uv can keep using a cached copy of the project page within
   its freshness window and a just-published version is invisible to
   `uv tool upgrade` until that window expires. Wheel files may be cached
   long — their filenames are unique per version.
3. Do not add authentication: the index and wheels are public release
   artifacts (no secrets, no user data — see the security notes).

## Per-release steps (hash-pinned)

1. Bump `version` in `pyproject.toml`, commit, and tag `vX.Y.Z`.
2. Build the wheel at the release commit:
   `uv build --wheel` → `dist/pku_course_sync-X.Y.Z-py3-none-any.whl`.
3. Regenerate the upload tree from the repo root:
   `py installer\release\make_simple_index.py --wheels dist --out installer\release\site`
   (default `--base-url https://aeoluswu.info/packages/`). The page lists
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
7. **Upload** (user action): copy the `site/` tree to the website so the
   two URL-contract paths above serve it.
8. **Post-deploy verification** (user, read-only):
   `curl.exe -s https://aeoluswu.info/packages/simple/pku-course-sync/`
   shows the new anchor; a scratch
   `uv tool install --index https://aeoluswu.info/packages/simple/ pku-course-sync`
   succeeds and the downloaded wheel's sha256 equals `SHA256SUMS.txt`; with
   two versions published, a literal `uv tool upgrade pku-course-sync`
   swaps to the newest.
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

`https://aeoluswu.info/packages/simple/pku-course-sync/` is **not yet
deployed**: probes from the mission host returned a TLS handshake failure
via the local proxy and 502/HTTP on the apex domain (the relay subdomain
`pku.aeoluswu.info` answers normally, so the apex static path is the gap).
Until deployment, installs take the installer's bundled-wheel `--find-links`
fallback (still a name-based receipt). After deployment, existing
fallback-installed clients switch to the index-backed form with the one-time
command:

```powershell
uv tool upgrade pku-course-sync --index https://aeoluswu.info/packages/simple/
```

(verified locally: this both upgrades and rewrites the receipt to the
index-backed form, after which literal upgrades work).

## Verified upgrade semantics (local fixture evidence, 2026-09-22)

| Receipt form | Literal `uv tool upgrade pku-course-sync` |
|---|---|
| index-backed (`--index`, normal path) | resolves newer published wheels; verified 0.1.0 → 0.1.1 swap, `.env`/data untouched; works with a user-level `UV_DEFAULT_INDEX` mirror set |
| `--find-links` fallback | safe no-op ("Nothing to upgrade"); one explicit `--index` upgrade repairs it |
| wheel-path (legacy pkg-install-artifact form) | permanent no-op — this is why the normal path is no longer wheel-path-based |
| failed/interrupted upgrade | previous version stays installed and runnable; retry after the index recovers completes the swap |

Note: `uv`'s `--find-links` expects a directory (or HTML page), not a single
wheel file — bootstrap.ps1 resolves the bundled wheel's directory.

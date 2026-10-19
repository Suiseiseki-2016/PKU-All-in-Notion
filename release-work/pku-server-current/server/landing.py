"""Public download page and signed desktop update feed."""
from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

ASSETS = Path(__file__).with_name("site_assets")
RELEASES_ENV = "PKU_RELEASE_DIR"
DEFAULT_RELEASES = Path("/data/releases")
PLATFORMS = {
    "windows-x86_64": ("windows", ".exe"),
    "darwin-aarch64": ("mac/arm", ".dmg"),
    "darwin-x86_64": ("mac/intel", ".dmg"),
}
ASSET_TYPES = {
    "site.css": "text/css; charset=utf-8",
    "icon.png": "image/png",
    "desktop-window.png": "image/png",
    "dashboard-preview.png": "image/png",
}
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}$")


def _safe_file(root: Path, name: object, suffix: str) -> Path | None:
    if not isinstance(name, str) or not FILENAME.fullmatch(name) or not name.endswith(suffix):
        return None
    path = (root / name).resolve()
    if path.parent != root.resolve() or not path.is_file():
        return None
    return path


def _releases(root: Path) -> dict:
    try:
        content = json.loads((root / "latest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return content if isinstance(content, dict) else {}


def _entry(root: Path, platform: str) -> tuple[dict, Path] | None:
    item = _releases(root).get(platform)
    if not isinstance(item, dict) or not isinstance(item.get("version"), str):
        return None
    if not VERSION.fullmatch(item["version"]):
        return None
    path = _safe_file(root, item.get("installer"), PLATFORMS[platform][1])
    return (item, path) if path else None


def _button(label: str, href: str, available: bool, *, primary: bool = False) -> str:
    if not available:
        return '<span class="button button-disabled" aria-disabled="true">即将开放</span>'
    cls = "button-primary" if primary else "button-soft"
    return f'<a class="button {cls}" href="{html.escape(href)}">{html.escape(label)} <span aria-hidden="true">↓</span></a>'


def _landing_html(root: Path) -> str:
    windows = _entry(root, "windows-x86_64")
    arm = _entry(root, "darwin-aarch64")
    intel = _entry(root, "darwin-x86_64")
    replacements = {
        "{{WINDOWS_HERO_ACTION}}": _button("下载 Windows 版", "/download/windows", bool(windows), primary=True),
        "{{WINDOWS_CARD_ACTION}}": _button("下载 Windows 安装包", "/download/windows", bool(windows), primary=True),
        "{{WINDOWS_META}}": f'版本 {html.escape(windows[0]["version"])} · {(windows[1].stat().st_size / 1024 / 1024):.1f} MB' if windows else "完成最新构建与安装验收后开放下载。",
        "{{MAC_ARM_ACTION}}": _button("Apple 芯片", "/download/mac/arm", bool(arm)),
        "{{MAC_INTEL_ACTION}}": _button("Intel 芯片", "/download/mac/intel", bool(intel)),
    }
    page = (ASSETS / "landing.html").read_text(encoding="utf-8")
    for marker, value in replacements.items():
        page = page.replace(marker, value)
    return page


def _version(value: str) -> tuple[int, int, int] | None:
    if not VERSION.fullmatch(value):
        return None
    return tuple(int(part) for part in value.split("."))


def add_landing_routes(app: FastAPI, *, release_dir: Path | None = None) -> None:
    root = release_dir or Path(os.environ.get(RELEASES_ENV, DEFAULT_RELEASES))

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def landing() -> HTMLResponse:
        return HTMLResponse(
            _landing_html(root),
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "strict-origin-when-cross-origin",
            },
        )

    @app.get("/site/{name}", include_in_schema=False)
    def site_asset(name: str) -> FileResponse:
        if name not in ASSET_TYPES:
            raise HTTPException(status_code=404)
        return FileResponse(ASSETS / name, media_type=ASSET_TYPES[name], headers={"Cache-Control": "public, max-age=3600"})

    def installer_for(platform: str) -> RedirectResponse:
        found = _entry(root, platform)
        if not found:
            raise HTTPException(status_code=404, detail="This platform download is not available yet")
        _, path = found
        return RedirectResponse(f"/download/release/{path.name}", status_code=302,
                                headers={"Cache-Control": "no-store"})

    @app.get("/download/release/{name}", include_in_schema=False)
    def release_asset(name: str) -> FileResponse:
        # Only files named by the current manifest are public. The versioned
        # filename never changes, so CDN and browser caches can keep it safely.
        for platform in PLATFORMS:
            found = _entry(root, platform)
            if not found:
                continue
            item, installer = found
            updater = _safe_file(root, item.get("updater", item["installer"]),
                                 ".exe" if platform.startswith("windows-") else ".app.tar.gz")
            for path in (installer, updater):
                if path is not None and name == path.name:
                    return FileResponse(path, filename=path.name,
                                        media_type="application/octet-stream",
                                        headers={"Cache-Control": "public, max-age=31536000, immutable",
                                                 "CDN-Cache-Control": "public, max-age=31536000",
                                                 "X-Content-Type-Options": "nosniff"})
        raise HTTPException(status_code=404)

    @app.get("/download/windows", include_in_schema=False)
    def windows_download() -> RedirectResponse:
        return installer_for("windows-x86_64")

    @app.get("/download/mac/{chip}", include_in_schema=False)
    def mac_download(chip: str) -> RedirectResponse:
        platform = {"arm": "darwin-aarch64", "intel": "darwin-x86_64"}.get(chip)
        if platform is None:
            raise HTTPException(status_code=404)
        return installer_for(platform)

    @app.get("/updates/{target}/{arch}/{current_version}", include_in_schema=False)
    def update_feed(target: str, arch: str, current_version: str) -> Response:
        platform = f"{target}-{arch}"
        if platform not in PLATFORMS:
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
        found = _entry(root, platform)
        current = _version(current_version)
        if not found or current is None:
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
        item, installer = found
        newer = _version(item["version"])
        signature = item.get("signature")
        updater = _safe_file(root, item.get("updater", item["installer"]), ".exe" if target == "windows" else ".app.tar.gz")
        if not newer or newer <= current or not updater or not isinstance(signature, str) or not signature.strip() or len(signature) > 1000:
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
        return JSONResponse({
            "version": item["version"],
            "notes": str(item.get("notes", "PKU All in One 更新"))[:500],
            "url": f"https://pku.aeoluswu.info/download/release/{updater.name}",
            "signature": signature.strip(),
        }, headers={"Cache-Control": "no-store"})

    @app.get("/download/update/{target}/{arch}/{version}", include_in_schema=False)
    def update_download(target: str, arch: str, version: str) -> FileResponse:
        platform = f"{target}-{arch}"
        if platform not in PLATFORMS:
            raise HTTPException(status_code=404)
        found = _entry(root, platform)
        if not found or found[0]["version"] != version:
            raise HTTPException(status_code=404)
        item = found[0]
        updater = _safe_file(root, item.get("updater", item["installer"]), ".exe" if target == "windows" else ".app.tar.gz")
        if updater is None or not item.get("signature"):
            raise HTTPException(status_code=404)
        return FileResponse(updater, media_type="application/octet-stream", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

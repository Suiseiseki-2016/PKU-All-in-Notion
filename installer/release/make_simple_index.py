"""Assemble the user-owned public PEP 503 upload tree for pku-course-sync.

The packaged client installs and upgrades by package name from the public
simple index at ``https://pku.aeoluswu.info/packages/simple/pku-course-sync/``
(an ADDITIVE static location on the same host as the deployed relay — it
never routes through the relay application and never modifies relay routes).
This script builds the exact tree the website must serve:

    <out>/
      simple/pku-course-sync/index.html   PEP 503 page, sha256-pinned anchors
      wheels/<wheel files>                copied from the wheels directory
      SHA256SUMS.txt                      hash manifest of every served wheel

Usage (release engineer, from the repo root):

    uv build --wheel --out-dir dist
    py installer/release/make_simple_index.py --wheels dist --out installer/release/site

The output is deterministic (no timestamps, sorted versions), so regenerating
from identical wheels yields byte-identical files. Website deployment is a
user-owned action except as explicitly recorded in
installer/release/RUNBOOK.md (dated per-release authorizations).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import re
import shutil
import sys
from pathlib import Path

# User-authorized production route (feature m5-fix-production-index-route,
# 2026-09-22): the additive static /packages/ location on the SAME host as
# the deployed relay (pku.aeoluswu.info). The earlier apex aeoluswu.info
# base never served and was replaced by this user-authorized route.
DEFAULT_BASE_URL = "https://pku.aeoluswu.info/packages/"
DEFAULT_PROJECT = "pku-course-sync"
DEFAULT_REQUIRES_PYTHON = ">=3.11"


class IndexBuildError(Exception):
    """Raised when the upload tree cannot be assembled honestly."""


def normalize(name: str) -> str:
    """PEP 503 name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_wheel_filename(filename: str) -> tuple[str, str]:
    """Return (distribution, version) from a wheel filename.

    Wheel filenames are ``{distribution}-{version}(-{build})?-{python}-{abi}-{platform}.whl``
    where the distribution never contains ``-``.
    """
    stem = filename[: -len(".whl")]
    parts = stem.split("-")
    if len(parts) < 5:
        raise IndexBuildError(f"not a valid wheel filename: {filename}")
    return parts[0], parts[1]


def version_sort_key(version: str) -> tuple:
    """Numeric-aware ascending sort key for simple dotted versions."""
    segments: list[tuple[int, object]] = []
    for segment in version.split("."):
        if segment.isdigit():
            segments.append((0, int(segment)))
        else:
            segments.append((1, segment))
    return tuple(segments)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_site(
    *,
    wheels_dir: Path | str,
    out_dir: Path | str,
    base_url: str = DEFAULT_BASE_URL,
    project: str = DEFAULT_PROJECT,
    requires_python: str = DEFAULT_REQUIRES_PYTHON,
) -> dict:
    """Build the upload tree and return a summary of what was written.

    All validation happens before anything is written, so a rejected input
    never leaves a partial tree behind.
    """
    wheels_dir = Path(wheels_dir)
    out_dir = Path(out_dir)

    if not re.match(r"^https?://", base_url):
        raise IndexBuildError(f"base URL must be absolute http(s): {base_url}")
    base_url = base_url if base_url.endswith("/") else base_url + "/"

    if not wheels_dir.is_dir():
        raise IndexBuildError(f"wheels directory not found: {wheels_dir}")
    wheels = sorted(wheels_dir.glob("*.whl"))
    if not wheels:
        raise IndexBuildError(f"no *.whl wheels found in {wheels_dir}")

    seen_versions: set[str] = set()
    entries: list[tuple[tuple, str, str]] = []
    for wheel in wheels:
        distribution, version = parse_wheel_filename(wheel.name)
        if normalize(distribution) != normalize(project):
            raise IndexBuildError(
                f"wheel {wheel.name} is not a {project} distribution "
                f"(parsed {distribution}); refusing to publish a mixed index"
            )
        if version in seen_versions:
            raise IndexBuildError(
                f"duplicate version {version} in {wheels_dir}; the index must "
                "list exactly one wheel per version"
            )
        seen_versions.add(version)
        entries.append((version_sort_key(version), version, wheel.name))

    entries.sort()
    anchors = []
    sums_lines = []
    for _key, _version, filename in entries:
        wheel_path = wheels_dir / filename
        digest = sha256_of(wheel_path)
        href = f"{base_url}wheels/{filename}#sha256={digest}"
        anchors.append(
            f'<a href="{href}" data-requires-python="{html.escape(requires_python)}">'
            f"{filename}</a><br/>"
        )
        sums_lines.append(f"{digest}  wheels/{filename}")

    project_dir = normalize(project)
    page = (
        "<!DOCTYPE html>\n"
        "<html>\n"
        "  <head>\n"
        '    <meta name="pypi:repository-version" content="1.0">\n'
        '    <meta charset="utf-8">\n'
        f"    <title>Links for {project}</title>\n"
        "  </head>\n"
        "  <body>\n"
        f"    <h1>Links for {project}</h1>\n"
    )
    for anchor in anchors:
        page += f"    {anchor}\n"
    page += "  </body>\n</html>\n"

    index_html = out_dir / "simple" / project_dir / "index.html"
    sums_txt = out_dir / "SHA256SUMS.txt"
    index_html.parent.mkdir(parents=True, exist_ok=True)
    (out_dir / "wheels").mkdir(parents=True, exist_ok=True)
    index_html.write_text(page, encoding="utf-8", newline="\n")
    sums_txt.write_text("\n".join(sums_lines) + "\n", encoding="utf-8", newline="\n")
    for _key, _version, filename in entries:
        shutil.copyfile(wheels_dir / filename, out_dir / "wheels" / filename)

    return {
        "index_html": str(index_html),
        "sha256sums": str(sums_txt),
        "wheels": [filename for _key, _version, filename in entries],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the public PEP 503 upload tree for pku-course-sync."
    )
    parser.add_argument(
        "--wheels",
        required=True,
        help="directory containing the release wheels (*.whl)",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="output directory for the upload tree (simple/, wheels/, SHA256SUMS.txt)",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"public base URL of the packages path (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--project",
        default=DEFAULT_PROJECT,
        help=f"project name served by the index (default: {DEFAULT_PROJECT})",
    )
    parser.add_argument(
        "--requires-python",
        default=DEFAULT_REQUIRES_PYTHON,
        help=f"data-requires-python marker (default: {DEFAULT_REQUIRES_PYTHON})",
    )
    args = parser.parse_args(argv)
    try:
        summary = build_site(
            wheels_dir=Path(args.wheels),
            out_dir=Path(args.out),
            base_url=args.base_url,
            project=args.project,
            requires_python=args.requires_python,
        )
    except IndexBuildError as error:
        print(f"make_simple_index: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    print(f"make_simple_index: wrote {summary['index_html']}")
    print(f"make_simple_index: wrote {summary['sha256sums']}")
    for wheel in summary["wheels"]:
        print(f"make_simple_index: staged wheels/{wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

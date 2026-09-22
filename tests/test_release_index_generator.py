"""Release simple-index generator tests (m5-fix-index-backed-install-autoupdate).

Covers `installer/release/make_simple_index.py`, the tool that assembles the
user-owned public PEP 503 upload tree for `https://aeoluswu.info/packages/`
(hash-pinned anchors + SHA256SUMS manifest + copied wheels). The generator is
stdlib-only and deterministic so a regenerated index is byte-stable.
"""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = REPO_ROOT / "installer" / "release" / "make_simple_index.py"
DEFAULT_BASE_URL = "https://aeoluswu.info/packages/"


@pytest.fixture(scope="module")
def generator():
    spec = importlib.util.spec_from_file_location("make_simple_index", GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_wheel(directory: Path, version: str, name: str = "pku_course_sync") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}-{version}-py3-none-any.whl"
    path.write_bytes(f"fake wheel bytes {name} {version}".encode("utf-8"))
    return path


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_generator(generator, wheels: Path, out: Path, base_url: str = DEFAULT_BASE_URL):
    return generator.main(
        ["--wheels", str(wheels), "--out", str(out), "--base-url", base_url]
    )


def test_page_pins_sha256_and_uses_public_base_url(generator, tmp_path):
    wheels = tmp_path / "wheels"
    wheel = write_wheel(wheels, "0.1.0")
    out = tmp_path / "site"
    run_generator(generator, wheels, out)

    page = out / "simple" / "pku-course-sync" / "index.html"
    assert page.is_file()
    html = page.read_text(encoding="utf-8")
    anchor = (
        f'<a href="{DEFAULT_BASE_URL}wheels/{wheel.name}#sha256={sha256_of(wheel)}" '
        f'data-requires-python="&gt;=3.11">{wheel.name}</a><br/>'
    )
    assert anchor in html
    assert "<h1>Links for pku-course-sync</h1>" in html


def test_wheels_copied_and_sums_manifest_written(generator, tmp_path):
    wheels = tmp_path / "wheels"
    wheel = write_wheel(wheels, "0.1.0")
    out = tmp_path / "site"
    run_generator(generator, wheels, out)

    copied = out / "wheels" / wheel.name
    assert copied.is_file()
    assert copied.read_bytes() == wheel.read_bytes()
    sums = out / "SHA256SUMS.txt"
    assert sums.is_file()
    assert f"{sha256_of(wheel)}  wheels/{wheel.name}" in sums.read_text(encoding="utf-8")


def test_ignores_non_wheel_files(generator, tmp_path):
    wheels = tmp_path / "wheels"
    wheel = write_wheel(wheels, "0.1.0")
    (wheels / "pku_course_sync-0.1.0.tar.gz").write_bytes(b"not a wheel")
    (wheels / "notes.txt").write_text("ignore me", encoding="utf-8")
    out = tmp_path / "site"
    run_generator(generator, wheels, out)

    html = (out / "simple" / "pku-course-sync" / "index.html").read_text(encoding="utf-8")
    assert html.count("<a href=") == 1
    assert wheel.name in html
    assert "tar.gz" not in html
    assert sorted(p.name for p in (out / "wheels").iterdir()) == [wheel.name]


def test_versions_sorted_ascending(generator, tmp_path):
    wheels = tmp_path / "wheels"
    write_wheel(wheels, "0.1.1")
    write_wheel(wheels, "0.1.0")
    out = tmp_path / "site"
    run_generator(generator, wheels, out)

    html = (out / "simple" / "pku-course-sync" / "index.html").read_text(encoding="utf-8")
    assert html.count("<a href=") == 2
    assert html.index("pku_course_sync-0.1.0-py3-none-any.whl") < html.index(
        "pku_course_sync-0.1.1-py3-none-any.whl"
    )


def test_foreign_distribution_wheel_rejected(generator, tmp_path, capsys):
    wheels = tmp_path / "wheels"
    write_wheel(wheels, "0.1.0", name="other_project")
    out = tmp_path / "site"
    with pytest.raises(SystemExit) as excinfo:
        run_generator(generator, wheels, out)
    assert excinfo.value.code == 2
    assert "other_project" in capsys.readouterr().err
    assert not (out / "simple").exists()


def test_output_is_deterministic(generator, tmp_path):
    wheels = tmp_path / "wheels"
    write_wheel(wheels, "0.1.0")
    write_wheel(wheels, "0.1.1")
    first, second = tmp_path / "site1", tmp_path / "site2"
    run_generator(generator, wheels, first)
    run_generator(generator, wheels, second)

    for relative in (
        "simple/pku-course-sync/index.html",
        "SHA256SUMS.txt",
    ):
        left = (first / relative).read_bytes()
        right = (second / relative).read_bytes()
        assert left == right


def test_base_url_must_be_absolute(generator, tmp_path):
    wheels = tmp_path / "wheels"
    write_wheel(wheels, "0.1.0")
    with pytest.raises(SystemExit) as excinfo:
        generator.main(
            ["--wheels", str(wheels), "--out", str(tmp_path / "site"), "--base-url", "relative/"]
        )
    assert excinfo.value.code == 2


def test_local_http_base_url_supported_for_fixture_verification(generator, tmp_path):
    """The local simple-index fixture uses the same generator with an http base."""
    wheels = tmp_path / "wheels"
    wheel = write_wheel(wheels, "0.1.0")
    out = tmp_path / "site"
    base = "http://127.0.0.1:8802/"
    run_generator(generator, wheels, out, base_url=base)

    html = (out / "simple" / "pku-course-sync" / "index.html").read_text(encoding="utf-8")
    assert f'href="{base}wheels/{wheel.name}#sha256={sha256_of(wheel)}"' in html


def test_empty_wheels_directory_rejected(generator, tmp_path, capsys):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    with pytest.raises(SystemExit) as excinfo:
        run_generator(generator, wheels, tmp_path / "site")
    assert excinfo.value.code == 2
    assert "wheel" in capsys.readouterr().err


def test_build_site_reusable_directly(generator, tmp_path):
    """The fixture/verification legs call build_site() without argparse."""
    wheels = tmp_path / "wheels"
    wheel = write_wheel(wheels, "0.1.0")
    out = tmp_path / "site"
    generator.build_site(
        wheels_dir=wheels,
        out_dir=out,
        base_url=DEFAULT_BASE_URL,
    )

    assert (out / "wheels" / wheel.name).is_file()
    assert "Links for pku-course-sync" in (
        out / "simple" / "pku-course-sync" / "index.html"
    ).read_text(encoding="utf-8")

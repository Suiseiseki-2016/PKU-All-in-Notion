"""stdio MCP server for read-only access to the local PKU course tree."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP

from pku_sync.config import Settings
from pku_sync.mcp_tools import LocalCourseTools

mcp = FastMCP(
    "pku-course-sync",
    instructions=(
        "Read-only local PKU Teaching Network data. Use the separate official "
        "Notion MCP server for every Notion read or write."
    ),
)


def _tools() -> LocalCourseTools:
    return LocalCourseTools(Settings().data_dir)


@mcp.tool()
def health() -> dict:
    """Check DATA_DIR availability and count locally synced courses."""
    return _tools().health()


@mcp.tool()
def list_courses() -> list[dict]:
    """List locally synced courses and their folder names."""
    return _tools().list_courses()


@mcp.tool()
def list_course_files(
    course_folder: str = "",
    section: str = "all",
) -> list[dict]:
    """List course artifacts by section without reading attachment contents."""
    return _tools().list_files(course_folder, section)  # type: ignore[arg-type]


@mcp.tool()
def read_course_text(relative_path: str, max_chars: int = 50_000) -> dict:
    """Read one .md/.json/.log/.txt artifact below DATA_DIR."""
    return _tools().read_text(relative_path, max_chars)


@mcp.tool()
def list_recording_status(course_id: str = "") -> list[dict]:
    """Show indexed/downloaded/transcribed/notes-ready state for recordings."""
    return _tools().recording_status(course_id)


@mcp.tool()
def get_course_summary(course: str = "") -> dict:
    """Read course summary markdown and lecture coverage by course ID or name."""
    return _tools().get_course_summary(course)


@mcp.tool()
def get_lecture_notes(course: str, lecture: str = "") -> list[dict]:
    """Read generated lecture notes, filtering by lecture title or date."""
    return _tools().get_lecture_notes(course, lecture)


@mcp.tool()
def search_course_knowledge(query: str, course: str = "") -> list[dict]:
    """Find ranked snippets in notes, transcripts, assignments and notices."""
    return _tools().search_course_knowledge(query, course)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

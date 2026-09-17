"""stdio MCP server for read-only access to the local PKU course tree."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .config import Settings
from .mcp_tools import LocalCourseTools

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
def get_daily_status(max_chars: int = 30_000) -> dict:
    """Return the latest deterministic daily log and generated local summary."""
    return _tools().daily_status(max_chars)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

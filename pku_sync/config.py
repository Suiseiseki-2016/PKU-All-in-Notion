from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # CWD first for the normal repo-root run, then the package's own root:
        # an external scheduler or service may start the process anywhere, and
        # the chain must still find its .env sitting next to the project.
        env_file=(".env", str(Path(__file__).resolve().parent.parent / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    pku_username: str = ""
    pku_password: str = ""

    data_dir: Path = Path("data")

    course_allowlist: str = ""
    course_denylist: str = ""

    # Blackboard course codes start with a 5-digit term, e.g. 26271 is
    # "2026-27 academic year, term 1". Empty means auto-detect the newest term
    # present in the account. Not named `term`: pydantic-settings matches env
    # vars case-insensitively, so that would inherit the shell's TERM.
    course_term: str = ""
    include_past_terms: bool = False

    openai_base_url: str = "https://openrouter.ai/api/v1"
    openai_api_key: str = ""
    notes_model: str = "qwen/qwen3.5-397b-a17b"

    # Backend for one-shot LLM calls (daily summary; batch content later).
    # "auto" = the OpenAI-compatible API when OPENAI_API_KEY is set, else a
    # logged-in agent CLI used as a plain text backend (claude, codex, droid).
    llm_provider: str = "auto"
    llm_model: str = ""  # empty = provider default; openai falls back to notes_model
    llm_timeout: int = 600

    # Agent host used for MCP-backed Notion work. Each host stores its own
    # OAuth credential; a user only authenticates the host they actually use.
    # claude is the project default; codex and factory (droid) are first-class.
    agent_host: str = "claude"

    # Internal integration token for the native Notion REST client
    # fallback. The default path uses the official Notion MCP server instead.
    notion_token: str = ""
    notion_wrong_answer_hub_id: str = ""

    # Optional self-hosted REST OAuth fallback. Normal users do not need these:
    # Claude, Codex, or Factory owns the official Notion MCP OAuth session.
    notion_oauth_client_id: str = ""
    notion_oauth_client_secret: str = ""

    whisper_model: str = "small"
    whisper_device: str = "cuda"
    whisper_compute_type: str = "float16"

    # Where `process` sends a recording: "local" runs faster-whisper on this
    # machine (default, unchanged behavior); "cloud" is the M2 transcription
    # service — audio-only upload, deleted right after transcription
    # (docs/SERVICE_PLAN.md §3.1-3.2). Until M2 ships, cloud refuses loudly.
    transcription_backend: str = "local"

    # M2 cloud wrapper (docs/SERVICE_PLAN.md §1.7, §3.2-3.3): audio is
    # uploaded to OUR relay only, never to a third-party API; the relay
    # holds the upstream ASR key and meters minutes per platform account.
    # All AI is proxied by the platform -- users never see any key.
    # PLATFORM_TOKEN is the account session the app writes automatically
    # at activation (devs may fill it by hand); empty values keep the
    # cloud backend refusing loudly until the relay is deployed.
    cloud_transcribe_url: str = "https://pku.aeoluswu.info/v1/transcribe"
    platform_token: str = ""

    delete_video_after_processing: bool = True

    @property
    def allowlist(self) -> set[str]:
        return _split_ids(self.course_allowlist)

    @property
    def denylist(self) -> set[str]:
        return _split_ids(self.course_denylist)

    def course_selected(self, course_id: str) -> bool:
        if self.denylist and course_id in self.denylist:
            return False
        if self.allowlist:
            return course_id in self.allowlist
        return True


def _split_ids(raw: str) -> set[str]:
    return {part.strip() for part in raw.split(",") if part.strip()}


settings = Settings()

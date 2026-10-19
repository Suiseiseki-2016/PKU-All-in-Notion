"""Deployment entrypoint: build the relay app from environment config.

    uvicorn --factory server.main:app --host 0.0.0.0 --port 8000

The accounts DB lives on a mounted volume (``RELAY_DB_PATH``, default
``/data/accounts.db``). The upstream ASR injection point stays
unconfigured until the vendor key lands -- the endpoint refuses 503
rather than half-working (SERVICE_PLAN §3.3).
"""

from __future__ import annotations

import os
from pathlib import Path

from .asr import BailianFileAsr, configure_asr_audit_logging
from .llm import BailianLlm
from .notion_oauth import NotionOAuth
from . import mail, relay


def app():
    # Surface the transient OSS temp-object audit lines on stdout at INFO by
    # default (the native uvicorn start path; see server/asr.py).
    configure_asr_audit_logging()
    db_path = Path(os.environ.get("RELAY_DB_PATH", "/data/accounts.db"))
    llm = BailianLlm.from_environment()
    try:
        ab_account_id = int(os.environ["LLM_AB_TEST_ACCOUNT_ID"])
        if ab_account_id <= 0:
            ab_account_id = None
    except (KeyError, ValueError):
        ab_account_id = None
    llm_plus = (BailianLlm(llm.api_key, llm.base_url, "qwen3.7-plus")
                if llm else None)
    llm_max = (BailianLlm(llm.api_key, llm.base_url, "qwen3.8-max-0902")
               if llm else None)
    notion_oauth = NotionOAuth.from_environment()
    try:
        mail_config = mail.MailConfig.from_environment()
    except mail.MailError:
        mail_config = None
    mail_sender = mail.make_sender(mail_config) if mail_config else None
    return relay.create_relay_app(
        db_path,
        streaming_asr_fn=BailianFileAsr.from_environment(),
        llm_fn=llm.complete if llm else None,
        llm_plus_fn=llm_plus.complete if llm_plus else None,
        llm_max_fn=llm_max.complete if llm_max else None,
        llm_ab_test_account_id=ab_account_id,
        notion_exchange_fn=notion_oauth.exchange if notion_oauth else None,
        mail_config=mail_config,
        mail_sender=mail_sender,
    )

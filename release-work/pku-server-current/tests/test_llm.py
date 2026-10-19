from __future__ import annotations

import httpx

from server.llm import BailianLlm


def test_bailian_llm_uses_reported_tokens_for_points():
    seen = {}

    def post(url, headers, json, timeout):
        seen.update(url=url, headers=headers, json=json, timeout=timeout)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "答案"}}],
                "usage": {"prompt_tokens": 10_000, "completion_tokens": 1_000},
            },
        )

    result = BailianLlm("secret", "https://bailian.example/v1", post=post).complete(
        "qa", "system", "question"
    )
    assert result.content == "答案"
    assert (result.input_tokens, result.output_tokens) == (10_000, 1_000)
    assert result.millipoints == 40  # ¥0.004 × 10,000 milli-points/RMB
    assert seen["url"].endswith("/chat/completions")
    assert seen["json"]["model"] == "qwen3.5-flash"

"""Metered Bailian OpenAI-compatible LLM calls."""

from __future__ import annotations

import json
import ipaddress
import math
import os
import socket
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

POINT_BUDGET_RMB = 10
MILLIPOINTS_PER_RMB = 10_000
MODEL_PRICES_RMB_PER_MILLION = {
    "qwen3.5-flash": (0.2, 2.0),
    "qwen3.7-plus": (2.0, 8.0),
    "qwen3.8-max-0902": (12.0, 36.0),
}
SELECTABLE_NOTE_MODELS = frozenset(MODEL_PRICES_RMB_PER_MILLION)
# The configured price rows are the vendor's base context tiers. Refuse a
# larger response rather than silently charging that response at the base rate.
BASE_PRICE_CONTEXT_LIMIT = {"qwen3.5-flash": 128_000, "qwen3.7-plus": 256_000, "qwen3.8-max-0902": 1_000_000}
ALLOWED_OPERATIONS = {"notes", "summary", "quiz", "grade", "qa"}
# Operations whose relay contract is exactly one JSON object; the client
# parses their content with json.loads (exercise_organizer._parse_quiz and
# the strict pku-e2e-grade-v1 grader contract), so the adapter normalizes
# away any reasoning wrapper for these only. Prose operations (notes,
# summary, qa) pass through untouched.
JSON_CONTRACT_OPERATIONS = frozenset({"quiz", "grade"})
# qwen3-series hybrid models toggle thinking per request via
# `enable_thinking` on the DashScope-compatible endpoint; other/unknown
# model families must not receive the (potentially rejected) parameter.
THINKING_CONTROL_MODEL_PREFIX = "qwen3"
_DEFAULT_POST = httpx.post
_A_RECORD_TTL_SECONDS = 60
_NO_A_RETRY_SECONDS = 10
_ADDRESS_COOLDOWN_SECONDS = 90


def _now() -> float:
    return time.monotonic()


def _resolve_a_records(host: str, port: int) -> tuple[str, ...]:
    """Use current DNS A records; never keep vendor IPs in configuration."""
    answers = socket.getaddrinfo(host, port, family=socket.AF_INET,
                                 type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(str(ipaddress.IPv4Address(item[4][0])) for item in answers))


def _proxy_configured() -> bool:
    # The IP URL would change a proxy's CONNECT target. Preserve the normal
    # hostname route whenever the process has an explicit proxy configured.
    return any(os.environ.get(name) for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ))


def _enable_thinking_control(model: str) -> bool:
    """Whether the model accepts the enable_thinking request parameter."""
    return (model or "").startswith(THINKING_CONTROL_MODEL_PREFIX)


def _matching_brace_end(text: str, start: int) -> int | None:
    """Index of the ``}`` closing the object opened at ``start``, or None.

    Honors string contents so a { or } inside a JSON string value never
    throws off the brace balance.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _balanced_object_spans(text: str) -> list[tuple[int, int]]:
    """All top-level brace-balanced {...} spans, honoring string contents.

    Once an object closes at depth zero, scanning resumes after it, so
    objects nested inside a value belong to the enclosing span only.
    """
    spans: list[tuple[int, int]] = []
    position = 0
    while position < len(text):
        if text[position] != "{":
            position += 1
            continue
        end = _matching_brace_end(text, position)
        if end is None:
            # An unterminated object swallows the rest of the text: no
            # further balanced span exists after it.
            break
        spans.append((position, end))
        position = end + 1
    return spans


def _strip_reasoning_wrapper(content: str) -> str:
    """Normalize a qwen reasoning-wrapper response to one JSON object.

    Thinking-style output wraps the actual quiz/grade JSON in a thinking
    preamble and/or a ```json fence; both would fail the client's strict
    json.loads gate AFTER the metered charge (the Window D first run: 3811
    completion tokens for a ~400-token JSON). This extracts the balanced
    {...} span that actually parses as JSON and returns it unchanged in its
    original serialization — but ONLY when that object is the ONLY one in
    the content: with two or more candidates (an example object embedded in
    prose ahead of the real answer), silently returning any one of them
    could hand the client the wrong result, so the content passes through
    untouched and the client's strict gate rejects it honestly. Content
    that is already a plain JSON object — or contains no parseable JSON
    object at all — passes through untouched, so no response is ever
    fabricated or mangled.
    """
    text = content.strip()
    objects: list[tuple[int, int]] = []
    for start, end in _balanced_object_spans(text):
        try:
            json.loads(text[start : end + 1])
        except ValueError:
            continue
        objects.append((start, end))
    if len(objects) != 1:
        return content
    start, end = objects[0]
    return text[start : end + 1]


class LlmError(RuntimeError):
    pass


@dataclass(frozen=True)
class Completion:
    content: str
    input_tokens: int
    output_tokens: int
    millipoints: int
    model: str


class BailianLlm:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str = "qwen3.5-flash",
        post: Callable[..., httpx.Response] = httpx.post,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = "qwen3.8-max-0902" if model == "qwen3.8-max" else model
        self.post = post
        # Injected post functions are used by existing contract tests and
        # custom deployments; only the built-in HTTP path selects DNS A records.
        self._select_a_records = post is _DEFAULT_POST
        self._address_lock = threading.Lock()
        self._a_records: tuple[str, ...] = ()
        self._a_expires_at = 0.0
        self._unhealthy_until: dict[str, float] = {}
        self._last_success: dict[str, float] = {}

    def _addresses(self, host: str, port: int) -> tuple[str, ...]:
        now = _now()
        with self._address_lock:
            if now < self._a_expires_at:
                return self._a_records
        try:
            resolved = _resolve_a_records(host, port)
        except OSError:
            resolved = ()
        with self._address_lock:
            self._a_records = resolved
            self._a_expires_at = now + (
                _A_RECORD_TTL_SECONDS if resolved else _NO_A_RETRY_SECONDS
            )
            self._unhealthy_until = {
                ip: until for ip, until in self._unhealthy_until.items() if ip in resolved
            }
            self._last_success = {
                ip: at for ip, at in self._last_success.items() if ip in resolved
            }
            return self._a_records

    def _choose_address(self, host: str, port: int) -> str | None:
        addresses = self._addresses(host, port)
        now = _now()
        with self._address_lock:
            healthy = [
                ip for ip in addresses if self._unhealthy_until.get(ip, 0) <= now
            ]
            if not healthy:
                return None
            # Keep using a recently successful A record while it remains
            # healthy; otherwise retain DNS order. A timed-out address cools
            # briefly and the next job attempt picks an alternate record.
            return max(healthy, key=lambda ip: self._last_success.get(ip, 0))

    def _post_to_address(self, url: str, address: str, host: str,
                         headers: dict[str, str], payload: dict,
                         timeout: httpx.Timeout) -> httpx.Response:
        parts = urlsplit(url)
        port = parts.port or 443
        address_netloc = address if port == 443 else f"{address}:{port}"
        address_url = urlunsplit((parts.scheme, address_netloc, parts.path,
                                  parts.query, parts.fragment))
        original_headers = {**headers, "Host": parts.netloc}
        # httpcore 1.0.9 passes this request extension to TLS start_tls as
        # server_hostname. It is both SNI and the hostname checked by the
        # default SSL context, even though TCP connects to an A-record IP.
        with httpx.Client(verify=True, trust_env=True, follow_redirects=False,
                          timeout=timeout) as client:
            request = client.build_request("POST", address_url,
                                           headers=original_headers, json=payload)
            request.extensions["sni_hostname"] = host
            return client.send(request)

    def _post_vendor(self, url: str, headers: dict[str, str], payload: dict,
                     timeout: httpx.Timeout) -> httpx.Response:
        parts = urlsplit(url)
        if (not self._select_a_records or _proxy_configured() or
                parts.scheme != "https" or not parts.hostname or parts.username):
            return self.post(url, headers=headers, json=payload, timeout=timeout)
        try:
            address = self._choose_address(parts.hostname, parts.port or 443)
        except ValueError:
            address = None
        if address is None:
            return self.post(url, headers=headers, json=payload, timeout=timeout)
        try:
            response = self._post_to_address(url, address, parts.hostname,
                                             headers, payload, timeout)
        except httpx.ConnectTimeout:
            with self._address_lock:
                self._unhealthy_until[address] = _now() + _ADDRESS_COOLDOWN_SECONDS
            raise
        else:
            with self._address_lock:
                self._unhealthy_until.pop(address, None)
                self._last_success[address] = _now()
            return response

    @classmethod
    def from_environment(cls) -> "BailianLlm | None":
        api_key = os.environ.get("DASHSCOPE_API_KEY")
        base_url = os.environ.get("BAILIAN_OPENAI_BASE_URL")
        if not api_key or not base_url:
            return None
        return cls(api_key, base_url, os.environ.get("BAILIAN_LLM_MODEL", "qwen3.5-flash"))

    def complete(self, operation: str, system: str, user: str) -> Completion:
        if operation not in ALLOWED_OPERATIONS:
            raise LlmError("unsupported AI operation")
        if self.model not in MODEL_PRICES_RMB_PER_MILLION:
            raise LlmError("unsupported AI model")
        if len(system) + len(user) > 200_000:
            raise LlmError("request is too large")
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        # Qwen3 hybrid models think by default, which burns output tokens on
        # a reasoning preamble and wraps the JSON answer (the Window D first
        # run paid 3811 output tokens for a ~400-token JSON). Disable the
        # thinking mode explicitly so the metered output is the answer.
        if _enable_thinking_control(self.model):
            payload["enable_thinking"] = False
        # JSON-contract operations must come back as exactly one valid JSON
        # object: the client parses quiz/grade content with json.loads, and
        # anything else only fails AFTER the metered charge (the Window C
        # first run burned 11 millipoints on a quiz the client could not
        # parse). Bailian's OpenAI-compatible endpoint accepts the
        # response_format json_object mode for the Qwen-Flash family in the
        # non-thinking mode forced above, so the vendor is constrained to
        # valid JSON output instead of prompt discipline alone (the fixed
        # client prompts already carry the literal JSON keyword the API
        # requires). json_object guarantees valid JSON, NOT the quiz/grade
        # schema — the client's shape validation stays authoritative. A
        # model family that rejects the parameter fails loud at request
        # time (LlmError -> 502 pre-charge), never silently. Prose
        # operations (notes, summary, qa) keep byte-identical payloads.
        if operation in JSON_CONTRACT_OPERATIONS:
            payload["response_format"] = {"type": "json_object"}
        response = self._post_vendor(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"}, payload=payload,
            # A dead TLS connection must fail quickly so the durable job can
            # retry. Keep a longer read window for legitimate generation.
            timeout=httpx.Timeout(180, connect=15),
        )
        if response.status_code != 200:
            raise LlmError(f"Bailian LLM failed (HTTP {response.status_code})")
        body: dict[str, Any] = response.json()
        reported_model = body.get("model")
        if self.model in {"qwen3.7-plus", "qwen3.8-max-0902"} and reported_model is None:
            raise LlmError("Bailian LLM omitted selected model")
        if reported_model is not None and reported_model != self.model:
            raise LlmError("Bailian LLM returned a different model")
        choice = (body.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content", "").strip()
        finish_reason = choice.get("finish_reason")
        usage = body.get("usage") or {}
        if (not isinstance(usage, dict) or
                type(usage.get("prompt_tokens")) is not int or
                type(usage.get("completion_tokens")) is not int):
            raise LlmError("Bailian LLM returned invalid usage")
        input_tokens = usage["prompt_tokens"]
        output_tokens = usage["completion_tokens"]
        if not content or input_tokens <= 0 or output_tokens <= 0:
            raise LlmError("Bailian LLM returned an invalid completion")
        if input_tokens + output_tokens >= BASE_PRICE_CONTEXT_LIMIT[self.model]:
            raise LlmError("Bailian LLM usage exceeds configured price tier")
        # A JSON-contract answer that did not finish ("length" truncation,
        # a content filter, ...) would be served as content and only fail
        # the client's strict JSON gate after the charge; fail loud
        # pre-settlement instead. Prose operations keep today's behavior
        # (truncated prose is served as prose), and an absent finish_reason
        # is not proof of truncation, so only a present, non-"stop" reason
        # fails the call.
        if (
            operation in JSON_CONTRACT_OPERATIONS
            and finish_reason is not None
            and finish_reason != "stop"
        ):
            raise LlmError("Bailian LLM completion did not finish cleanly")
        # Defense-in-depth: even with thinking disabled, a residual reasoning
        # wrapper (preamble/fence) would fail the client's strict JSON gate
        # after the charge. Normalize JSON-contract operations only; prose
        # operations pass through untouched.
        if operation in JSON_CONTRACT_OPERATIONS:
            content = _strip_reasoning_wrapper(content)
        input_price, output_price = MODEL_PRICES_RMB_PER_MILLION[self.model]
        # Exact decimal cost: in float arithmetic a hand-computable charge
        # like ¥0.28 becomes 0.2800000000000000277…, and ceil() crosses the
        # integer boundary to 2801 millipoints. The charge must equal the
        # price row's hand calculation, so the money math is decimal.
        cost = (
            Decimal(input_tokens) * Decimal(str(input_price))
            + Decimal(output_tokens) * Decimal(str(output_price))
        ) / 1_000_000
        return Completion(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            millipoints=max(1, math.ceil(cost * MILLIPOINTS_PER_RMB)),
            model=self.model,
        )

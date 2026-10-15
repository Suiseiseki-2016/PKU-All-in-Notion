"""Independent, read-only live oracles; never consult the application's cache.

Credentials are environment references, e.g. ``credentials.account_token =
{"env": "E2E_ACCOUNT_TOKEN"}``. Portal authentication must be provisioned as a
session cookie: this module deliberately cannot log in or perform any POST.
Missing billing instrumentation is a blocker, not a zero-cost measurement.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from product_e2e.core import ResourceBlocked

HOSTS = {"account": "pku.aeoluswu.info", "notion": "api.notion.com",
         "portal": "course.pku.edu.cn"}
_SECRET_KEYS = re.compile(r"password|secret|token|authorization|cookie|auth.?code|email|username", re.I)
_LOGIN = re.compile(r"iaaa\.pku\.edu\.cn|oauthlogin|<input[^>]+type=[\"']password|"
                    r"<form[^>]+(?:login|signin)|name=[\"'](?:userName|password)", re.I)
_ID = re.compile(r"^[0-9a-fA-F-]{32,36}$")


class OracleError(RuntimeError):
    """Safe diagnostic: no remote body, signed URL or credential in exceptions."""

    def __init__(self, code: str, *, service: str = "", status: int | None = None,
                 retry_after_seconds: int | None = None):
        self.code, self.service, self.status = code, service, status
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"{service or 'oracle'}:{code}" + (f":{status}" if status else ""))


class Oracle:
    def __init__(self, config: dict, evidence_dir: Path):
        self.config = config
        self.evidence_dir = Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.hosts: set[str] = set()
        self.observed_hosts = self.hosts
        self.requests: list[dict] = []
        self._secrets: set[str] = set()
        self._sequence = 0
        self.options = config.get("oracles", {})
        if not isinstance(self.options, dict):
            raise ResourceBlocked("oracle_configuration_invalid")

    def _credential(self, name: str) -> str:
        credentials = self.config.get("credentials", {})
        reference = credentials.get(name) if isinstance(credentials, dict) else None
        if (not isinstance(reference, dict) or set(reference) != {"env"}
                or not isinstance(reference["env"], str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", reference["env"])):
            raise ResourceBlocked(f"{name}_environment_reference_required")
        value = os.environ.get(reference["env"], "")
        if not value or "\r" in value or "\n" in value:
            raise ResourceBlocked(f"{name}_environment_unavailable")
        self._secrets.add(value)
        # A cookie header contains several individual secrets, not just one.
        if name == "portal_cookie":
            self._secrets.update(part.split("=", 1)[1].strip() for part in value.split(";")
                                 if "=" in part and part.split("=", 1)[1].strip())
        return value

    def _redact(self, value):
        if isinstance(value, dict):
            return {key: "[redacted]" if _SECRET_KEYS.search(str(key)) else self._redact(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, str):
            for secret in sorted(self._secrets, key=len, reverse=True):
                value = value.replace(secret, "[redacted]")
            # Signed URLs in external Notion file fields must not enter reports.
            value = re.sub(r"https?://[^\s\"<>]+", lambda match:
                           match[0].split("?", 1)[0].split("#", 1)[0], value)
        return value

    def _record(self, service: str, path: str, status: int, body: bytes, value=None) -> None:
        self._sequence += 1
        record = {"service": service, "host": HOSTS[service], "path": self._redact(path),
                  "method": "GET", "status": status, "body_bytes": len(body),
                  "body_sha256": hashlib.sha256(body).hexdigest(),
                  "observed_at": datetime.now(timezone.utc).isoformat()}
        if value is not None:
            record["readback"] = self._redact(value)
        self.requests.append(record)
        target = self.evidence_dir / f"oracle-{self._sequence:04}.json"
        # Exclusive creation prevents overwriting evidence from earlier attempts.
        with target.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)

    def get(self, service: str, path: str, params: dict | None = None):
        # Only genuine Notion read throttling is retried. Every failed response
        # is recorded first; permissions, restricted connections, account faults
        # and invalid/long wait values remain terminal. Never retry a write.
        for attempt in range(3):
            try:
                return self._get_once(service, path, params)
            except OracleError as exc:
                if (service != 'notion' or exc.status not in {429, 529}
                        or exc.retry_after_seconds is None or attempt == 2):
                    raise
                time.sleep(exc.retry_after_seconds)

    def _record_transport_failure(self, service, path, exc, started, status, body):
        self._sequence += 1
        record = {'service': service, 'host': HOSTS[service], 'path': self._redact(path),
                  'method': 'GET', 'status': status, 'response_received': status is not None,
                  'error_type': type(exc).__name__, 'error_code': 'transport_unavailable',
                  'elapsed_seconds': time.monotonic() - started,
                  'observed_at': datetime.now(timezone.utc).isoformat()}
        if status is not None:
            record.update(partial_body=True, body_bytes=len(body),
                          body_sha256=hashlib.sha256(body).hexdigest())
        self.requests.append(record)
        with (self.evidence_dir / f'oracle-{self._sequence:04}.json').open('x', encoding='utf-8') as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)

    def _get_once(self, service: str, path: str, params: dict | None = None):
        """GET an allowlisted HTTPS metadata endpoint; HTML only for the portal."""
        if service not in HOSTS:
            raise OracleError("service_not_allowlisted")
        parsed = urlsplit(path)
        if (parsed.scheme or parsed.netloc or not path.startswith("/")
                or path.startswith("//") or parsed.query or parsed.fragment
                or "\\" in path or ".." in path.split("/")):
            raise OracleError("unsafe_endpoint", service=service)
        allowed = {"account": ("/v1/",), "notion": ("/v1/pages/", "/v1/blocks/", "/v1/users/me"),
                   "portal": ("/learn/api/public/v1/", "/webapps/portal/execute/",
                              "/webapps/bb-streammedia-hqy-BBLEARN/videoList.action",
                              "/webapps/blackboard/content/listContent.jsp",
                              "/webapps/assignment/uploadAssignment")}
        if not path.startswith(allowed[service]):
            raise OracleError("metadata_endpoint_required", service=service)
        if params and any(_SECRET_KEYS.search(str(key)) for key in params):
            raise OracleError("secret_query_forbidden", service=service)
        if service == "portal" and params and any(str(key).casefold() in {
                "action", "dispatch", "newattempt", "delete"} for key in params):
            raise OracleError("state_changing_portal_get_forbidden", service=service)
        headers = {"Accept": "application/json, text/html;q=0.5"}
        if service == "portal":
            headers["Cookie"] = self._credential("portal_cookie")
        else:
            headers["Authorization"] = "Bearer " + self._credential(service + "_token")
        if service == "notion":
            headers["Notion-Version"] = "2022-06-28"
        tls = True
        if service == "portal" and self.options.get("portal", {}).get("ca_bundle"):
            import ssl
            tls = ssl.create_default_context(cafile=self.options["portal"]["ca_bundle"])
        # No inherited proxy, TLS downgrade, redirect, app transport or write.
        started, status, body = time.monotonic(), None, bytearray()
        try:
            with httpx.Client(timeout=httpx.Timeout(20, connect=8), follow_redirects=False,
                              trust_env=False, verify=tls) as client:
                with client.stream("GET", "https://" + HOSTS[service] + path,
                                   headers=headers, params=params) as response:
                    self.hosts.add(HOSTS[service])
                    status = response.status_code
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 4 * 1024 * 1024:
                            raise OracleError("metadata_response_too_large", service=service)
                    status = response.status_code
                    content_type = response.headers.get("content-type", "").lower()
                    retry_after = response.headers.get('Retry-After', '')
        except httpx.HTTPError as exc:
            self._record_transport_failure(service, path, exc, started, status, body)
            raise OracleError("transport_unavailable", service=service) from None
        text = bytes(body).decode("utf-8", errors="replace")
        if (300 <= status < 400 or status in {401, 403}
                or (("text/html" in content_type or text.lstrip().startswith("<")) and _LOGIN.search(text))):
            self._record(service, path, status, bytes(body))
            if service == "portal":
                raise ResourceBlocked("independent_portal_session_not_authorized")
            raise OracleError("authentication_or_login_html", service=service, status=status)
        if status != 200:
            delay, throttle = None, None
            if service == 'notion' and status in {429, 529}:
                try:
                    error = json.loads(text)
                    reason = (error.get('additional_data') or {}).get('rate_limit_reason')
                    eligible = (error.get('code') in {'rate_limited', 'service_overload'}
                                and reason != 'public_api_request_blocked')
                    if eligible and re.fullmatch(r'[0-9]{1,2}', retry_after) and int(retry_after) <= 60:
                        delay = int(retry_after)
                    throttle = {'retry_after_seconds': delay,
                                'restricted_connection': reason == 'public_api_request_blocked'}
                except (ValueError, AttributeError, TypeError):
                    pass
            self._record(service, path, status, bytes(body), throttle)
            if service == "portal" and status in {404, 405, 501}:
                raise ResourceBlocked("independent_portal_metadata_endpoint_unavailable")
            raise OracleError("http_status", service=service, status=status, retry_after_seconds=delay)
        try:
            value = json.loads(text)
        except ValueError:
            self._record(service, path, status, bytes(body))
            if service == "portal" and "text/html" in content_type and "<" in text:
                return text
            raise OracleError("invalid_json_or_unexpected_html", service=service) from None
        if not isinstance(value, (dict, list)):
            self._record(service, path, status, bytes(body))
            raise OracleError("json_object_or_array_required", service=service)
        self._record(service, path, status, bytes(body), value)
        return value

    @staticmethod
    def _number(value, code: str):
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise OracleError(code)
        return value

    def notion_course_directory(self, home_id: str) -> dict:
        """Fresh GET-only page/parent/source-marker identity, including pagination."""
        if not isinstance(home_id, str) or not _ID.fullmatch(home_id):
            raise OracleError('invalid_notion_directory_id', service='notion')
        root = home_id.replace('-', '').lower()
        children = self._pages('notion', '/v1/blocks/' + quote(home_id, safe='') + '/children')
        courses, titles, seen, unmarked = {}, [], set(), []
        for child in children:
            if child.get('type') != 'child_page':
                continue
            page_id = child.get('id')
            if not isinstance(page_id, str) or not _ID.fullmatch(page_id) or page_id in seen:
                raise OracleError('ambiguous_notion_directory_child', service='notion')
            seen.add(page_id)
            title = child.get('child_page', {}).get('title')
            titles.append(title)
            page = self.get('notion', '/v1/pages/' + quote(page_id, safe=''))
            parent = page.get('parent') or {}
            if (page.get('id', '').replace('-', '').lower() != page_id.replace('-', '').lower()
                    or page.get('archived') or page.get('in_trash')
                    or parent.get('type') != 'page_id'
                    or str(parent.get('page_id', '')).replace('-', '').lower() != root):
                raise OracleError('notion_course_parent_or_availability_disagrees', service='notion')
            blocks = self._pages('notion', '/v1/blocks/' + quote(page_id, safe='') + '/children')
            markers = []
            for block in blocks:
                if block.get('type') != 'paragraph':
                    continue
                for part in block.get('paragraph', {}).get('rich_text', []):
                    url = (part.get('text', {}).get('link') or {}).get('url', '')
                    match = re.fullmatch(r'https://course\.pku\.edu\.cn/#pku-course:(_[0-9]+_[0-9]+)', url)
                    if match:
                        markers.append(match[1])
            if not markers:
                unmarked.append(page_id)
                continue
            if len(markers) != 1 or markers[0] in courses:
                raise OracleError('ambiguous_notion_course_source_marker', service='notion')
            courses[markers[0]] = {'id': page_id, 'title': title, 'parent_id': home_id,
                                    'source_course_id': markers[0]}
        return {'root_id': home_id, 'course_pages': courses, 'child_titles': titles,
                'unmarked_page_ids': unmarked}

    def profile(self) -> dict:
        value = self.get("account", "/v1/profile")
        if (not isinstance(value, dict) or type(value.get("revision")) is not int
                or value["revision"] < 0 or not isinstance(value.get("profile"), dict)):
            raise OracleError("invalid_profile_envelope", service="account")
        return value

    @staticmethod
    def _catalog_envelope(value, course_id: str | None = None):
        if (not isinstance(value, dict) or type(value.get("revision")) is not int
                or value["revision"] < 0 or "snapshot" not in value):
            raise OracleError("invalid_catalog_revision", service="account")
        snapshot = value["snapshot"]
        if value["revision"] == 0:
            if snapshot is not None:
                raise OracleError("zero_revision_nonempty_snapshot", service="account")
            return value
        # This is deliberately independent of the app's validate_snapshot().
        if (not isinstance(snapshot, dict) or snapshot.get("authority") != "course.pku.edu.cn"
                or type(snapshot.get("schema")) is not int or snapshot["schema"] != 1
                or not isinstance(snapshot.get("course_id"), str) or not snapshot["course_id"]
                or (course_id is not None and snapshot["course_id"] != course_id)
                or not isinstance(snapshot.get("course"), dict)
                or snapshot["course"].get("course_id") != snapshot["course_id"]
                or not snapshot["course"].get("name")):
            raise OracleError("invalid_catalog_identity", service="account")
        sections = snapshot.get("sections")
        if not isinstance(sections, dict) or set(sections) != {
                "recordings", "materials", "assignments", "announcements"}:
            raise OracleError("incomplete_catalog_sections", service="account")
        for section in sections.values():
            if (not isinstance(section, dict) or section.get("complete") is not True
                    or not isinstance(section.get("items"), list)):
                raise OracleError("partial_catalog_snapshot", service="account")
            ids = [row.get("source_id") for row in section["items"] if isinstance(row, dict)]
            if (len(ids) != len(section["items"]) or any(not isinstance(i, str) or not i for i in ids)
                    or len(ids) != len(set(ids))):
                raise OracleError("invalid_catalog_source_ids", service="account")
        return value

    def catalog(self) -> dict:
        value = self.get("account", "/v1/catalog")
        rows = value.get("courses") if isinstance(value, dict) else None
        if not isinstance(rows, list):
            raise OracleError("invalid_catalog_list", service="account")
        seen = set()
        for row in rows:
            self._catalog_envelope(row)
            if row["revision"] == 0:
                raise OracleError("uninitialized_catalog_list_entry", service="account")
            cid = row["snapshot"]["course_id"]
            if cid in seen:
                raise OracleError("duplicate_catalog_course", service="account")
            seen.add(cid)
            readback = self._catalog_envelope(self.get("account", "/v1/catalog/" + quote(cid, safe="")), cid)
            if readback != row:
                raise OracleError("catalog_changed_during_readback", service="account")
        return value

    def quota(self) -> dict:
        value = self.get("account", "/v1/quota")
        if not isinstance(value, dict):
            raise OracleError("invalid_quota", service="account")
        for field in ("transcribe_seconds_remaining", "llm_points_remaining"):
            if type(value.get(field)) not in {int, float} or not math.isfinite(value[field]):
                raise OracleError("quota_measurement_missing", service="account")
        return value

    def _pages(self, service: str, path: str, *, params=None):
        results, cursors = [], set()
        query = dict(params or {})
        for _ in range(100):
            value = self.get(service, path, query)
            if not isinstance(value, dict) or not isinstance(value.get("results"), list):
                raise OracleError("invalid_paginated_response", service=service)
            results.extend(value["results"])
            if len(results) > 10000:
                raise OracleError("pagination_bound_exceeded", service=service)
            if service == "notion":
                more, cursor = value.get("has_more"), value.get("next_cursor")
                if type(more) is not bool:
                    raise OracleError("invalid_pagination", service=service)
                if not more:
                    return results
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    raise OracleError("invalid_pagination_cursor", service=service)
                cursors.add(cursor)
                query["start_cursor"] = cursor
            else:
                paging = value.get("paging", {})
                if not isinstance(paging, dict):
                    raise OracleError("invalid_pagination", service=service)
                next_page = paging.get("nextPage")
                if not next_page:
                    return results
                if not isinstance(next_page, str):
                    raise OracleError("invalid_pagination_cursor", service=service)
                next_url = urlsplit(next_page)
                if (next_url.scheme not in {"", "https"}
                        or (next_url.netloc and next_url.hostname != HOSTS[service])
                        or next_url.username or next_url.password or next_url.fragment
                        or next_url.port not in {None, 443} or next_page in cursors):
                    raise OracleError("unsafe_pagination", service=service)
                from urllib.parse import parse_qsl
                cursors.add(next_page)
                path, query = next_url.path, dict(parse_qsl(next_url.query))
        raise OracleError("pagination_bound_exceeded", service=service)

    def portal_courses(self) -> dict:
        # Real independent session, no use of app account catalog/local index.
        self._credential("portal_cookie")
        identity = self.get("portal", "/learn/api/public/v1/users/me")
        if not isinstance(identity, dict) or not isinstance(identity.get("id"), str):
            raise OracleError("portal_session_identity_unverified", service="portal")
        memberships = self._pages("portal", "/learn/api/public/v1/users/me/courses",
                                  params={"expand": "course", "limit": 100})
        courses, seen = [], set()
        for membership in memberships:
            row = membership.get("course") if isinstance(membership, dict) else None
            if not isinstance(row, dict):
                raise OracleError("portal_membership_missing_course", service="portal")
            cid = row.get("id") or membership.get("courseId")
            if not isinstance(cid, str) or not cid or cid in seen or not isinstance(row.get("name"), str):
                raise OracleError("portal_course_identity_invalid", service="portal")
            seen.add(cid)
            courses.append({"course_id": cid, "name": row["name"],
                            "code": row.get("courseId", ""),
                            "term": row["term"].get("name", "") if isinstance(row.get("term"), dict) else ""})
        return {"origin": "independent-teaching-portal-read", "courses": courses,
                "course_ids": [row["course_id"] for row in courses]}

    def portal_course(self, course_id: str) -> dict:
        """Read teaching metadata independently, without entering a video player.

        REST is used where Blackboard exposes it; content and recording tables
        are independently parsed from the authenticated HTML a student sees.
        Incomplete/unsupported HTML is a blocker rather than an empty section.
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            raise ResourceBlocked("independent_portal_html_parser_unavailable") from None
        from urllib.parse import parse_qs

        if not isinstance(course_id, str) or not re.fullmatch(r"_\d+_\d+", course_id):
            raise OracleError("invalid_portal_course_id", service="portal")
        api = "/learn/api/public/v1/courses/" + quote(course_id, safe="")

        def html_text(raw: str, multiline=False):
            soup = BeautifulSoup(raw, "html.parser")
            for tag in soup.find_all(["script", "style"]):
                tag.decompose()
            if multiline:
                for br in soup.find_all("br"):
                    br.replace_with("\n")
                for tag in soup.find_all(["p", "div", "li", "tr", "h1", "h2", "h3", "h4"]):
                    tag.append("\n")
                return "\n".join(" ".join(line.split()) for line in soup.get_text().splitlines()
                                 if line.strip())
            return " ".join(soup.get_text(" ", strip=True).split())

        video_html = self.get("portal", "/webapps/bb-streammedia-hqy-BBLEARN/videoList.action",
                              {"course_id": course_id, "mode": "view", "sortDir": "ASCENDING",
                               "editPaging": "true"})
        if (not isinstance(video_html, str) or re.search(r"下一页|nextPage|paging_next", video_html, re.I)
                or (sum(label in video_html for label in ("名称", "时间", "教师", "操作")) < 2
                    and not re.search(r"暂无(?:录像|录播|视频)|没有(?:录像|录播|视频)", video_html))):
            raise ResourceBlocked("independent_recording_directory_incomplete")
        lessons, starts, columns = [], set(), []
        for row in BeautifulSoup(video_html, "html.parser").find_all("tr"):
            cells = row.find_all(["th", "td"], recursive=False)
            values = [html_text(str(cell)) for cell in cells]
            link = row.find("a", href=re.compile(r"playVideo\.action"))
            if not link:
                if sum(value in {"名称", "时间", "教师", "操作"} for value in values) >= 2:
                    columns = values
                continue
            mapped = {}
            for index, value in enumerate(values):
                labelled = re.match(r"^(名称|时间|教师|操作)\s*[:：]\s*(.*)$", value)
                if labelled:
                    mapped[labelled[1]] = labelled[2]
                elif index < len(columns):
                    mapped[columns[index]] = value
            start, title = mapped.get("时间", ""), mapped.get("名称", "")
            try:
                datetime.fromisoformat(start)
            except ValueError:
                raise ResourceBlocked("independent_recording_timestamp_unreadable") from None
            if not title or start in starts:
                raise OracleError("ambiguous_portal_recording", service="portal")
            starts.add(start)
            identity = json.dumps({"course": course_id, "start": start}, ensure_ascii=False,
                                  sort_keys=True, separators=(",", ":")).encode()
            lessons.append({"source_id": "pku:recording:" + hashlib.sha256(identity).hexdigest()[:32],
                            "title": title, "date": start.split("T")[0].split(" ")[0],
                            "recorded_at": start})
        lessons.sort(key=lambda row: (row["recorded_at"], row["title"]))
        announcements = []
        for raw in self._pages("portal", api + "/announcements", params={"limit": 100}):
            if not isinstance(raw, dict) or not raw.get("id") or not raw.get("title"):
                raise OracleError("invalid_portal_announcement", service="portal")
            announcements.append({"source_id": f"pku:{course_id}:announcements:{raw['id']}",
                                  "id": raw["id"], "title": raw["title"],
                                  "posted_at": raw.get("created") or raw.get("modified") or "",
                                  "body_text": html_text(raw.get("body") or "", multiline=True)})
        announcements.sort(key=lambda row: row["posted_at"], reverse=True)
        contents, visited = [], set()

        def folder(content_id: str, depth=0):
            if content_id in visited or depth > 10 or len(visited) > 1000:
                raise ResourceBlocked("independent_content_tree_incomplete")
            visited.add(content_id)
            raw = self.get("portal", "/webapps/blackboard/content/listContent.jsp",
                           {"course_id": course_id, "content_id": content_id})
            if not isinstance(raw, str):
                raise ResourceBlocked("independent_content_html_required")
            soup = BeautifulSoup(raw, "html.parser")
            container = soup.find("ul", id="content_listContainer")
            if container is None:
                message = soup.select_one("#content #containerdiv .noItems.container-empty .main-message")
                forms = soup.find_all("form", attrs={"name": "contentForm"})
                query = parse_qs(urlsplit(str(forms[0].get("action", ""))).query) if len(forms) == 1 else {}
                if (message and message.get_text(strip=True) == "没有要显示的内容。"
                        and query.get("course_id") == [course_id]
                        and query.get("content_id") == [content_id]):
                    return
                raise ResourceBlocked("independent_content_directory_incomplete")
            for item in container.find_all("li", recursive=False):
                if "liItem" not in item.get("class", []):
                    continue
                heading, icon = item.find(["h3", "h4"]), item.find("img", alt=True)
                native_id = re.search(r"_\d+_\d+", str(item.get("id", "")))
                if not heading or not native_id:
                    raise ResourceBlocked("independent_content_identity_unreadable")
                title, kind = html_text(str(heading)), str(icon.get("alt", "")) if icon else ""
                details = item.find("div", class_=lambda value: value and "details" in value)
                instructions = html_text(str(details)) if details else ""
                instructions = re.sub(r"已附加文件\s*[:：]\s*\S.*?\(\s*[\d.]+\s*[KMGT]?B\s*\)",
                                      "", instructions).strip()
                contents.append({"content_id": native_id[0], "title": title,
                                 "kind": kind, "instructions": instructions})
                children = set()
                for anchor in item.find_all("a", href=True):
                    target = urlsplit(anchor["href"])
                    if target.path.endswith("listContent.jsp"):
                        query = parse_qs(target.query)
                        child = query.get("content_id", [""])[0]
                        if not re.fullmatch(r"_\d+_\d+", child):
                            raise ResourceBlocked("independent_content_child_identity_unreadable")
                        children.add(child)
                for child in sorted(children):
                    folder(child, depth + 1)
        for area in self._pages("portal", api + "/contents", params={"limit": 100}):
            if not isinstance(area, dict) or not area.get("id"):
                raise OracleError("invalid_portal_content_area", service="portal")
            if (area.get("availability") or {}).get("available") != "No":
                folder(area["id"])
        assignments = [{**row, "source": "content-tree", "due_at": ""} for row in contents
                       if row["kind"] in {"作业", "Assignment"}]
        assessments = [row for row in contents if row["kind"] in {
            "考试", "测试", "测验", "小测", "Test", "Quiz", "Exam", "Survey"}]
        for raw in self._pages("portal", "/learn/api/public/v1/calendars/items", params={"limit": 100}):
            if not isinstance(raw, dict):
                raise OracleError("invalid_portal_calendar_item", service="portal")
            if raw.get("calendarId") == course_id:
                if not raw.get("id"):
                    raise OracleError("portal_calendar_identity_missing", service="portal")
                assignments.append({"content_id": "", "source_id": "calendar:" + str(raw["id"]),
                                    "title": raw.get("title") or "(无标题)", "source": "calendar",
                                    "due_at": raw.get("end") or raw.get("start") or "", "instructions": ""})
        assignments.sort(key=lambda row: (row["due_at"] or "9999", row["title"]))
        return {"origin": "independent-teaching-portal-read", "course_id": course_id,
                "lessons": lessons, "assignments": assignments, "announcements": announcements,
                "official_assessments": assessments}

    def notion_tree(self, root_id: str) -> dict:
        if not isinstance(root_id, str) or not _ID.fullmatch(root_id):
            raise OracleError("invalid_notion_root_id", service="notion")
        root = self.get("notion", "/v1/pages/" + root_id)
        if not isinstance(root, dict) or root.get("archived") or root.get("in_trash"):
            raise OracleError("notion_root_unavailable", service="notion")
        pages, blocks, seen = [root], [], set()

        def visit(parent: str, depth: int):
            identity = parent.replace("-", "").lower()
            if identity in seen or depth > 12 or len(seen) >= 2000:
                raise OracleError("notion_tree_bound_or_cycle", service="notion")
            seen.add(identity)
            for block in self._pages("notion", "/v1/blocks/" + parent + "/children",
                                     params={"page_size": 100}):
                if not isinstance(block, dict) or not _ID.fullmatch(str(block.get("id", ""))):
                    raise OracleError("invalid_notion_block", service="notion")
                blocks.append({"parent_id": parent, "block": block})
                if len(blocks) > 10000:
                    raise OracleError("notion_tree_bound_or_cycle", service="notion")
                if block.get("type") == "child_page":
                    page = self.get("notion", "/v1/pages/" + block["id"])
                    if not isinstance(page, dict):
                        raise OracleError("invalid_notion_page", service="notion")
                    pages.append(page)
                    visit(block["id"], depth + 1)
                elif block.get("has_children"):
                    visit(block["id"], depth + 1)
        visit(root_id, 0)
        return {"root_id": root_id, "pages": pages, "blocks": blocks}

    def _assignment_page(self, course_id: str, content_id: str) -> dict:
        if not all(isinstance(value, str) and re.fullmatch(r"_\d+_\d+", value)
                   for value in (course_id, content_id)):
            raise OracleError("invalid_assignment_target", service="portal")
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            raise ResourceBlocked("independent_portal_html_parser_unavailable") from None
        # No newAttempt/action query: even a GET must not open an attempt.
        raw = self.get("portal", "/webapps/assignment/uploadAssignment",
                       {"course_id": course_id, "content_id": content_id})
        if not isinstance(raw, str):
            raise ResourceBlocked("independent_assignment_html_required")
        soup = BeautifulSoup(raw, "html.parser")
        forms = []
        for form in soup.find_all("form"):
            identities = {key: [node.get("value") for node in form.find_all("input", attrs={"name": key})]
                          for key in ("course_id", "content_id")}
            if identities == {"course_id": [course_id], "content_id": [content_id]}:
                forms.append(form)
        from urllib.parse import parse_qs
        identity_confirmed = bool(forms)
        for anchor in soup.find_all("a", href=True):
            target = urlsplit(anchor["href"])
            query = parse_qs(target.query)
            if (target.scheme in {"", "https"} and target.hostname in {None, HOSTS["portal"]}
                    and target.path.endswith("/uploadAssignment")
                    and query.get("course_id") == [course_id]
                    and query.get("content_id") == [content_id]):
                identity_confirmed = True
        text = soup.get_text(" ", strip=True)
        review = "复查提交历史记录" in text or "Review Submission History" in text
        return {"course_id": course_id, "content_id": content_id,
                "authority": "course.pku.edu.cn", "form_present": len(forms) == 1,
                "identity_confirmed": identity_confirmed,
                "review_history_present": review,
                "page_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                # A review heading does not identify attempts or prove received bytes.
                "history": None, "history_available": False}

    def portal_assignment(self, course_id: str, content_id: str, *, require_history=True) -> dict:
        """Observe the real default assignment entry, never create an attempt.

        PKU's documented page proves only form/review state. It does not expose
        a verified per-attempt attachment hash contract. Until such a real
        readback is implemented, the E2E history requirement is blocked.
        """
        page = self._assignment_page(course_id, content_id)
        metadata = self.portal_course(course_id)
        matches = [row for row in metadata["assignments"] if row.get("content_id") == content_id
                   and row.get("source") == "content-tree"]
        if len(matches) != 1:
            raise ResourceBlocked("independent_official_assignment_target_not_confirmed")
        value = {**matches[0], **page,
                 "source_id": f"pku:{course_id}:assignments:{content_id}"}
        if require_history:
            raise ResourceBlocked("independent_assignment_attempt_and_file_history_unavailable")
        return value

    def historical_assignment(self, course_id: str) -> dict:
        """Verify an explicit historical target with official read-only evidence."""
        fixture = self.options.get("portal", {}).get("historical_assignment")
        if not isinstance(fixture, dict) or not all(isinstance(fixture.get(key), str)
                for key in ("course_id", "content_id")):
            raise ResourceBlocked("independent_historical_assignment_target_required")
        target_course, content_id = fixture["course_id"], fixture["content_id"]
        if not re.fullmatch(r"_\d+_\d+", target_course):
            raise OracleError("invalid_historical_course_id", service="portal")
        official = self.get("portal", "/learn/api/public/v1/courses/" + target_course)
        if not isinstance(official, dict) or official.get("id") != target_course:
            raise ResourceBlocked("independent_historical_course_identity_unavailable")
        availability = official.get("availability", {})
        duration = availability.get("duration", {}) if isinstance(availability, dict) else {}
        end = duration.get("end") if isinstance(duration, dict) else None
        try:
            ended = datetime.fromisoformat(end.replace("Z", "+00:00")) < datetime.now(timezone.utc)
        except (AttributeError, TypeError, ValueError):
            raise ResourceBlocked("independent_historical_course_end_unavailable") from None
        page = self._assignment_page(target_course, content_id)
        if (not ended or availability.get("available") != "No" or page["form_present"]
                or not page["identity_confirmed"]
                or not page["review_history_present"]):
            raise ResourceBlocked("independent_historical_assignment_readonly_unproven")
        return {**page, "read_only": True, "ended_at": end,
                "requested_course_id": course_id, "proof": "official_ended_unavailable_course_no_form"}

    def _billing(self) -> dict:
        spec = self.options.get("billing")
        if (not isinstance(spec, dict) or not isinstance(spec.get("path"), str)
                or not spec["path"].startswith("/v1/") or spec["path"] == "/v1/quota"
                or not all(isinstance(spec.get(key), str) and spec[key]
                           for key in ("points_field", "seconds_field"))):
            raise ResourceBlocked("independent_cumulative_billing_ledger_required")
        if any(re.search(r"remaining|balance|quota", spec[key], re.I)
               for key in ("points_field", "seconds_field")):
            raise ResourceBlocked("remaining_balance_is_not_cumulative_billing")
        try:
            value = self.get("account", spec["path"])
        except OracleError as exc:
            if exc.status in {404, 501}:
                raise ResourceBlocked("independent_billing_ledger_endpoint_unavailable") from None
            raise
        if not isinstance(value, dict):
            raise OracleError("invalid_billing_ledger", service="account")
        return {"charged_points": self._number(value.get(spec["points_field"]), "ledger_points_missing"),
                "charged_seconds": self._number(value.get(spec["seconds_field"]), "ledger_seconds_missing"),
                "source": "account_cumulative_ledger", "path": spec["path"]}

    @staticmethod
    def _traffic() -> dict:
        """Actual host counters are a conservative upper bound, never synthetic."""
        try:
            if sys.platform == "win32":
                completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                    "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); Get-NetAdapterStatistics | Select-Object Name,ReceivedBytes,SentBytes | ConvertTo-Json -Compress"],
                    capture_output=True, timeout=15, check=True, text=True, encoding="utf-8")
                values = json.loads(completed.stdout)
                values = values if isinstance(values, list) else [values]
                adapters = {row["Name"]: {"download_bytes": row["ReceivedBytes"],
                                         "upload_bytes": row["SentBytes"]} for row in values}
            elif sys.platform.startswith("linux"):
                adapters = {}
                for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
                    name, data = line.split(":", 1)
                    if name.strip() != "lo":
                        fields = data.split()
                        adapters[name.strip()] = {"download_bytes": int(fields[0]), "upload_bytes": int(fields[8])}
            elif sys.platform == "darwin":
                completed = subprocess.run(["/usr/sbin/netstat", "-ibn"], capture_output=True,
                                           timeout=15, check=True, text=True)
                lines = completed.stdout.splitlines()
                header = lines[0].split()
                adapters = {}
                for line in lines[1:]:
                    fields = line.split()
                    if len(fields) >= len(header) and fields[2].startswith("<Link#") and fields[0] != "lo0":
                        adapters[fields[0]] = {"download_bytes": int(fields[header.index("Ibytes")]),
                                              "upload_bytes": int(fields[header.index("Obytes")])}
            else:
                raise ResourceBlocked("host_network_counters_unsupported")
        except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
            raise ResourceBlocked("host_network_counters_unavailable") from None
        if not adapters:
            raise ResourceBlocked("host_network_counters_empty")
        for values in adapters.values():
            for value in values.values():
                Oracle._number(value, "invalid_host_network_counter")
        return {"source": "host_network_counters_upper_bound", "adapters": adapters}

    def snapshot(self, *, include_billing=False) -> dict:
        """Read account metadata; optional accounting is unknown, never zero.

        Zero-cost startup may execute without a deployed accounting endpoint.
        Paid journeys must explicitly request a measured billing baseline and
        operations ledger before their first charge-producing UI action.
        """
        value = {"billing": None, "operations": None, "quota": None,
                 "profile": self.profile(), "catalog": self.catalog()}
        if include_billing:
            value.update(billing=self._billing(), operations=self.operations(), quota=self.quota())
        value["traffic"] = self._traffic()
        return value

    def operations(self) -> dict:
        spec = self.options.get("operations")
        if not isinstance(spec, dict) or not isinstance(spec.get("path"), str):
            raise ResourceBlocked("independent_operation_ledger_required")
        try:
            value = self.get("account", spec["path"])
        except OracleError as exc:
            if exc.status in {404, 501}:
                raise ResourceBlocked("independent_operation_ledger_endpoint_unavailable") from None
            raise
        if not isinstance(value, dict) or any(not isinstance(value.get(key), list)
                for key in ("jobs", "settlements", "submission_posts", "oss_objects")):
            raise OracleError("invalid_independent_operation_ledger", service="account")
        return value

    @staticmethod
    def metrics(before: dict, after: dict, *, require_billing=True) -> dict:
        metrics = {"charged_points": None, "charged_seconds": None}
        measured = isinstance(before.get("billing"), dict) and isinstance(after.get("billing"), dict)
        if require_billing and not measured:
            raise ResourceBlocked("measured_billing_baseline_and_readback_required")
        if measured:
            for field in ("charged_points", "charged_seconds"):
                if (before["billing"]["source"] != "account_cumulative_ledger"
                        or after["billing"]["source"] != "account_cumulative_ledger"
                        or before["billing"]["path"] != after["billing"]["path"]):
                    raise OracleError("billing_measurement_origin_changed")
                metrics[field] = Oracle._number(after["billing"][field] - before["billing"][field],
                                               "billing_counter_regressed")
        previous, current = before["traffic"], after["traffic"]
        if (previous["source"] != "host_network_counters_upper_bound"
                or current["source"] != previous["source"]
                or set(previous["adapters"]) != set(current["adapters"])):
            raise OracleError("network_adapter_measurement_changed")
        for field in ("download_bytes", "upload_bytes"):
            deltas = [Oracle._number(current["adapters"][name][field] - values[field],
                                     "network_counter_regressed")
                      for name, values in previous["adapters"].items()]
            metrics[field] = sum(deltas)
        return metrics

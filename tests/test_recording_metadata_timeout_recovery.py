"""Renew one-use player credentials after a read timeout; never mask failure."""
import httpx
import pytest

from pku_sync.models import Recording
from pku_sync.recordings import resolve_media


def player_client(*, persistent_timeout=False, status=None):
    seen = []
    play_reads = 0

    def respond(request):
        nonlocal play_reads
        seen.append((request.method, request.url.path))
        assert request.method == "GET"
        if request.url.path.endswith("playVideo.action"):
            play_reads += 1
            return httpx.Response(200, text='<iframe src="https://yjapise.pku.edu.cn/cas/'
                                  + str(play_reads) + '"></iframe>')
        if request.url.path.startswith("/cas/"):
            if status is not None:
                return httpx.Response(status)
            if persistent_timeout or play_reads == 1:
                raise httpx.ReadTimeout("fixture timeout", request=request)
            return httpx.Response(302, headers={"location":
                "https://onlineroomse.pku.edu.cn/player?course_id=23&sub_id=42"})
        if request.url.path == "/player":
            return httpx.Response(200)
        assert request.url.path.endswith("get-course-subject-info")
        return httpx.Response(200, json={"code": 0, "data": {"duration": "7187",
            "content": {"playback": {"url": "https://media.pku.edu.cn/lesson.mp4"}}}})

    return httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True), seen


def recording():
    return Recording(course_id="_101540_1", title="Lecture", recorded_at="2026-09-29 08:00:00",
                     play_url="playVideo.action?token=fixture")


def test_player_read_timeout_renews_page_and_one_use_cas_then_returns_exact_duration():
    client, seen = player_client()
    with client:
        original = recording()
        result = resolve_media(client, original)
    assert result is original
    assert result.duration_seconds == 7187
    assert result.platform_course_id == "23" and result.subject_id == "42"
    assert seen.count(("GET", "/webapps/bb-streammedia-hqy-BBLEARN/playVideo.action")) == 2
    assert seen.count(("GET", "/cas/1")) == 1
    assert seen.count(("GET", "/cas/2")) == 1
    assert not any(path.endswith(".mp4") for _, path in seen)


def test_persistent_player_timeout_is_bounded_and_remains_failure():
    client, seen = player_client(persistent_timeout=True)
    with client, pytest.raises(httpx.ReadTimeout):
        resolve_media(client, recording())
    assert seen == [("GET", "/webapps/bb-streammedia-hqy-BBLEARN/playVideo.action"),
                    ("GET", "/cas/1"),
                    ("GET", "/webapps/bb-streammedia-hqy-BBLEARN/playVideo.action"),
                    ("GET", "/cas/2")]


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_player_http_rejection_is_not_retried_as_read_timeout(status):
    client, seen = player_client(status=status)
    with client, pytest.raises(httpx.HTTPStatusError) as captured:
        resolve_media(client, recording())
    assert captured.value.response.status_code == status
    assert seen == [("GET", "/webapps/bb-streammedia-hqy-BBLEARN/playVideo.action"), ("GET", "/cas/1")]

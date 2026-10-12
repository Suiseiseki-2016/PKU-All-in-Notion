"""Developer checks of an explicit host-scoped route, not native evidence."""
import httpx
import pytest
from pku_sync import auth, network


@pytest.mark.parametrize('value,expected', [
    ('', None), ('direct', None), ('not-a-proxy', None),
    ('file:///tmp/proxy', None), ('http://127.0.0.1:1', None),
])
def test_media_proxy_rejects_missing_invalid_or_dead_routes(monkeypatch, value, expected):
    monkeypatch.setenv('PKU_CAMPUS_MEDIA_PROXY', value)
    assert network.campus_media_proxy() == expected


def test_explicit_media_proxy_only_mounts_media_and_player_api(monkeypatch):
    monkeypatch.setattr(network, 'campus_media_proxy', lambda: 'http://127.0.0.1:12345')
    # The media route must not intercept passwords, ordinary course requests,
    # account service, or Notion; ordinary direct behavior remains available.
    monkeypatch.setattr(auth, 'campus_identity_proxy', lambda: None)
    with auth.new_client() as client:
        def transport(url): return client._transport_for_url(httpx.URL(url))
        default = transport('https://course.pku.edu.cn/')
        media = transport('https://resourcese.pku.edu.cn/movie.mp4')
        assert media is not default
        assert transport('https://yjapise.pku.edu.cn/vlabpassportapi/person/course-api/get-course-subject-info') is not default
        for host in ('iaaa.pku.edu.cn', 'course.pku.edu.cn', 'pku.aeoluswu.info', 'api.notion.com'):
            assert transport('https://' + host + '/') is default
    with auth.new_client(force_direct=True) as client:
        assert client._transport_for_url(httpx.URL('https://resourcese.pku.edu.cn/video')) is client._transport

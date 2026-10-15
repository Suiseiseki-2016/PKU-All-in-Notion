"""Read official player metadata without the product Recording/parser code."""
from datetime import datetime, timezone
import hashlib
import html
import json
import re
from contextlib import closing
from urllib.parse import urljoin, urlsplit, parse_qs

from bs4 import BeautifulSoup

from product_e2e.oracles import OracleError

_HOSTS = {'course.pku.edu.cn', 'yjapise.pku.edu.cn', 'onlineroomse.pku.edu.cn'}
_BASE = 'https://course.pku.edu.cn/webapps/bb-streammedia-hqy-BBLEARN/'


def _iframe_urls(body, base):
    # PKU emits signed query attributes with raw ampersands. HTML5's optional
    # semicolon entity parsing changes e.g. &real_name into a mathematical
    # symbol, invalidating the signature. Decode only terminated entities.
    result = []
    for tag in re.findall(r'<iframe\b[^>]*>', body, re.I):
        match = re.search(r'\ssrc\s*=\s*([\"\'])(.*?)\1', tag, re.I | re.S)
        if not match:
            continue
        value = re.sub(r'&(?:#[0-9]+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]+);',
                       lambda entity: html.unescape(entity[0]), match[2])
        value = urljoin(base, value)
        if urlsplit(value).hostname == 'yjapise.pku.edu.cn':
            result.append(_url(value))
    return result


def _url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.hostname not in _HOSTS
            or parsed.username or parsed.password or parsed.port not in {None, 443}
            or parsed.fragment):
        raise OracleError('unsafe_player_metadata_url', service='media')
    return value


def _read(client, oracle, url, *, referer=None):
    """Bounded GETs, manual allowlisted redirects, scoped cookie jar only."""
    for _ in range(6):
        _url(url)
        request = client.build_request('GET', url)
        request.headers.pop('authorization', None)
        request.headers.pop('cookie', None)
        client.cookies.set_cookie_header(request)
        if referer:
            request.headers['Referer'] = _url(referer)
        request.extensions['timeout'] = {'connect': 8, 'read': 20, 'write': 20, 'pool': 8}
        with closing(client.send(request, stream=True, follow_redirects=False)) as response:
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 4 * 1024 * 1024:
                    raise OracleError('player_metadata_too_large', service='media')
            host = urlsplit(url).hostname
            oracle.hosts.add(host)
            record = {'service': 'media', 'host': host, 'path': urlsplit(url).path,
                      'method': 'GET', 'status': response.status_code,
                      'body_bytes': len(body), 'body_sha256': hashlib.sha256(body).hexdigest(),
                      'observed_at': datetime.now(timezone.utc).isoformat()}
            if urlsplit(url).path.endswith('/get-course-subject-info') and response.status_code == 200:
                try:
                    metadata = json.loads(body)
                    raw_duration = metadata.get('data', {}).get('duration')
                    record['official_duration_type'] = type(raw_duration).__name__
                    if type(raw_duration) is int or (isinstance(raw_duration, str)
                            and re.fullmatch(r'[0-9]{1,12}', raw_duration)):
                        record['official_duration_raw'] = raw_duration
                except (ValueError, AttributeError, TypeError):
                    record['official_duration_type'] = 'invalid-envelope'
            oracle.requests.append(record)
            path = oracle.evidence_dir / ('oracle-media-' + str(len(oracle.requests)).zfill(5) + '.json')
            with path.open('x', encoding='utf-8') as stream:
                json.dump(record, stream)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get('location')
                if not location:
                    raise OracleError('player_redirect_missing', service='media')
                referer, url = url, _url(urljoin(url, location))
                continue
            if response.status_code != 200:
                raise OracleError('player_metadata_http_failure', service='media', status=response.status_code)
            return bytes(body), url
    raise OracleError('player_redirect_bound_exceeded', service='media')


def _metadata_duration(payload):
    if not isinstance(payload, dict) or type(payload.get('code')) is not int or payload['code'] != 0:
        raise OracleError('player_metadata_not_success', service='media')
    data = payload.get('data')
    duration = data.get('duration') if isinstance(data, dict) else None
    # The real official endpoint encodes e.g. 7167 as the decimal string
    # "7167" (recorded in oracle evidence). Preserve positive integer seconds;
    # never accept Count, a float, boolean, signed value or guessed unit.
    if isinstance(duration, str) and re.fullmatch(r'[1-9][0-9]*', duration):
        duration = int(duration)
    if type(duration) is not int or duration <= 0:
        raise OracleError('official_duration_unavailable', service='media')
    content = data.get('content')
    if not isinstance(content, dict):
        raise OracleError('official_playback_unavailable', service='media')
    candidates = []
    for key, field in [('playback', 'url'), ('save_playback', 'contents'), ('firm_source', 'contents')]:
        value = content.get(key)
        if isinstance(value, dict):
            candidates.append(value.get(field))
    for row in content.get('file_list', []):
        if isinstance(row, dict):
            candidates.append(row.get('file_name'))
    available = any(isinstance(value, str) and urlsplit(value).scheme == 'https'
                    and urlsplit(value).hostname and not urlsplit(value).username
                    and not urlsplit(value).password for value in candidates)
    if not available:
        raise OracleError('official_playback_unavailable', service='media')
    return {'duration_seconds': duration, 'official_media_available': True,
            'official_metadata_sha256': hashlib.sha256(json.dumps(payload, sort_keys=True,
                separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()}


def official_duration(client, oracle, course_id, source_id):
    """Find the stable timestamp in a fresh official table and read its player."""
    return _metadata_duration(_official_subject_info(client, oracle, course_id, source_id))


def official_original_source(client, oracle, course_id, source_id):
    """Return a private download address, never serialize it into evidence.

    Identity and duration still come from a newly read official table/player.
    This selects the primary original recording rather than a generated clip.
    """
    payload = _official_subject_info(client, oracle, course_id, source_id)
    checked = _metadata_duration(payload)
    content = payload['data']['content']
    primary = content.get('playback')
    url = primary.get('url') if isinstance(primary, dict) else None
    if not isinstance(url, str) or urlsplit(url).scheme != 'https' or not urlsplit(url).hostname:
        raise OracleError('official_primary_original_media_unavailable', service='media')
    if urlsplit(url).username or urlsplit(url).password or urlsplit(url).fragment:
        raise OracleError('unsafe_original_media_url', service='media')
    oracle._secrets.add(url)
    return {**checked, 'course_id': course_id, 'source_id': source_id, 'private_media_url': url}


def _official_subject_info(client, oracle, course_id, source_id):
    if not re.fullmatch(r'_\d+_\d+', course_id):
        raise OracleError('invalid_portal_course_id', service='media')
    raw = oracle.get('portal', '/webapps/bb-streammedia-hqy-BBLEARN/videoList.action',
                     {'course_id': course_id, 'mode': 'view', 'sortDir': 'ASCENDING', 'editPaging': 'true'})
    soup = BeautifulSoup(raw, 'html.parser')
    columns, matches = [], []
    for row in soup.find_all('tr'):
        cells = [cell.get_text(' ', strip=True) for cell in row.find_all(['th', 'td'], recursive=False)]
        link = row.find('a', href=re.compile(r'playVideo\.action'))
        if not link:
            if sum(cell in {'名称', '时间', '教师', '操作'} for cell in cells) >= 2:
                columns = cells
            continue
        fields = {}
        for index, cell in enumerate(cells):
            match = re.match(r'^(名称|时间|教师|操作)\s*[:：]\s*(.*)$', cell)
            if match:
                fields[match[1]] = match[2]
            elif index < len(columns):
                fields[columns[index]] = cell
        start = fields.get('时间', '')
        datetime.fromisoformat(start)
        identity = json.dumps({'course': course_id, 'start': start}, ensure_ascii=False,
                              sort_keys=True, separators=(',', ':')).encode()
        if 'pku:recording:' + hashlib.sha256(identity).hexdigest()[:32] == source_id:
            matches.append(_url(urljoin(_BASE, link['href'])))
    if len(matches) != 1:
        raise OracleError('ambiguous_official_player_identity', service='media')
    body, _ = _read(client, oracle, matches[0])
    frames = _iframe_urls(body.decode('utf-8'), matches[0])
    if len(frames) != 1:
        raise OracleError('official_player_frame_unavailable', service='media')
    _, landed = _read(client, oracle, frames[0], referer='https://course.pku.edu.cn/')
    params = parse_qs(urlsplit(landed).query)
    if any(len(params.get(key, [])) != 1 or not params[key][0] for key in ('course_id', 'sub_id')):
        raise OracleError('official_player_identity_unavailable', service='media')
    from urllib.parse import urlencode
    endpoint = ('https://yjapise.pku.edu.cn/vlabpassportapi/person/course-api/get-course-subject-info?'
                + urlencode({key: params[key][0] for key in ('course_id', 'sub_id')}))
    body, _ = _read(client, oracle, endpoint, referer=landed)
    return json.loads(body)

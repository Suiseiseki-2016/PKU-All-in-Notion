"""A portal title/folder change must not detach existing lecture artifacts."""
import json
import pytest

from pku_sync.models import Recording
from pku_sync.pipeline import RecordingJob
from pku_sync.panel import recordings_api
from pku_sync.panel import campus_catalog, notion_home
from pku_sync.catalog_snapshots import recording_source_id


def recording(title='第一讲', **kw):
    return Recording(course_id='_101545_1', title=title,
                     recorded_at='2026-09-29 08:00:00', **kw)


def test_ui_recording_identity_survives_title_and_course_folder_changes(tmp_path):
    old = RecordingJob('发展心理学', tmp_path / 'old-course', recording())
    new = RecordingJob('发展心理学改名', tmp_path / 'new-course', recording('新的讲次标题'))
    assert recordings_api.recording_id(old) == recordings_api.recording_id(new)


def test_ui_identity_distinguishes_native_courses_with_same_explicit_source_id(tmp_path):
    first = RecordingJob('一班', tmp_path / 'first', recording(source_id='native:one'))
    other = recording(source_id='native:one').model_copy(update={'course_id': '_202_1'})
    second = RecordingJob('二班', tmp_path / 'second', other)
    assert recordings_api.recording_id(first) != recordings_api.recording_id(second)


def test_renamed_recording_reuses_original_local_directory_by_source_identity(tmp_path):
    old = RecordingJob('发展心理学', tmp_path / 'course', recording())
    original = old.directory
    original.mkdir(parents=True)
    (original / 'recording.json').write_text(old.recording.model_dump_json(), 'utf-8')
    (original / 'video.mp4').write_bytes(b'fixture-only-no-network')
    (original / 'notes.md').write_text('已验收笔记', 'utf-8')
    new = RecordingJob('发展心理学', old.course_dir, recording('新的讲次标题'))
    assert new.directory == original
    assert new.video.read_bytes() == b'fixture-only-no-network'
    assert (new.directory / 'notes.md').read_text('utf-8') == '已验收笔记'


def test_same_date_different_course_does_not_adopt_foreign_artifacts(tmp_path):
    old = RecordingJob('发展心理学', tmp_path / 'course', recording())
    original = old.directory
    original.mkdir(parents=True)
    foreign = old.recording.model_copy(update={'course_id': '_202_1'})
    (original / 'recording.json').write_text(foreign.model_dump_json(), 'utf-8')
    new = RecordingJob('发展心理学', old.course_dir, recording('新的讲次标题'))
    assert new.directory != original


def test_previous_title_based_download_url_is_still_readable(tmp_path, monkeypatch):
    job = RecordingJob('发展心理学', tmp_path / 'course', recording())
    import hashlib
    old_key = hashlib.sha256('\n'.join((job.recording.course_id,
        job.recording.recorded_at, job.recording.title, job.course_dir.name)).encode()).hexdigest()[:24]
    monkeypatch.setattr(recordings_api, 'collect_jobs', lambda root: [job])
    assert recordings_api.find_job(tmp_path, old_key) is job


def test_same_title_on_same_day_different_start_does_not_overwrite_old_files(tmp_path):
    old = RecordingJob('发展心理学', tmp_path / 'course', recording())
    old.directory.mkdir(parents=True)
    (old.directory / 'recording.json').write_text(old.recording.model_dump_json(), 'utf-8')
    later = recording().model_copy(update={'recorded_at': '2026-09-29 14:00:00'})
    new = RecordingJob('发展心理学', old.course_dir, later)
    assert new.directory != old.directory


def test_duplicate_local_source_identity_is_not_silently_adopted(tmp_path):
    job = RecordingJob('发展心理学', tmp_path / 'course', recording())
    for name in ['first', 'second']:
        folder = job.course_dir / 'recordings' / name
        folder.mkdir(parents=True)
        (folder / 'recording.json').write_text(job.recording.model_dump_json(), 'utf-8')
    with pytest.raises(ValueError, match='多个本地目录'):
        _ = job.directory


def setup_course(tmp_path):
    job = RecordingJob('发展心理学', tmp_path / 'course', recording())
    (job.course_dir / 'recordings').mkdir(parents=True)
    (job.course_dir / 'course.json').write_text(json.dumps({
        'course_id': job.recording.course_id, 'name': job.course_name}), 'utf-8')
    (job.course_dir / 'recordings/index.json').write_text(json.dumps([job.recording.model_dump()]), 'utf-8')
    job.directory.mkdir()
    return job


def test_old_publication_marker_retains_links_after_ui_identity_migration(tmp_path, monkeypatch):
    job = setup_course(tmp_path)
    monkeypatch.setattr(notion_home, 'saved_home', lambda root: {'id': 'home'})
    (job.directory / 'notion-publication.json').write_text(json.dumps({
        'recording_id': recordings_api.legacy_recording_id(job), 'home_id': 'home',
        'lecture_url': 'https://www.notion.so/lecture', 'note_url': 'https://www.notion.so/note'}), 'utf-8')
    lesson = campus_catalog.campus_course(tmp_path, job.recording.course_id)['lessons'][0]
    assert lesson['notion_note_url'] == 'https://www.notion.so/note'


def test_manifest_with_previous_ui_key_recovers_links_only_by_unique_native_source(tmp_path, monkeypatch):
    job = setup_course(tmp_path)
    source_id = recording_source_id(job.recording)
    manifest = {'id': 'home', 'schema': 2, 'courses': {job.recording.course_id: {
        'lectures': {'previous-title-key': {'source_id': source_id,
            'url': 'https://www.notion.so/lecture', 'note_url': 'https://www.notion.so/note'}}}}}
    monkeypatch.setattr(notion_home, 'saved_home', lambda root: manifest)
    lesson = campus_catalog.campus_course(tmp_path, job.recording.course_id)['lessons'][0]
    assert lesson['notion_note_url'] == 'https://www.notion.so/note'
    manifest['courses'][job.recording.course_id]['lectures']['duplicate'] = {
        'source_id': source_id, 'url': 'https://www.notion.so/other'}
    ambiguous = campus_catalog.campus_course(tmp_path, job.recording.course_id)['lessons'][0]
    assert not ambiguous.get('notion_note_url')


@pytest.mark.parametrize('foreign', [False, True])
def test_renamed_legacy_publication_requires_same_native_published_version(tmp_path, monkeypatch, foreign):
    job = setup_course(tmp_path)
    source_id = recording_source_id(job.recording)
    (job.directory / 'recording.json').write_text(job.recording.model_dump_json(), 'utf-8')
    note_id, lecture_id = 'a' * 32, 'b' * 32
    (job.directory / 'notion-publication.json').write_text(json.dumps({
        'recording_id': recordings_api.legacy_recording_id(job), 'home_id': 'home',
        'lecture_id': lecture_id, 'lecture_url': f'https://www.notion.so/{lecture_id}',
        'note_url': f'https://www.notion.so/{note_id}'}), 'utf-8')
    (job.directory / 'published-note-version.json').write_text(json.dumps({
        'source_id': 'foreign' if foreign else source_id,
        'note_id': note_id, 'lecture_id': lecture_id}), 'utf-8')
    renamed = job.recording.model_copy(update={'title': '新标题'})
    (job.course_dir / 'recordings/index.json').write_text(json.dumps([renamed.model_dump()]), 'utf-8')
    monkeypatch.setattr(notion_home, 'saved_home', lambda root: {'id': 'home'})
    lesson = campus_catalog.campus_course(tmp_path, job.recording.course_id)['lessons'][0]
    assert bool(lesson.get('notion_note_url')) is not foreign


def test_download_existing_video_after_title_change_uses_stable_url(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tests.test_panel_recordings import settings, Directory
    job = setup_course(tmp_path)
    key = recordings_api.recording_id(job)
    (job.directory / 'recording.json').write_text(job.recording.model_dump_json(), 'utf-8')
    (job.directory / 'video.mp4').write_bytes(b'original-fixture-video')
    renamed = job.recording.model_copy(update={'title': '新标题'})
    (job.course_dir / 'recordings/index.json').write_text(json.dumps([renamed.model_dump()]), 'utf-8')
    app = FastAPI()
    recordings_api.add_recording_routes(app, recordings_api.RecordingWorkManager(settings(tmp_path)))
    response = TestClient(app).get(f'/api/recordings/{key}/video')
    assert response.status_code == 200
    assert response.content == b'original-fixture-video'
    assert (job.directory / 'video.mp4').read_bytes() == b'original-fixture-video'

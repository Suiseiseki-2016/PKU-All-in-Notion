from fastapi.testclient import TestClient
from pku_sync.config import Settings
from pku_sync.panel.webapi import create_app

def test_retired_exercise_routes_remain_unavailable(tmp_path):
    client = TestClient(create_app(Settings(_env_file=None, data_dir=tmp_path)))
    for path in ("/api/directory", "/api/exercises", "/api/exercises/grade/status"):
        assert client.get(path).status_code == 404
    assert client.post("/api/exercises/example/grade").status_code == 404

def test_retired_home_data_is_preserved_without_activating_old_mode(tmp_path):
    from pku_sync.panel.notion_home import saved_home
    path = tmp_path / 'notion-learning-home.json'
    path.write_text('{"id":"old","parent_id":"parent"}', 'utf-8')
    before = path.read_bytes()
    assert saved_home(tmp_path) == {}
    assert path.read_bytes() == before

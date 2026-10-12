"""Version-two home identity is a separate account field, invisible to old clients."""
from __future__ import annotations

from .. import platform
from ..catalog_snapshots import CatalogUnavailable


def remote(settings) -> dict:
    result = platform._fetch_account_profile(settings)
    if result.get("status") != "ready":
        raise CatalogUnavailable("账号主页配置尚未确认，新版目录保持只读。")
    return result


def publish(settings, home: dict, previous: dict, *, replace_existing=False) -> int:
    identity = {key: home[key] for key in ("id", "url", "parent_id")}
    profile = previous["profile"]
    existing = profile.get("notion_workspace_v2")
    if existing and existing.get("id") != identity["id"] and not replace_existing:
        raise CatalogUnavailable("账号已有另一个新版主页，请重新读取账号配置。")
    if existing != identity:
        options = {'replace_workspace_id': existing['id']} if replace_existing and existing else {}
        result = platform._put_account_profile(settings, previous["revision"],
                                               {**profile, "notion_workspace_v2": identity}, **options)
        if result.get("status") != "pushed":
            raise CatalogUnavailable("新版主页账号配置未确认；未切换学习主页。")
    verified = remote(settings)
    if verified["profile"].get("notion_workspace_v2") != identity:
        raise CatalogUnavailable("新版主页账号配置写入后验证失败；未切换学习主页。")
    return verified["revision"]


def pull(settings, profile: dict) -> dict:
    from pathlib import Path
    from ..catalog_schema import fingerprint
    from .notion_workspace import _save, manifest_path
    from ..catalog_snapshots import activate_account
    activate_account(settings)
    account_namespace = fingerprint({'session': settings.platform_token})
    identity = profile["profile"].get("notion_workspace_v2")
    if not identity:
        return {"status": "absent"}
    if not isinstance(identity, dict) or set(identity) != {"id", "url", "parent_id"}:
        return {"status": "invalid_response"}
    if not getattr(settings, 'notion_token', ''):
        return {"status": "notion_reconnect_required"}
    from ..notion import NotionError
    from .notion_workspace import NotionClient, _verify_home, _verify_selected_parent
    import httpx
    try:
        with NotionClient(settings.notion_token) as client:
            page = client.get_page(identity['id'])
            if page.get('archived') or page.get('in_trash'):
                return {'status': 'notion_home_archived'}
            _verify_home(page, identity['id'], identity['parent_id'])
            _verify_selected_parent(client, identity['parent_id'])
    except NotionError as exc:
        return {'status': 'notion_reconnect_required' if exc.status in {401, 403, 404}
                else 'notion_unavailable'}
    except httpx.HTTPError:
        return {'status': 'notion_unavailable'}
    except ValueError:
        return {'status': 'notion_home_changed'}
    if fingerprint({'session': settings.platform_token}) != account_namespace:
        return {'status': 'account_unavailable'}
    verified = platform._fetch_account_profile(settings)
    if (verified.get('status') != 'ready' or verified.get('revision') != profile['revision']
            or verified.get('profile', {}).get('notion_workspace_v2') != identity):
        return {'status': 'account_unavailable'}
    root = Path(settings.data_dir)
    # Keep this account's previously verified course projections, but never
    # import another session's local identity without remote confirmation.
    from .notion_workspace import saved_workspace
    local = saved_workspace(root)
    preserved = local if local.get("id") == identity["id"] else {}
    home = {**preserved, **identity, "schema": 2, "active": True,
            "account_namespace": account_namespace,
            "profile_revision": profile["revision"]}
    try:
        _save(manifest_path(root), home)
    except OSError:
        return {'status': 'local_write_failed'}
    return {"status": "pulled", "revision": profile["revision"]}

"""PKU IAAA single sign-on for course.pku.edu.cn (Blackboard Learn).

Flow, reverse-engineered from OAuthLogin.js on iaaa.pku.edu.cn:
  1. GET /iaaa/getPublicKey.do            -> RSA public key (PEM)
  2. RSA/PKCS1v15-encrypt the password with that key (matches JSEncrypt)
  3. POST /iaaa/oauthlogin.do             -> {"success": true, "token": "..."}
  4. GET campusLogin?token=...            -> Blackboard session cookies

PKU's CA is absent from the default macOS/Windows trust stores, so TLS
verification is disabled for these hosts.
"""

from __future__ import annotations

import base64
import logging
import time

import httpx
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from .config import settings
from . import network
from .network import campus_identity_proxy, campus_identity_fallback_proxy

IAAA_BASE = "https://iaaa.pku.edu.cn"
BB_BASE = "https://course.pku.edu.cn"
RESOURCE_BASE = "https://resourcese.pku.edu.cn"

# IAAA's appid allowlist is registered against the HTTP callback, but the HTTP
# gateway intermittently 502s, so the token is redeemed over HTTPS instead.
_CAMPUS_LOGIN_REGISTERED = (
    "http://course.pku.edu.cn/webapps/bb-sso-BBLEARN/execute/authValidate/campusLogin"
)
_CAMPUS_LOGIN_HTTPS = (
    "https://course.pku.edu.cn/webapps/bb-sso-BBLEARN/execute/authValidate/campusLogin"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0"
)


class CampusLoginRejected(RuntimeError):
    """IAAA refused the supplied credentials; never include its private response."""


def _encrypt_password(password: str, public_key_pem: str) -> str:
    key = load_pem_public_key(public_key_pem.encode())
    assert isinstance(key, RSAPublicKey)
    return base64.b64encode(key.encrypt(password.encode("utf-8"), padding.PKCS1v15())).decode()


def new_client(*, force_direct: bool = False, identity_proxy: str | None = None) -> httpx.Client:
    """An httpx client tuned for the campus network.

    course.pku.edu.cn aborts a TLS handshake every so often, roughly once in
    several attempts and more often from off-campus hosts. Retrying inside the
    transport means any request in a long sync recovers, not just the login.
    """
    proxy = None if force_direct else (identity_proxy or campus_identity_proxy())
    mounts = ({IAAA_BASE: httpx.HTTPTransport(proxy=proxy, retries=1, verify=False)}
              if proxy else {})
    media_proxy = None if force_direct else network.campus_media_proxy()
    if media_proxy:
        for host in (RESOURCE_BASE, "https://yjapise.pku.edu.cn"):
            mounts[host] = httpx.HTTPTransport(proxy=media_proxy, retries=1, verify=False)
    return httpx.Client(
        base_url=BB_BASE,
        follow_redirects=True,
        timeout=httpx.Timeout(20.0, connect=6.0),
        transport=httpx.HTTPTransport(retries=1, verify=False),
        mounts=mounts or None,
        headers={"User-Agent": USER_AGENT},
    )


def get_session(
    attempts: int = 2, username: str = "", password: str = ""
) -> httpx.Client:
    """Authenticate with IAAA and return a client holding a Blackboard session.

    Credentials default to the configured pair in ``.env``; explicit values
    override per call, so a service layer can inject one user's credentials
    without touching global config.
    Transport errors that survive the client's own retries are retried once more
    around the whole login; a rejected credential is raised immediately, since
    retrying cannot help.
    """
    last_error: httpx.TransportError | None = None
    fallback = campus_identity_fallback_proxy() if campus_identity_proxy() is None else None
    for attempt in range(attempts):
        try:
            return _login(username, password)
        except httpx.TransportError as exc:
            last_error = exc
            if fallback:
                logging.getLogger(__name__).warning(
                    "campus_identity_route=windows-local-proxy host=iaaa.pku.edu.cn reason=%s",
                    type(exc).__name__)
                try:
                    return _login(username, password, identity_proxy=fallback)
                except httpx.TransportError as fallback_error:
                    last_error = fallback_error
                fallback = None
            if attempt + 1 < attempts:
                time.sleep(2 * (attempt + 1))
    if campus_identity_proxy() is not None:
        try:
            return _login(username, password, force_direct=True)
        except httpx.TransportError as exc:
            last_error = exc
    raise RuntimeError(f"IAAA login failed after {attempts} attempts: {last_error}") from last_error


def iaaa_authenticate(
    client: httpx.Client,
    appid: str,
    redir_url: str,
    username: str = "",
    password: str = "",
) -> str:
    """Redeem the given (or configured) credentials into an IAAA OAuth token.

    ``appid`` selects the target system (blackboard, portalPublicQuery, …);
    ``redir_url`` is the callback IAAA has registered for that appid. The
    token is what the caller exchanges for a session at the target's SSO
    endpoint. Explicit ``username``/``password`` override the configured
    pair (service-layer injection); empty means
    "use .env".
    """
    user = username or settings.pku_username
    pwd = password or settings.pku_password
    key_resp = client.get(f"{IAAA_BASE}/iaaa/getPublicKey.do")
    key_resp.raise_for_status()
    key_data = key_resp.json()
    if not key_data.get("success"):
        raise RuntimeError(f"Failed to fetch IAAA public key: {key_data}")

    encrypted_pwd = _encrypt_password(pwd, key_data["key"])

    login_resp = client.post(
        f"{IAAA_BASE}/iaaa/oauthlogin.do",
        data={
            "appid": appid,
            "userName": user,
            "password": encrypted_pwd,
            "randCode": "",
            "smsCode": "",
            "otpCode": "",
            "redirUrl": redir_url,
        },
    )
    login_resp.raise_for_status()
    payload = login_resp.json()
    if not payload.get("success"):
        raise CampusLoginRejected("教学网登录被拒绝，请检查账号和密码；如需额外验证，请先到统一身份认证页面完成验证。")
    return payload["token"]


def _login(
    username: str = "", password: str = "", *, force_direct: bool = False,
    identity_proxy: str | None = None
) -> httpx.Client:
    user = username or settings.pku_username
    pwd = password or settings.pku_password
    if not user or not pwd:
        raise RuntimeError("PKU_USERNAME and PKU_PASSWORD must be set in .env")

    client = (new_client(force_direct=True) if force_direct else
              new_client(identity_proxy=identity_proxy) if identity_proxy else new_client())
    try:
        token = iaaa_authenticate(client, "blackboard", _CAMPUS_LOGIN_REGISTERED, user, pwd)
        bb_resp = client.get(_CAMPUS_LOGIN_HTTPS, params={"token": token})
        bb_resp.raise_for_status()
        return client
    except Exception:
        client.close()
        raise


def cookie_header(client: httpx.Client, domain_suffix: str = "pku.edu.cn") -> str:
    """Render the session cookies as a single `Cookie:` header value.

    ffmpeg cannot share httpx's cookie jar, so HLS downloads receive the
    session this way.
    """
    seen: dict[str, str] = {}
    for cookie in client.cookies.jar:
        if cookie.domain and cookie.domain.lstrip(".").endswith(domain_suffix):
            seen[cookie.name] = cookie.value or ""
    return "; ".join(f"{name}={value}" for name, value in seen.items())

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
import time

import httpx
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from .config import settings

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


def _encrypt_password(password: str, public_key_pem: str) -> str:
    key = load_pem_public_key(public_key_pem.encode())
    assert isinstance(key, RSAPublicKey)
    return base64.b64encode(key.encrypt(password.encode("utf-8"), padding.PKCS1v15())).decode()


def new_client() -> httpx.Client:
    """An httpx client tuned for the campus network.

    course.pku.edu.cn aborts a TLS handshake every so often, roughly once in
    several attempts and more often from off-campus hosts. Retrying inside the
    transport means any request in a long sync recovers, not just the login.
    """
    return httpx.Client(
        base_url=BB_BASE,
        follow_redirects=True,
        timeout=60,
        transport=httpx.HTTPTransport(retries=5, verify=False),
        headers={"User-Agent": USER_AGENT},
    )


def get_session(
    attempts: int = 4, username: str = "", password: str = ""
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
    for attempt in range(attempts):
        try:
            return _login(username, password)
        except httpx.TransportError as exc:
            last_error = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"IAAA login failed after {attempts} attempts: {last_error}")


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
        raise RuntimeError(f"IAAA login failed: {payload.get('errors') or payload}")
    return payload["token"]


def _login(username: str = "", password: str = "") -> httpx.Client:
    user = username or settings.pku_username
    pwd = password or settings.pku_password
    if not user or not pwd:
        raise RuntimeError("PKU_USERNAME and PKU_PASSWORD must be set in .env")

    client = new_client()
    token = iaaa_authenticate(client, "blackboard", _CAMPUS_LOGIN_REGISTERED, user, pwd)

    bb_resp = client.get(_CAMPUS_LOGIN_HTTPS, params={"token": token})
    bb_resp.raise_for_status()
    return client


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

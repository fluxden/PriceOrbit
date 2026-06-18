"""Generic OpenID Connect (Authorization Code + PKCE) for SSO sign-in.

Confidential client, server-side code flow. The ID Token is received directly
from the token endpoint over a TLS-protected back-channel, so per OIDC Core
3.1.3.7 the standard claims (iss/aud/exp/nonce) are validated and TLS provides
transport integrity. Only httpx is required (already a dependency).
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

import httpx

_DISCOVERY_CACHE: dict[str, tuple[float, dict]] = {}
_DISCOVERY_TTL = 3600.0
_TIMEOUT = 10.0
_LEEWAY = 120  # seconds of clock skew allowed on exp/iat


class OIDCError(Exception):
    pass


def _b64url_decode(seg: str) -> bytes:
    seg += "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg.encode())


def discovery(issuer: str) -> dict:
    """Fetch and cache the provider's well-known configuration."""
    issuer = (issuer or "").rstrip("/")
    if not issuer:
        raise OIDCError("No issuer configured.")
    now = time.time()
    hit = _DISCOVERY_CACHE.get(issuer)
    if hit and now - hit[0] < _DISCOVERY_TTL:
        return hit[1]
    url = issuer + "/.well-known/openid-configuration"
    try:
        r = httpx.get(url, timeout=_TIMEOUT)
        r.raise_for_status()
        meta = r.json()
    except Exception as exc:  # noqa: BLE001
        raise OIDCError(f"Could not load provider metadata: {exc}") from exc
    for key in ("authorization_endpoint", "token_endpoint"):
        if not meta.get(key):
            raise OIDCError(f"Provider metadata missing {key}.")
    _DISCOVERY_CACHE[issuer] = (now, meta)
    return meta


def make_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def authorization_url(meta: dict, client_id: str, redirect_uri: str, scopes: str,
                      state: str, nonce: str, challenge: str) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scopes or "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return meta["authorization_endpoint"] + "?" + urlencode(params)


def exchange_code(meta: dict, client_id: str, client_secret: str, redirect_uri: str,
                  code: str, verifier: str) -> dict:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": verifier,
    }
    try:
        r = httpx.post(meta["token_endpoint"], data=data, timeout=_TIMEOUT,
                       headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001
        raise OIDCError(f"Token exchange failed: {exc}") from exc


def decode_id_token(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        return json.loads(_b64url_decode(payload))
    except Exception as exc:  # noqa: BLE001
        raise OIDCError("Malformed ID token.") from exc


def validate_claims(claims: dict, issuer: str, client_id: str, nonce: str) -> None:
    iss = (claims.get("iss") or "").rstrip("/")
    if iss != (issuer or "").rstrip("/"):
        raise OIDCError("ID token issuer mismatch.")
    aud = claims.get("aud")
    aud_ok = client_id in aud if isinstance(aud, list) else aud == client_id
    if not aud_ok:
        raise OIDCError("ID token audience mismatch.")
    now = time.time()
    if claims.get("exp") and now > float(claims["exp"]) + _LEEWAY:
        raise OIDCError("ID token expired.")
    if claims.get("nonce") and nonce and claims["nonce"] != nonce:
        raise OIDCError("ID token nonce mismatch.")


def userinfo(meta: dict, access_token: str) -> dict:
    endpoint = meta.get("userinfo_endpoint")
    if not endpoint or not access_token:
        return {}
    try:
        r = httpx.get(endpoint, timeout=_TIMEOUT,
                      headers={"Authorization": f"Bearer {access_token}"})
        r.raise_for_status()
        return r.json()
    except Exception:  # noqa: BLE001
        return {}


def derive_identity(claims: dict) -> tuple[str, str, str | None]:
    """Return (subject, username, display_name) from merged claims."""
    subject = str(claims.get("sub") or "")
    username = (claims.get("preferred_username") or claims.get("email")
                or claims.get("name") or subject)
    if username and "@" in username:
        username = username.split("@")[0]
    display = claims.get("name") or claims.get("email")
    return subject, (username or subject).strip(), (display or None)

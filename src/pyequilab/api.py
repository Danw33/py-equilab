"""Small, read-only Firebase client; independent of Home Assistant.

No admin credentials, entitlement changes, storage downloads or cloud writes.
The sole POST operations authenticate and refresh authentication.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypedDict
from urllib.parse import quote

import aiohttp

PROJECT = "project-847196107745527358"
API_KEY = "AIzaSyCRPTesSPploBVb2SMXbloWDyEkmHB1Wx8"
AUTH_URL = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={API_KEY}"
TOKEN_URL = f"https://securetoken.googleapis.com/v1/token?key={API_KEY}"
ROOT = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents"


class EquilabError(Exception):
    """Safe public exception; never includes response bodies or URLs."""


class AuthError(EquilabError):
    """Authentication expired or rejected."""


class AccessError(EquilabError):
    """Document access denied."""


class MissingError(EquilabError):
    """Document no longer exists."""


class RateLimitError(EquilabError):
    """Upstream quota exceeded."""


class RateLimitObservations(TypedDict):
    """Non-sensitive rate-limit metadata observed from the service."""

    http_429_count: int
    headers: dict[str, str]


def decode(value: dict[str, Any]) -> Any:
    """Decode Firestore values, retaining timezone-aware timestamps."""
    if "nullValue" in value:
        return None
    for key, converter in (
        ("stringValue", str),
        ("booleanValue", bool),
        ("integerValue", int),
        ("doubleValue", float),
    ):
        if key in value:
            return converter(value[key])
    if "timestampValue" in value:
        return datetime.fromisoformat(value["timestampValue"].replace("Z", "+00:00"))
    if "mapValue" in value:
        return {k: decode(v) for k, v in value["mapValue"].get("fields", {}).items()}
    if "arrayValue" in value:
        return [decode(v) for v in value["arrayValue"].get("values", [])]
    # Unsupported types are intentionally not surfaced or dereferenced.
    return None


def active_ids(value: Any) -> set[str]:
    """Only explicitly active boolean index entries confer discovery."""
    if not isinstance(value, dict):
        return set()
    return {k for k, v in value.items() if isinstance(k, str) and v is True}


class EquilabClient:
    """Use a caller-owned session, with serialized refresh and bounded reads."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        uid: str = "",
        refresh_token: str = "",
        on_token: Callable[[str], None] | None = None,
    ):
        self.session = session
        self.uid = uid
        self.refresh_token = refresh_token
        self.on_token = on_token
        self._id_token = ""
        self._expires = 0.0
        self._refresh_lock = asyncio.Lock()
        self._read_limit = asyncio.Semaphore(4)
        self.rate_limit_observations: RateLimitObservations = {
            "http_429_count": 0,
            "headers": {},
        }

    async def _request(
        self, method: str, url: str, *, auth: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            async with self.session.request(
                method,
                url,
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=False,
                **kwargs,
            ) as response:
                observed = {
                    k.lower(): str(v)[:200]
                    for k, v in response.headers.items()
                    if k.lower()
                    in {
                        "retry-after",
                        "ratelimit-limit",
                        "ratelimit-remaining",
                        "ratelimit-reset",
                        "ratelimit",
                        "ratelimit-policy",
                        "x-ratelimit-limit",
                        "x-ratelimit-remaining",
                        "x-ratelimit-reset",
                    }
                }
                self.rate_limit_observations["headers"].update(observed)
                if response.status == 429:
                    self.rate_limit_observations["http_429_count"] += 1
                    raise RateLimitError("Cloud rate limit; try again later")
                if response.status == 401:
                    raise AuthError("Authentication rejected")
                if response.status == 403:
                    raise AccessError("Cloud access denied")
                if response.status == 404:
                    raise MissingError("Document not found")
                if response.status >= 500:
                    raise EquilabError("Cloud service unavailable")
                if response.status == 400 and auth:
                    body = await response.json()
                    code = body.get("error", {}).get("message", "").split(" : ")[0]
                    if code in {
                        "INVALID_LOGIN_CREDENTIALS",
                        "INVALID_PASSWORD",
                        "EMAIL_NOT_FOUND",
                        "USER_DISABLED",
                        "USER_NOT_FOUND",
                        "INVALID_REFRESH_TOKEN",
                        "TOKEN_EXPIRED",
                        "INVALID_EMAIL",
                    }:
                        raise AuthError("Authentication rejected")
                    if code == "TOO_MANY_ATTEMPTS_TRY_LATER":
                        raise RateLimitError("Too many authentication attempts")
                    raise EquilabError("Authentication service rejected request")
                if response.status != 200:
                    raise EquilabError("Unexpected cloud response")
                body = await response.json()
                if not isinstance(body, dict):
                    raise EquilabError("Invalid cloud response")
                return body
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, AttributeError):
            raise EquilabError("Could not communicate with Equilab") from None

    def _tokens(self, body: dict[str, Any], *, refresh: bool) -> None:
        uid_value = body.get("user_id" if refresh else "localId")
        token_value = body.get("id_token" if refresh else "idToken")
        renewal_value = body.get("refresh_token" if refresh else "refreshToken")
        try:
            expiry = float(body.get("expires_in" if refresh else "expiresIn", 3600))
        except (TypeError, ValueError):
            raise EquilabError("Invalid authentication response") from None
        if not (
            isinstance(uid_value, str)
            and uid_value
            and isinstance(token_value, str)
            and token_value
            and isinstance(renewal_value, str)
            and renewal_value
        ):
            raise EquilabError("Incomplete authentication response")
        if not math.isfinite(expiry) or expiry <= 0:
            raise EquilabError("Invalid token lifetime")
        if self.uid and self.uid != uid_value:
            raise AuthError("Account identity changed")
        self.uid, self._id_token = uid_value, token_value
        self._expires = time.monotonic() + max(0, expiry - 60)
        changed = self.refresh_token != renewal_value
        self.refresh_token = renewal_value
        if changed and self.on_token:
            self.on_token(renewal_value)

    async def async_login(self, email: str, password: str) -> None:
        body = await self._request(
            "POST",
            AUTH_URL,
            auth=True,
            json={
                "email": email.strip(),
                "password": password,
                "returnSecureToken": True,
            },
        )
        self._tokens(body, refresh=False)

    async def _async_ensure_token(self, rejected: str | None = None) -> None:
        async with self._refresh_lock:
            if self._id_token and time.monotonic() < self._expires:
                if rejected is None or rejected != self._id_token:
                    return
            if not self.refresh_token:
                raise AuthError("Sign in required")
            body = await self._request(
                "POST",
                TOKEN_URL,
                auth=True,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                },
            )
            self._tokens(body, refresh=True)

    async def _async_document(self, collection: str, identifier: str) -> dict[str, Any]:
        """Read one referenced document; refresh once on a rejected ID token."""
        if not isinstance(identifier, str) or not identifier:
            raise EquilabError("Invalid document identifier")
        async with self._read_limit:
            await self._async_ensure_token()
            url = f"{ROOT}/{quote(collection, safe='')}/{quote(identifier, safe='')}"
            for attempt in range(2):
                token = self._id_token
                try:
                    body = await self._request(
                        "GET",
                        url,
                        headers={
                            "Authorization": f"Bearer {token}",
                        },
                    )
                    return {k: decode(v) for k, v in body.get("fields", {}).items()}
                except AuthError:
                    if attempt:
                        raise
                    await self._async_ensure_token(rejected=token)
                except (ValueError, TypeError, AttributeError, OverflowError):
                    raise EquilabError("Invalid document data") from None
            raise AuthError("Sign in required")

    async def async_get_user(self, identifier: str | None = None) -> dict[str, Any]:
        """Return a decoded user document."""
        return await self._async_document("users", identifier or self.uid)

    async def async_get_horse(self, identifier: str) -> dict[str, Any]:
        """Return a decoded horse document."""
        return await self._async_document("horses", identifier)

    async def async_get_training(self, identifier: str) -> dict[str, Any]:
        """Return a decoded training document."""
        return await self._async_document("trainings", identifier)

    async def async_get_stable(self, identifier: str) -> dict[str, Any]:
        """Return a decoded stable or group document."""
        return await self._async_document("stables", identifier)

    async def async_get_latest_notification(self) -> list[dict[str, Any]]:
        """Read only the most recent inbox item; never acknowledge it."""
        async with self._read_limit:
            await self._async_ensure_token()
            url = f"{ROOT}/users/{quote(self.uid, safe='')}/notifications"
            for attempt in range(2):
                token = self._id_token
                try:
                    body = await self._request(
                        "GET",
                        url,
                        params={"pageSize": "1", "orderBy": "date desc"},
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    return [
                        {k: decode(v) for k, v in item.get("fields", {}).items()}
                        for item in body.get("documents", [])
                    ]
                except AuthError:
                    if attempt:
                        raise
                    await self._async_ensure_token(rejected=token)
                except (ValueError, TypeError, AttributeError, OverflowError):
                    raise EquilabError("Invalid notification data") from None
            raise AuthError("Sign in required")

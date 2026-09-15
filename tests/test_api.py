"""Tests for authentication, transport safety and decoded reads."""

import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp
import pytest

from pyequilab import (
    AccessError,
    AuthError,
    EquilabClient,
    EquilabError,
    MissingError,
    RateLimitError,
    active_ids,
)
from pyequilab.api import AUTH_URL, ROOT, TOKEN_URL, decode


@dataclass(slots=True)
class MockResult:
    status: int = 200
    payload: object = None
    body: str | None = None
    headers: dict[str, str] | None = None
    exception: Exception | None = None


class MockResponse:
    def __init__(self, result: MockResult):
        self.status = result.status
        self.headers = result.headers or {}
        self.payload = result.payload
        self.body = result.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return self.payload

    async def text(self):
        return self.body or ""


class MockHttp:
    def __init__(self):
        self.routes = defaultdict(deque)
        self.requests = defaultdict(list)
        self.responses = []

    def get(self, url, **kwargs):
        self._add("GET", url, **kwargs)

    def post(self, url, **kwargs):
        self._add("POST", url, **kwargs)

    def _add(
        self, method, url, *, status=200, payload=None, body=None, headers=None, exception=None
    ):
        self.routes[(method, self._normalise_url(url))].append(
            MockResult(
                status=status,
                payload=payload,
                body=body,
                headers=headers,
                exception=exception,
            )
        )

    def request(self, method, url, **kwargs):
        request_url = self._normalise_url(url, kwargs.get("params"))
        key = (method, request_url)
        self.requests[key].append(kwargs)
        if not self.routes[key]:
            raise AssertionError(f"Unexpected HTTP request: {method} {request_url}")
        result = self.routes[key].popleft()
        if result.exception is not None:
            raise result.exception
        response = MockResponse(result)
        self.responses.append(response)
        return response

    @staticmethod
    def _normalise_url(url, params=None):
        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        if params:
            query.extend(params.items())
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sorted(query)), ""))


@asynccontextmanager
async def mocked_http():
    mock = MockHttp()
    yield mock, mock


def auth_payload(refresh: bool = False, **changes):
    result = (
        {
            "user_id": "user",
            "id_token": "token-new",
            "refresh_token": "renew-new",
            "expires_in": "3600",
        }
        if refresh
        else {
            "localId": "user",
            "idToken": "token",
            "refreshToken": "renew",
            "expiresIn": "3600",
        }
    )
    return {**result, **changes}


def test_firestore_decode_and_active_membership():
    assert decode({"integerValue": "12"}) == 12
    assert decode({"doubleValue": 2.5}) == 2.5
    assert decode({"booleanValue": True}) is True
    assert decode({"stringValue": "value"}) == "value"
    assert decode({"nullValue": None}) is None
    assert decode({"timestampValue": "2026-09-09T12:00:00Z"}) == datetime(
        2026, 9, 9, 12, tzinfo=UTC
    )
    assert decode(
        {"mapValue": {"fields": {"array": {"arrayValue": {"values": [{"doubleValue": 2.0}]}}}}}
    ) == {"array": [2.0]}
    assert decode({"referenceValue": "not-exposed"}) is None
    assert active_ids({"a": True, "b": False, "c": None, "d": 1}) == {"a"}
    assert active_ids(None) == set()


async def test_login_document_read_and_token_rotation():
    rotations = []
    async with mocked_http() as (session, mock):
        mock.post(AUTH_URL, payload=auth_payload())
        mock.get(f"{ROOT}/users/user", status=401)
        mock.post(TOKEN_URL, payload=auth_payload(True))
        mock.get(f"{ROOT}/users/user", payload={"fields": {"name": {"stringValue": "Rider"}}})
        client = EquilabClient(session, on_token=rotations.append)
        await client.async_login(" rider@example.invalid ", "private")
        assert await client.async_get_user() == {"name": "Rider"}
        assert client.uid == "user"
        assert rotations == ["renew", "renew-new"]
        assert not hasattr(client, "password")


async def test_domain_read_methods_quote_identifiers():
    async with mocked_http() as (session, mock):
        client = EquilabClient(session)
        client._tokens(auth_payload(), refresh=False)
        for collection, identifier in (
            ("users", "other user"),
            ("horses", "horse/id"),
            ("trainings", "training/id"),
            ("stables", "stable/id"),
        ):
            mock.get(
                f"{ROOT}/{collection}/{identifier.replace('/', '%2F').replace(' ', '%20')}",
                payload={},
            )
        assert await client.async_get_user("other user") == {}
        assert await client.async_get_horse("horse/id") == {}
        assert await client.async_get_training("training/id") == {}
        assert await client.async_get_stable("stable/id") == {}


async def test_concurrent_reads_share_one_token_refresh():
    async with mocked_http() as (session, mock):
        mock.post(TOKEN_URL, payload=auth_payload(True))
        for identifier in ("a", "b", "c"):
            mock.get(f"{ROOT}/horses/{identifier}", payload={"fields": {}})
        client = EquilabClient(session, uid="user", refresh_token="renew")
        await asyncio.gather(*(client.async_get_horse(key) for key in ("a", "b", "c")))
        assert (
            sum(len(calls) for (method, _), calls in mock.requests.items() if method == "POST") == 1
        )


@pytest.mark.parametrize(
    "status, error",
    [
        (401, AuthError),
        (403, AccessError),
        (404, MissingError),
        (429, RateLimitError),
        (500, EquilabError),
        (302, EquilabError),
    ],
)
async def test_http_errors_are_safe(status, error):
    url = f"{ROOT}/horses/id"
    async with mocked_http() as (session, mock):
        mock.get(url, status=status, body="PRIVATE_DATA")
        client = EquilabClient(session)
        with pytest.raises(error) as caught:
            await client._request("GET", url)
        assert mock.responses[-1].body == "PRIVATE_DATA"
        assert "PRIVATE_DATA" not in str(caught.value)
        if status == 429:
            assert client.rate_limit_observations["http_429_count"] == 1


@pytest.mark.parametrize(
    "message, error",
    [
        ("INVALID_LOGIN_CREDENTIALS", AuthError),
        ("TOO_MANY_ATTEMPTS_TRY_LATER", RateLimitError),
        ("UNRECOGNIZED_AUTH_FAILURE", EquilabError),
    ],
)
async def test_authentication_service_errors(message, error):
    async with mocked_http() as (session, mock):
        mock.post(AUTH_URL, status=400, payload={"error": {"message": message}})
        with pytest.raises(error):
            await EquilabClient(session).async_login("x", "x")


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"expiresIn": "invalid"}, "Invalid authentication response"),
        ({"idToken": ""}, "Incomplete authentication response"),
        ({"expiresIn": "nan"}, "Invalid token lifetime"),
        ({"expiresIn": "0"}, "Invalid token lifetime"),
    ],
)
def test_invalid_authentication_payloads(changes, message):
    client = EquilabClient(None)
    with pytest.raises(EquilabError, match=message):
        client._tokens(auth_payload(**changes), refresh=False)


def test_token_identity_cannot_change():
    client = EquilabClient(None, uid="expected")
    with pytest.raises(AuthError, match="identity changed"):
        client._tokens(auth_payload(True), refresh=True)


async def test_invalid_response_and_network_failure_are_safe():
    url = f"{ROOT}/horses/id"
    async with mocked_http() as (session, mock):
        mock.get(url, payload=[])
        with pytest.raises(EquilabError, match="Invalid cloud response"):
            await EquilabClient(session)._request("GET", url)
        mock.get(url, exception=aiohttp.ClientConnectionError("PRIVATE_DATA"))
        with pytest.raises(EquilabError, match="Could not communicate") as caught:
            await EquilabClient(session)._request("GET", url)
        assert "PRIVATE_DATA" not in str(caught.value)


async def test_missing_refresh_token_requires_sign_in():
    async with aiohttp.ClientSession() as session:
        with pytest.raises(AuthError, match="Sign in required"):
            await EquilabClient(session)._async_ensure_token()


@pytest.mark.parametrize("identifier", ["", None])
async def test_empty_document_identifier_is_rejected(identifier):
    async with aiohttp.ClientSession() as session:
        client = EquilabClient(session)
        with pytest.raises(EquilabError, match="Invalid document identifier"):
            await client.async_get_horse(identifier)


@pytest.mark.parametrize("reader", ["document", "notification"])
async def test_second_rejected_token_is_not_retried(reader):
    async with mocked_http() as (session, mock):
        client = EquilabClient(session)
        client._tokens(auth_payload(), refresh=False)
        url = (
            f"{ROOT}/horses/id"
            if reader == "document"
            else f"{ROOT}/users/user/notifications?orderBy=date+desc&pageSize=1"
        )
        mock.get(url, status=401)
        mock.post(TOKEN_URL, payload=auth_payload(True))
        mock.get(url, status=401)
        with pytest.raises(AuthError):
            if reader == "document":
                await client.async_get_horse("id")
            else:
                await client.async_get_latest_notification()


@pytest.mark.parametrize("reader", ["document", "notification"])
async def test_malformed_firestore_collection_is_safe(reader):
    async with mocked_http() as (session, mock):
        client = EquilabClient(session)
        client._tokens(auth_payload(), refresh=False)
        if reader == "document":
            mock.get(f"{ROOT}/horses/id", payload={"fields": []})
            call = client.async_get_horse("id")
            message = "Invalid document data"
        else:
            mock.get(
                f"{ROOT}/users/user/notifications?orderBy=date+desc&pageSize=1",
                payload={"documents": [None]},
            )
            call = client.async_get_latest_notification()
            message = "Invalid notification data"
        with pytest.raises(EquilabError, match=message):
            await call


async def test_notification_read_preserves_only_safe_rate_limit_headers():
    async with mocked_http() as (session, mock):
        client = EquilabClient(session)
        client._tokens(auth_payload(), refresh=False)
        mock.get(
            f"{ROOT}/users/user/notifications?orderBy=date+desc&pageSize=1",
            payload={
                "documents": [
                    {
                        "fields": {
                            "date": {"timestampValue": "2026-09-10T12:00:00Z"},
                            "type": {"stringValue": "example"},
                        }
                    }
                ]
            },
            headers={"Retry-After": "60", "X-RateLimit-Remaining": "0", "Set-Cookie": "PRIVATE"},
        )
        result = await client.async_get_latest_notification()
        assert result[0]["date"] == datetime(2026, 9, 10, 12, tzinfo=UTC)
        assert client.rate_limit_observations["headers"] == {
            "retry-after": "60",
            "x-ratelimit-remaining": "0",
        }
        assert "PRIVATE" not in repr(client.rate_limit_observations)

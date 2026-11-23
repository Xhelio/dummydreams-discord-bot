from types import SimpleNamespace

import pytest

import verification


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class FakeAsyncClient:
    def __init__(self, response=None, error=None, **kwargs):
        self.response = response
        self.error = error
        self.kwargs = kwargs
        self.request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def get(self, url, headers):
        self.request = (url, headers)
        if self.error:
            raise self.error
        return self.response


def _mock_http(monkeypatch, response=None, error=None):
    client = FakeAsyncClient(response=response, error=error)
    monkeypatch.setattr(
        verification.httpx,
        "AsyncClient",
        lambda **kwargs: client,
    )
    return client


def _mock_tiktoklive_fallback(monkeypatch, data=None, error=None):
    client = SimpleNamespace(web=object())

    async def fake_to_thread(func, **kwargs):
        assert func is verification.TikTokLiveClient
        assert kwargs == {"unique_id": "creator"}
        return client

    async def fake_fetch_user_room_data(web, username):
        assert web is client.web
        assert username == "creator"
        if error:
            raise error
        return data if data is not None else {}

    monkeypatch.setattr(verification.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(
        verification.FetchRoomIdAPIRoute,
        "fetch_user_room_data",
        staticmethod(fake_fetch_user_room_data),
    )


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("", None),
        ("<html>no profile metadata</html>", None),
        ('{"signature":"hello world"}', "hello world"),
        ('{"bioDescription":"second field"}', "second field"),
        (r'{"signature":"bad\\q"}', r"bad\q"),
        ('{"signature":"first","bioDescription":"second"}', "first"),
    ],
)
def test_extract_bio_from_html(html, expected):
    assert verification._extract_bio_from_html(html) == expected


@pytest.mark.asyncio
async def test_async_fetch_tiktok_bio_returns_scraped_bio(monkeypatch):
    client = _mock_http(
        monkeypatch,
        response=FakeResponse(text='{"signature":"  hello  "}'),
    )

    result = await verification.async_fetch_tiktok_bio("@Creator")

    assert result == "  hello  "
    assert client.request[0].startswith("https://www.tiktok.com/@creator?")
    assert client.request[1] == verification.MOBILE_HEADERS


@pytest.mark.asyncio
async def test_async_fetch_tiktok_bio_uses_tiktoklive_after_non_200(monkeypatch):
    _mock_http(monkeypatch, response=FakeResponse(status_code=503))
    _mock_tiktoklive_fallback(
        monkeypatch,
        {"data": {"user": {"signature": "fallback bio"}}},
    )

    assert await verification.async_fetch_tiktok_bio("@Creator") == "fallback bio"


@pytest.mark.asyncio
async def test_async_fetch_tiktok_bio_uses_tiktoklive_after_http_exception(monkeypatch):
    _mock_http(monkeypatch, error=RuntimeError("network unavailable"))
    _mock_tiktoklive_fallback(
        monkeypatch,
        {"data": {"user": {"bioDescription": "fallback description"}}},
    )

    assert await verification.async_fetch_tiktok_bio("Creator") == "fallback description"


@pytest.mark.asyncio
async def test_async_fetch_tiktok_bio_returns_empty_when_all_sources_fail(monkeypatch):
    _mock_http(monkeypatch, response=FakeResponse(status_code=403))
    _mock_tiktoklive_fallback(monkeypatch, error=RuntimeError("fallback unavailable"))

    assert await verification.async_fetch_tiktok_bio("@Creator") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bio", "expected_code", "expected"),
    [
        ("Please verify with ABC-123", " abc-123 ", (True, "Please verify with ABC-123")),
        ("different code", "ABC-123", (False, "different code")),
        ("", "ABC-123", (False, "(Bio not found or profile is private)")),
    ],
)
async def test_async_verify_tiktok_bio(monkeypatch, bio, expected_code, expected):
    async def fake_fetch(username):
        assert username == "@Creator"
        return bio

    monkeypatch.setattr(verification, "async_fetch_tiktok_bio", fake_fetch)

    assert await verification.async_verify_tiktok_bio("@Creator", expected_code) == expected


@pytest.mark.asyncio
async def test_async_fetch_display_name_prefers_nickname(monkeypatch):
    client = _mock_http(
        monkeypatch,
        response=FakeResponse(text='{"nickname":"  Creator Name  ","uniqueId":"creator"}'),
    )

    assert await verification.async_fetch_tiktok_display_name("@Creator") == "Creator Name"
    assert client.request[0].startswith("https://www.tiktok.com/@creator?")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<title>Creator Name (@creator) | TikTok</title>", "Creator Name"),
        ("<TITLE>Creator Name | tiktok</TITLE>", "Creator Name"),
    ],
)
async def test_async_fetch_display_name_uses_title_fallback(monkeypatch, html, expected):
    _mock_http(monkeypatch, response=FakeResponse(text=html))

    assert await verification.async_fetch_tiktok_display_name("Creator") == expected


@pytest.mark.asyncio
async def test_async_fetch_display_name_uses_tiktoklive_fallback(monkeypatch):
    _mock_http(monkeypatch, response=FakeResponse(text="<html>profile blocked</html>"))
    _mock_tiktoklive_fallback(
        monkeypatch,
        {"data": {"user": {"nickname": "Fallback Name"}}},
    )

    assert await verification.async_fetch_tiktok_display_name("@Creator") == "Fallback Name"


@pytest.mark.asyncio
async def test_async_fetch_display_name_returns_normalized_username_when_unavailable(monkeypatch):
    _mock_http(monkeypatch, response=FakeResponse(status_code=404))
    _mock_tiktoklive_fallback(monkeypatch, data={"data": {"user": {}}})

    assert await verification.async_fetch_tiktok_display_name("@Creator") == "creator"


@pytest.mark.asyncio
async def test_async_fetch_display_name_uses_unique_id_from_tiktoklive(monkeypatch):
    _mock_http(monkeypatch, response=FakeResponse(text="<html>blocked</html>"))
    _mock_tiktoklive_fallback(
        monkeypatch,
        {"data": {"user": {"uniqueId": "fallback-id"}}},
    )

    assert await verification.async_fetch_tiktok_display_name("Creator") == "fallback-id"


def test_fetch_display_name_sync_wrapper(monkeypatch):
    async def fake_fetch(username):
        assert username == "@Creator"
        return "Creator Name"

    monkeypatch.setattr(verification, "async_fetch_tiktok_display_name", fake_fetch)

    assert verification.fetch_tiktok_display_name("@Creator") == "Creator Name"


def test_fetch_display_name_sync_wrapper_falls_back_on_unexpected_error(monkeypatch):
    def fail_run(coro):
        coro.close()
        raise ValueError("event loop unavailable")

    monkeypatch.setattr(verification.asyncio, "run", fail_run)

    assert verification.fetch_tiktok_display_name("@Creator") == "creator"

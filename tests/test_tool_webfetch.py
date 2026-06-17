"""Tests for the `webfetch` tool (v0.0.26): URL -> readable text.

`webfetch` is a read-only text-protocol tag (`<webfetch url="https://..."/>`) parsed
by `parse()` and executed via `Tools.webfetch`. It is the one network-touching tool:
bounded length, a timeout, friendly-on-error, http(s)-only. The network is ALWAYS
mocked here (monkeypatching `relay.tools._http_get`) -- the suite never hits the wire.
"""

from __future__ import annotations

import urllib.error

import relay.tools as tools_mod
from relay.protocol import parse
from relay.tools import WEBFETCH_CHAR_CAP, Tools


# --- parsing ----------------------------------------------------------------


def test_parse_webfetch_tag():
    result = parse('<webfetch url="https://example.com/docs"/>')
    assert [a.kind for a in result.actions] == ["webfetch"]
    assert result.actions[0].url == "https://example.com/docs"


# --- Tools.webfetch (network mocked) ----------------------------------------


def test_webfetch_returns_readable_text(monkeypatch):
    html_doc = (
        "<html><head><style>.x{color:red}</style><title>T</title></head>"
        "<body><script>evil()</script><h1>Hello</h1><p>World &amp; co</p></body></html>"
    )
    monkeypatch.setattr(tools_mod, "_http_get", lambda url: html_doc)
    out = Tools(".").webfetch("https://example.com")
    assert "Hello" in out and "World & co" in out  # entities unescaped
    assert "evil()" not in out and "color:red" not in out  # script/style stripped
    assert "<" not in out  # tags gone


def test_webfetch_truncates_long_content_with_a_note(monkeypatch):
    monkeypatch.setattr(tools_mod, "_http_get", lambda url: "A" * (WEBFETCH_CHAR_CAP + 500))
    out = Tools(".").webfetch("https://example.com/big")
    assert out.endswith(f"... (truncated at {WEBFETCH_CHAR_CAP} chars)")
    # The body before the note is exactly the cap.
    assert out[:WEBFETCH_CHAR_CAP] == "A" * WEBFETCH_CHAR_CAP


def test_webfetch_friendly_error_on_http_failure(monkeypatch):
    def boom(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)

    monkeypatch.setattr(tools_mod, "_http_get", boom)
    out = Tools(".").webfetch("https://example.com/missing")
    assert out.startswith("webfetch failed:")
    assert "HTTP 404" in out
    assert "Traceback" not in out and "traceback" not in out


def test_webfetch_friendly_error_on_url_error(monkeypatch):
    def boom(url):
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(tools_mod, "_http_get", boom)
    out = Tools(".").webfetch("https://nope.invalid")
    assert out.startswith("webfetch failed:")
    assert "name resolution failed" in out


def test_webfetch_rejects_non_http_url():
    out = Tools(".").webfetch("file:///etc/passwd")
    assert out.startswith("webfetch failed: only http(s) URLs are supported")


def test_webfetch_real_helper_sets_user_agent(monkeypatch):
    """The network seam sends a real (non-default) User-Agent -- guards the
    models.dev-style 403. We intercept urlopen so no real request is made."""
    captured = {}

    class _Resp:
        headers = type("H", (), {"get_content_charset": staticmethod(lambda: "utf-8")})()

        def read(self, n):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=None):
        captured["ua"] = request.headers.get("User-agent")
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(tools_mod.urllib.request, "urlopen", fake_urlopen)
    assert tools_mod._http_get("https://example.com") == "ok"
    assert captured["ua"] and "Relay" in captured["ua"]  # a real UA, not Python-urllib
    assert captured["timeout"] == tools_mod.WEBFETCH_TIMEOUT_S  # bounded, never hangs

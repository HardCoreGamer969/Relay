"""Unit tests for scripts/sync_version.py (network-free; part of the gating suite).

These pin the website-version rewriter's contract: it targets each marker as a
whole token (no ``data-relay-version`` vs ``data-relay-version-full`` cross-match),
is order-independent, replaces every occurrence (no stale duplicate), errors on a
missing marker, is idempotent — and the committed website is in sync with the code
version, so a forgotten badge bump fails CI here too (not only in quality.yml).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "sync_version", Path(__file__).resolve().parent.parent / "scripts" / "sync_version.py"
)
sync_version = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sync_version)


def test_render_sets_both_slots():
    html = (
        '<span data-relay-version>v0.0.1</span>\n'
        '<span data-relay-version-full>relay-cli v0.0.1</span>'
    )
    out = sync_version.render(html, "1.2.3")
    assert "<span data-relay-version>v1.2.3</span>" in out
    assert "<span data-relay-version-full>relay-cli v1.2.3</span>" in out
    assert "0.0.1" not in out


def test_render_is_order_independent_and_does_not_cross_match():
    # Full marker FIRST: the plain-marker pass must NOT clobber the full span
    # (the bug a naive `\bdata-relay-version\b` + count=1 would have).
    html = (
        '<span data-relay-version-full>relay-cli v0.0.1</span>\n'
        '<span data-relay-version>v0.0.1</span>'
    )
    out = sync_version.render(html, "2.0.0")
    assert "<span data-relay-version-full>relay-cli v2.0.0</span>" in out
    assert "<span data-relay-version>v2.0.0</span>" in out


def test_render_replaces_every_occurrence():
    # A duplicated plain marker must all update (no stale leftover that would wedge
    # --check). Both marker kinds must be present since render requires each.
    html = (
        '<span data-relay-version>v1</span> ... <a data-relay-version>v1</a>'
        '<i data-relay-version-full>relay-cli v1</i>'
    )
    out = sync_version.render(html, "9.9.9")
    assert out.count(">v9.9.9</") == 2, out
    assert ">relay-cli v9.9.9</" in out


def test_render_is_idempotent():
    html = '<span data-relay-version>v0.0.1</span><b data-relay-version-full>relay-cli v0.0.1</b>'
    once = sync_version.render(html, "3.3.3")
    twice = sync_version.render(once, "3.3.3")
    assert once == twice


def test_render_missing_marker_is_a_hard_error():
    with pytest.raises(SystemExit):
        sync_version.render("<span>no version markers here</span>", "1.0.0")


def test_committed_website_is_in_sync_with_code_version():
    version = sync_version.code_version()
    html = sync_version.WEBSITE.read_text(encoding="utf-8")
    assert sync_version.render(html, version) == html, (
        "website/index.html is out of sync with the code version "
        f"({version}). Run `python scripts/sync_version.py` and commit the result."
    )

#!/usr/bin/env python3
"""Sync the marketing website's displayed version to Relay's single source of truth.

The source of truth for Relay's version is ``relay/__init__.py`` (``__version__``),
which also matches ``pyproject.toml``. The static website (``website/index.html``)
shows the version in two places; rather than hand-editing those on every release,
this script rewrites them from ``__version__`` so they can never drift.

The two website spots are marked with stable data attributes so replacement is
exact (never a fragile match on the version string itself):

    <span data-relay-version>v0.0.28</span>            -> "v{version}"
    <span data-relay-version-full>relay-cli v0.0.28</span> -> "relay-cli v{version}"

Usage:
    python scripts/sync_version.py            # rewrite the website in place
    python scripts/sync_version.py --check    # verify only; exit 1 if out of sync

``--check`` is for CI (assert the committed website matches the code version
without modifying anything); the default rewrite mode is what the release/version
workflow runs and then commits.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT_PY = ROOT / "relay" / "__init__.py"
PYPROJECT = ROOT / "pyproject.toml"
WEBSITE = ROOT / "website" / "index.html"

# (data-attribute, prefix shown before the version) for each website spot.
WEBSITE_SLOTS = (
    ("data-relay-version", "v"),
    ("data-relay-version-full", "relay-cli v"),
)


def _read_version(path: Path, pattern: str) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(pattern, text)
    if not match:
        raise SystemExit(f"could not find a version in {path} (pattern: {pattern!r})")
    return match.group(1)


def code_version() -> str:
    """The authoritative version from ``relay/__init__.py`` (cross-checked vs pyproject)."""
    version = _read_version(INIT_PY, r'__version__\s*=\s*["\']([^"\']+)["\']')
    # A drifted pyproject is a real bug; surface it loudly rather than silently
    # publishing a website version that disagrees with the packaged one.
    pyproject_version = _read_version(PYPROJECT, r'(?m)^\s*version\s*=\s*["\']([^"\']+)["\']')
    if pyproject_version != version:
        raise SystemExit(
            f"version mismatch: relay/__init__.py is {version!r} but "
            f"pyproject.toml is {pyproject_version!r}. Reconcile these first."
        )
    return version


def render(html: str, version: str) -> str:
    """Return ``html`` with every website version slot set to ``version``.

    For each marker the attribute is matched as a WHOLE token: the ``(?![-\\w])``
    lookahead stops ``data-relay-version`` from also matching inside
    ``data-relay-version-full`` (so slot order is irrelevant). Only the element's
    *text* is rewritten (``[^<]*`` never swallows a child tag), and EVERY
    occurrence is replaced so a duplicated marker can't silently go stale and
    wedge ``--check``. A marker that matches zero times is a hard error.
    """
    out = html
    for attr, prefix in WEBSITE_SLOTS:
        pattern = re.compile(rf"(<[^>]*\b{re.escape(attr)}(?![-\w])[^>]*>)([^<]*)(</[^>]+>)")
        out, count = pattern.subn(rf"\g<1>{prefix}{version}\g<3>", out)
        if count == 0:
            raise SystemExit(
                f"could not find an element with `{attr}` in {WEBSITE}. "
                "Did the website markup change? Re-add the marker or update this script."
            )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the website matches the code version; exit 1 on drift (no writes).",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        dest="print_version",
        help="print the resolved code version and exit (no writes).",
    )
    args = parser.parse_args()

    version = code_version()

    if args.print_version:
        print(version)
        return 0
    original = WEBSITE.read_text(encoding="utf-8")
    updated = render(original, version)

    if args.check:
        if original != updated:
            print(
                f"website is OUT OF SYNC: expected version {version!r}. "
                "Run `python scripts/sync_version.py` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print(f"website is in sync at v{version}.")
        return 0

    if original == updated:
        print(f"website already at v{version}; nothing to do.")
        return 0

    WEBSITE.write_text(updated, encoding="utf-8")
    print(f"website updated to v{version} ({WEBSITE.relative_to(ROOT)}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

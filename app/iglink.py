"""An Instagram URL, turned into the thing it points at.

Links arrive in whatever shape the sender's app produced: `/p/<code>/`,
`/reel/<code>/`, `/reel/<code>/?utm_source=…`, `/tv/<code>/`, a story link, an
`l.instagram.com/?u=` wrapper, or Instagram's own `/share/reel/<token>` form
that only resolves by being followed. None of that is worth storing; one code
is, because `posts.code` is how the database already names a post.

Pure functions, no network: the one shape that needs a redirect (`/share/…`) is
resolved by the caller, which then parses the URL it landed on.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

HOSTS = ("instagram.com", "instagr.am", "ig.me")

_POST = re.compile(r"^/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")
_STORY = re.compile(r"^/stories/([A-Za-z0-9_.]+)/(\d+)")
_SHARE = re.compile(r"^/share/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)")


def unwrap(url: str) -> str:
    """Outbound links arrive wrapped in `l.instagram.com/?u=<encoded>`."""
    got = parse_qs(urlparse(url).query).get("u") if "l.instagram.com" in url else None
    return got[0] if got else url


def parse(url: str | None) -> dict | None:
    """`{kind, code|token|username}` for a link worth opening, else None.

    `kind` is `post` (a code), `share` (a token the caller must follow) or
    `story` (which the deck does not open; the caller says so in words).
    """
    if not url:
        return None
    url = unwrap(url.strip())
    parts = urlparse(url if "://" in url else f"https://{url}")
    host = (parts.hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in HOSTS):
        return None
    path = parts.path or "/"
    if match := _POST.match(path):
        return {"kind": "post", "code": match.group(1)}
    if match := _SHARE.match(path):
        # ponytail: resolved by following the redirect, not by guessing: the
        # token is opaque and short-lived. Cheap because it is one HEAD.
        return {"kind": "share", "token": match.group(1), "url": url}
    if match := _STORY.match(path):
        return {"kind": "story", "username": match.group(1),
                "media_id": match.group(2)}
    return None


def links_in(text: str | None) -> list[str]:
    """Every Instagram URL in a message, in the order it was written."""
    if not text:
        return []
    out: list[str] = []
    for word in re.split(r"\s+", text):
        cleaned = word.strip("<>()[]\"'.,·")
        if "instagram.com" in cleaned or "instagr.am" in cleaned or "ig.me" in cleaned:
            if parse(cleaned) and cleaned not in out:
                out.append(cleaned)
    return out

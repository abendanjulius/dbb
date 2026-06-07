#!/usr/bin/env python3
"""Sync Instagram post URLs in instagram.json to local thumbnails + captions.

No Instagram account login required. Uses Open Graph tags from public post pages.
Optionally uses Meta Graph API oEmbed when META_APP_TOKEN is set (APP_ID|APP_SECRET).
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTAGRAM_FILE = ROOT / "instagram.json"
IMAGES_DIR = ROOT / "images"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/17.0 Safari/537.36"
)
SHORTCODE_RE = re.compile(r"instagram\.com/(?:p|reel|tv)/([^/?#]+)")
OG_TAG_RE = re.compile(
    r'<meta\s+property="og:(?P<key>image|title|description)"\s+content="(?P<value>.*?)"\s*/?>',
    re.IGNORECASE | re.DOTALL,
)
POST_DATE_RE = re.compile(
    r"on\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})",
    re.IGNORECASE,
)


def load_config() -> dict:
    if not INSTAGRAM_FILE.exists():
        raise FileNotFoundError(f"Missing {INSTAGRAM_FILE}")
    return json.loads(INSTAGRAM_FILE.read_text(encoding="utf-8"))


def save_config(data: dict) -> None:
    INSTAGRAM_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.rstrip("/")
    if SHORTCODE_RE.search(url):
        match = SHORTCODE_RE.search(url)
        shortcode = match.group(1) if match else ""
        if "/reel/" in path:
            return f"https://www.instagram.com/reel/{shortcode}/"
        if "/tv/" in path:
            return f"https://www.instagram.com/tv/{shortcode}/"
        return f"https://www.instagram.com/p/{shortcode}/"
    return url


def extract_shortcode(url: str) -> str:
    match = SHORTCODE_RE.search(url)
    return match.group(1) if match else re.sub(r"[^a-zA-Z0-9_-]+", "-", url)[-40:]


def clean_caption(raw: str) -> str:
    text = html.unescape(raw or "").strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(
        r"^.*? on Instagram:\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = text.strip(" \"'")
    if len(text) > 180:
        text = text[:177].rstrip() + "..."
    return text


def fetch_url(url: str, timeout: int = 30) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def parse_open_graph(page_html: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for match in OG_TAG_RE.finditer(page_html):
        key = match.group("key").lower()
        value = html.unescape(match.group("value")).strip()
        values[key] = value
    return values


def parse_post_date(page_html: str) -> str | None:
    match = POST_DATE_RE.search(page_html)
    if not match:
        return None
    try:
        posted = datetime.strptime(match.group(1), "%B %d, %Y").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    return posted.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_open_graph_meta(page_html: str) -> dict[str, str]:
    og = parse_open_graph(page_html)
    caption = clean_caption(og.get("title") or og.get("description") or "")
    thumbnail = og.get("image", "")
    if not thumbnail:
        raise ValueError("Could not find og:image on Instagram post page")
    return {
        "caption": caption,
        "thumbnail_url": thumbnail,
        "publishedAt": parse_post_date(page_html) or "",
    }


def fetch_via_open_graph(url: str) -> dict[str, str]:
    page_html = fetch_url(url)
    return parse_open_graph_meta(page_html)


def fetch_via_graph_api(url: str, token: str) -> dict[str, str]:
    params = urllib.parse.urlencode(
        {
            "url": url,
            "access_token": token,
            "fields": "thumbnail_url,title,author_name",
        }
    )
    api_url = f"https://graph.facebook.com/v21.0/instagram_oembed?{params}"
    payload = json.loads(fetch_url(api_url))
    caption = clean_caption(payload.get("title") or payload.get("author_name") or "")
    thumbnail = payload.get("thumbnail_url", "")
    if not thumbnail:
        raise ValueError("Graph API oEmbed response missing thumbnail_url")
    return {"caption": caption, "thumbnail_url": thumbnail, "publishedAt": ""}


def download_image(thumbnail_url: str, dest: Path) -> None:
    request = urllib.request.Request(thumbnail_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read()
    dest.write_bytes(data)


def merge_post_entries(data: dict) -> list[dict]:
    posts = []
    seen = set()

    for entry in data.get("posts", []):
        if isinstance(entry, str):
            entry = {"url": entry}
        if not isinstance(entry, dict):
            continue
        url = normalize_url(str(entry.get("url", "")))
        if not url or url in seen:
            continue
        seen.add(url)
        posts.append({**entry, "url": url})

    for url in data.get("urls", []):
        normalized = normalize_url(str(url))
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        posts.append({"url": normalized})

    return posts


def sort_posts_by_date(posts: list[dict]) -> list[dict]:
    def sort_key(post: dict) -> tuple[str, str, str]:
        return (
            post.get("publishedAt") or "",
            post.get("syncedAt") or "",
            post.get("url") or "",
        )

    return sorted(posts, key=sort_key, reverse=True)


def sync_post(entry: dict, token: str | None) -> dict:
    url = entry["url"]
    shortcode = extract_shortcode(url)
    meta: dict[str, str]

    errors = []
    page_html = ""
    if token:
        try:
            meta = fetch_via_graph_api(url, token)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"graph:{exc}")
            page_html = fetch_url(url)
            meta = parse_open_graph_meta(page_html)
    else:
        try:
            page_html = fetch_url(url)
            meta = parse_open_graph_meta(page_html)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Failed to sync {url}: {exc}") from exc

    if not meta.get("publishedAt"):
        if not page_html:
            try:
                page_html = fetch_url(url)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"date:{exc}")
                page_html = ""
        if page_html:
            meta["publishedAt"] = parse_post_date(page_html) or ""

    IMAGES_DIR.mkdir(exist_ok=True)
    image_name = f"ig-{shortcode}.jpg"
    image_path = IMAGES_DIR / image_name
    download_image(meta["thumbnail_url"], image_path)

    synced_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated = {
        **entry,
        "id": entry.get("id") or shortcode,
        "shortcode": shortcode,
        "url": url,
        "image": f"images/{image_name}",
        "caption": meta.get("caption") or entry.get("caption") or "",
        "publishedAt": meta.get("publishedAt") or entry.get("publishedAt") or "",
        "syncedAt": synced_at,
    }
    if errors:
        updated["syncWarnings"] = errors
    return updated


def main() -> int:
    token = os.environ.get("META_APP_TOKEN", "").strip() or None
    data = load_config()
    posts = merge_post_entries(data)
    max_posts = int(data.get("maxPosts", 12) or 12)

    if not posts:
        print("No Instagram post URLs found in instagram.json")
        return 0

    synced_posts = []
    for index, entry in enumerate(posts, start=1):
        print(f"[{index}/{len(posts)}] Syncing {entry['url']}")
        try:
            synced_posts.append(sync_post(entry, token))
        except Exception as exc:  # noqa: BLE001
            print(f"  ! Keeping previous data where possible: {exc}", file=sys.stderr)
            if entry.get("image"):
                synced_posts.append(entry)
            else:
                raise

    data["posts"] = sort_posts_by_date(synced_posts)[:max_posts]
    data.pop("urls", None)
    data["mode"] = "oembed"
    data["lastSyncedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    save_config(data)
    print(f"Updated {INSTAGRAM_FILE} with {len(synced_posts)} posts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
#!/usr/bin/env python3
"""Why does a stream pass the search but fail playback? (maintainer tool)

Runs a real search for one channel on chosen providers, then requests each
stream it finds in several ways and prints what the server answered:

  session        how the playback session asks (cookies from the scrape, no Range)
  no cookies     the same without the scrape's browser cookies
  with Range     the same plus the Range header the verifier sends
  verifier       Jellyball's own liveness check (verify_stream_live)
  session again  the session's request once more, after the others

A playlist the verifier accepts but the session gets an image or a web page
for points at the request difference that flips it; one that only works the
first time is a single-use link. Only hosts are printed: stream URLs carry
access tokens.

    python tools/probe_stream.py "Formula 1" --category f1 --provider StreamEast --provider 1Stream

Run it from the Jellyball/ directory with Jellyball stopped or running (it
uses its own browser and HTTP clients and does not touch the running app).
"""

import argparse
import asyncio
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

import scrapers  # noqa: E402
import state  # noqa: E402
from config import _upstream_media_headers  # noqa: E402
from stream_extractor import verify_stream_live  # noqa: E402
from ts_normalize import find_ts_start  # noqa: E402
from upstream import _fetch_upstream_body  # noqa: E402


def describe(body: bytes) -> str:
    head = body.lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"#EXTM3U"):
        return "HLS playlist"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "WebP image"
    if head.startswith(b"\x89PNG"):
        return "PNG image"
    if head.startswith(b"\xff\xd8\xff"):
        return "JPEG image"
    if head.startswith(b"GIF8"):
        return "GIF image"
    if head[:1] == b"<":
        return "HTML/XML page"
    if head[:1] == b"\x47":
        return "MPEG-TS media"
    return f"other ({head[:8]!r})"


async def fetch(client: httpx.AsyncClient, url: str, headers: dict) -> str:
    timeout = httpx.Timeout(connect=5.0, read=8.0, write=10.0, pool=3.0)
    try:
        result = await _fetch_upstream_body(client, url, headers, 2 * 1024 * 1024, timeout)
    except Exception as exc:  # noqa: BLE001 - report, never crash the probe
        return f"error {type(exc).__name__}"
    if result is None:
        return "no response"
    status, effective_url, content_type, body, _ = result
    moved = urllib.parse.urlsplit(str(effective_url)).hostname
    where = f" (served by {moved})" if moved and moved != urllib.parse.urlsplit(url).hostname else ""
    sync = find_ts_start(body[:64 * 1024])
    ts_note = f", TS sync found at byte {sync}" if sync >= 0 and not body.lstrip().startswith(b"#EXTM3U") else ""
    return f"HTTP {status}, {content_type or 'no content-type'}, {len(body)} bytes: {describe(body)}{where}{ts_note}"


async def probe(stream: dict, shared: httpx.AsyncClient, media: httpx.AsyncClient) -> None:
    url = str(stream.get("url") or "")
    referer, origin, cookies = (str(stream.get(key) or "") for key in ("referer", "origin", "cookies"))
    host = urllib.parse.urlsplit(url).hostname
    print(f"\n  stream on {host} — {stream.get('match_title') or ''}")
    print(f"    referer host: {urllib.parse.urlsplit(referer).hostname or '(none)'}, "
          f"origin: {origin or '(none)'}, scrape cookies: {cookies.count('=') if cookies else 0}")
    session_headers = _upstream_media_headers(referer, origin, cookies)
    rows = [("session", media, session_headers)]
    if cookies:
        rows.append(("no cookies", media, _upstream_media_headers(referer, origin, "")))
    rows.append(("with Range", media, {**session_headers, "Range": "bytes=0-65535"}))
    for label, client, headers in rows:
        print(f"    {label:<14} {await fetch(client, url, headers)}")
    try:
        head = await shared.head(url, headers=session_headers, timeout=8.0, follow_redirects=True)
        print(f"    {'HEAD':<14} HTTP {head.status_code}, {head.headers.get('content-type') or 'no content-type'}")
    except Exception as exc:  # noqa: BLE001
        print(f"    {'HEAD':<14} error {type(exc).__name__}")
    reject: dict = {}
    ok = await verify_stream_live(shared, url, referer, origin=origin, reject=reject)
    print(f"    {'verifier':<14} {'passed' if ok else 'rejected: ' + reject.get('reason', '?')}")
    print(f"    {'session again':<14} {await fetch(media, url, session_headers)}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("query", help="channel name / search query, e.g. \"Formula 1\"")
    parser.add_argument("--category", default="", help="catalog category, e.g. f1 (reads that sport's pages)")
    parser.add_argument("--term", action="append", default=[], help="search term (repeatable; default: the query)")
    parser.add_argument("--provider", action="append", default=[], help="provider name (repeatable; default: all)")
    parser.add_argument("--max-streams", type=int, default=3, help="streams to probe per provider")
    args = parser.parse_args()

    wanted = {name.casefold() for name in args.provider}
    providers = [p for p in scrapers.ACTIVE_PROVIDERS if not wanted or p.name.casefold() in wanted]
    if not providers:
        print("No such provider. Known: " + ", ".join(p.name for p in scrapers.ACTIVE_PROVIDERS))
        return 2
    terms = args.term or [args.query.casefold()]

    shared = httpx.AsyncClient(timeout=12.0, follow_redirects=True, http2=True)
    media = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=3.0), follow_redirects=False, http2=True,
    )
    state.SHARED_HTTP_CLIENT = shared
    state.MEDIA_HTTP_CLIENT = media
    token = scrapers.SEARCH_CATEGORY.set(args.category.casefold())
    try:
        browser = await scrapers.get_healthy_browser()
        print(f"Searching {', '.join(p.name for p in providers)} for {terms} "
              f"(category={args.category or '-'}, browser={'yes' if browser else 'no'})")
        for provider in providers:
            print(f"\n{provider.name}:")
            try:
                streams = await asyncio.wait_for(
                    provider.search(terms, browser=browser, http_client=shared), timeout=120,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  search failed: {type(exc).__name__}: {exc}")
                continue
            if not streams:
                print("  no streams found")
                continue
            print(f"  {len(streams)} stream(s); probing up to {args.max_streams}")
            for stream in streams[: args.max_streams]:
                await probe(stream, shared, media)
    finally:
        scrapers.SEARCH_CATEGORY.reset(token)
        await shared.aclose()
        await media.aclose()
        if scrapers.SHARED_BROWSER is not None:
            await scrapers.SHARED_BROWSER.close()
        if scrapers.PLAYWRIGHT_CLIENT is not None:
            await scrapers.PLAYWRIGHT_CLIENT.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

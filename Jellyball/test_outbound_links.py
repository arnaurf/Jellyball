"""Directories whose event pages only list other sites' streams (Footybite),
and the TotalSportek provider."""

import asyncio
import unittest
from unittest import mock

import scrapers
from plugins import sdk
from plugins.builtin.footybite.provider import FootybiteScraper
from plugins.builtin.totalsportek.provider import TotalSportekScraper


EVENT_PAGE = """
<html><body>
<a href="/motogp-streams">MotoGP</a>
<a href="https://ads.example/promo">Ad</a>
<div id="streams"><h2>Available Streams (4)</h2>
  <a href="https://live2.mirror-a.example/MotoGP-Live/1">Mirror A · english · HD Watch</a>
  <a href="https://player.mirror-b.example/watch/motogp">Mirror B Watch</a>
  <a href="https://footybite.im/game/other">own site</a>
  <a href="https://player.mirror-b.example/watch/motogp">duplicate</a>
  <a href="https://dead.mirror-c.example/motogp">Mirror C Watch</a>
</div>
</body></html>
"""
EVENT_URL = "https://footybite.im/game/motogp-portuguese-grand-prix-at-portimao-11200900"


class FakeBrowser:
    def is_connected(self):
        return True


def stream(url, referer):
    return {"url": url, "referer": referer, "provider": "Footybite", "match_score": 100, "match_title": "MotoGP"}


class OutboundLinkTests(unittest.TestCase):
    def setUp(self):
        sdk._OUTBOUND_HOST_HITS.clear()
        self.addCleanup(sdk._OUTBOUND_HOST_HITS.clear)
        self.provider = FootybiteScraper()

    def test_selects_other_sites_from_the_stream_list_only(self):
        links = self.provider._outbound_links(EVENT_PAGE, EVENT_URL)
        self.assertEqual(links, [
            "https://live2.mirror-a.example/MotoGP-Live/1",
            "https://player.mirror-b.example/watch/motogp",
            "https://dead.mirror-c.example/motogp",
        ])

    def test_sites_that_worked_before_come_first(self):
        sdk._OUTBOUND_HOST_HITS["dead.mirror-c.example"] = 123.0
        links = self.provider._outbound_links(EVENT_PAGE, EVENT_URL)
        self.assertEqual(links[0], "https://dead.mirror-c.example/motogp")

    def test_link_cap(self):
        with mock.patch.object(sdk, "OUTBOUND_MAX_LINKS", 2):
            self.assertEqual(len(self.provider._outbound_links(EVENT_PAGE, EVENT_URL)), 2)

    def test_http_first_then_browser_for_the_rest(self):
        mirror_a, mirror_b, mirror_c = self.provider._outbound_links(EVENT_PAGE, EVENT_URL)

        async def http(client, url, *args, **kwargs):
            return [stream("https://cdn-a.example/a.m3u8", url)] if url == mirror_a else []

        browser_calls = []

        async def browser(browser, url, *args, **kwargs):
            browser_calls.append(url)
            return [stream("https://cdn-b.example/b.m3u8", url)] if url == mirror_b else []

        with mock.patch.object(sdk, "fetch_bounded_text", mock.AsyncMock(return_value=EVENT_PAGE)), \
                mock.patch.object(sdk, "fetch_streams_from_page", side_effect=http), \
                mock.patch.object(sdk, "playwright_intercept_streams", side_effect=browser):
            found = asyncio.run(self.provider._follow_outbound_links(None, FakeBrowser(), EVENT_URL, 100, "MotoGP"))

        self.assertEqual([s["url"] for s in found], ["https://cdn-a.example/a.m3u8", "https://cdn-b.example/b.m3u8"])
        self.assertEqual(sorted(browser_calls), sorted([mirror_b, mirror_c]))  # mirror A already worked
        self.assertEqual(set(sdk._OUTBOUND_HOST_HITS), {"live2.mirror-a.example", "player.mirror-b.example"})

    def test_search_follows_listed_sites_when_the_event_page_has_no_player(self):
        async def find_matches(*args, **kwargs):
            return [(EVENT_URL, 100, "MotoGP Portuguese Grand Prix")]

        follow = mock.AsyncMock(return_value=[stream("https://cdn-a.example/a.m3u8", "https://live2.mirror-a.example/")])
        with mock.patch.object(self.provider, "_find_matches", side_effect=find_matches), \
                mock.patch.object(sdk, "fetch_streams_from_page", mock.AsyncMock(return_value=[])), \
                mock.patch.object(self.provider, "_follow_outbound_links", follow), \
                mock.patch.object(sdk, "playwright_intercept_streams", mock.AsyncMock(return_value=[])) as page_browser:
            found = asyncio.run(self.provider.search(["motogp"], browser=FakeBrowser(), http_client=mock.Mock()))
        self.assertEqual([s["url"] for s in found], ["https://cdn-a.example/a.m3u8"])
        self.assertEqual(found[0]["match_url"], EVENT_URL)
        page_browser.assert_not_called()  # streams found: the event page itself needs no browser

    def test_providers_without_a_selector_never_follow(self):
        self.assertEqual(scrapers.StreamEastScraper().outbound_link_selector, "")


class FootybiteMotorsportTests(unittest.TestCase):
    def test_motorsport_pages(self):
        provider = FootybiteScraper()
        token = scrapers.SEARCH_CATEGORY.set("motogp")
        try:
            self.assertEqual(provider.get_scan_urls(), ["https://footybite.im", "https://footybite.im/motogp-stream"])
        finally:
            scrapers.SEARCH_CATEGORY.reset(token)


class TotalSportekTests(unittest.TestCase):
    def test_event_links_match_the_motogp_race(self):
        provider = TotalSportekScraper()
        page = """
        <a href="https://live2.totalsportek.gives/MotoGP-Portuguese-Grand-Prix-vs-Live/59101">
          Match Started MotoGP Portuguese Grand Prix Live</a>
        <a href="https://live2.totalsportek.gives/Arsenal-vs-Chelsea-vs-Live/59102">Arsenal vs Chelsea</a>
        <a href="https://live.totalsporteki.st/motogp-streams/">MotoGP</a>
        """
        matches: list = []
        provider._parse_matches_from_html(page, "https://live2.totalsportek.gives/", ["motogp"], matches, set())
        self.assertEqual([m[0] for m in matches],
                         ["https://live2.totalsportek.gives/MotoGP-Portuguese-Grand-Prix-vs-Live/59101"])

    def test_registered_as_builtin(self):
        self.assertIn("TotalSportek", [p.name for p in scrapers.ACTIVE_PROVIDERS])


if __name__ == "__main__":
    unittest.main()

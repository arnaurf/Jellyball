"""Motorsport opt-in groups: Formula 1, MotoGP and Formula E.

Each series is one channel that picks up every session of a race weekend, the
Formula 1 group adds the 24/7 F1 channels, and providers read their
series-only pages during that series' search and never during a team search.
"""

import asyncio
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

import catalog
import scrapers
import state
import stream_language
from leagues import F1, FORMULA_E, MOTOGP, MOTORSPORT_LEAGUES, UK_SPORTS_GROUP_KEY, schedule_league
from plugins import sdk
from sports_catalog import F1_SPECIAL_CHANNELS
from sports_matcher import match_team

MOTORSPORT_KEYS = tuple(league.key for league in MOTORSPORT_LEAGUES)


def _entries(groups):
    fetch = AsyncMock(return_value=())
    with patch.object(catalog, "_CATALOG_CACHE", {}), \
            patch.object(catalog, "_CATALOG_CACHE_LOADED_AT", 0.0), \
            patch.object(catalog, "_CATALOG_REMOTE_LOADED", False), \
            patch.object(catalog, "_fetch_espn_directory", fetch), \
            patch.object(state, "SHARED_HTTP_CLIENT", None):
        return asyncio.run(catalog.get_catalog_entries(groups))


def _series_terms():
    return {
        entry["category"]: entry["search_terms"]
        for entry in _entries(MOTORSPORT_KEYS)
        if entry["category"] in MOTORSPORT_KEYS
    }


class CatalogTests(unittest.TestCase):
    def test_each_series_is_one_channel_in_its_own_group(self):
        entries = {e["catalog_key"]: e for e in _entries(MOTORSPORT_KEYS)}
        for key, catalog_key, name in (
            ("f1", "team:f1:f1", "Formula 1"),
            ("motogp", "team:motogp:motogp", "MotoGP"),
            ("formula_e", "team:formula_e:formula-e", "Formula E"),
        ):
            with self.subTest(series=key):
                entry = entries[catalog_key]
                self.assertEqual(entry["name"], name)
                self.assertEqual(entry["catalog_group"], key)
                self.assertFalse(entry["always_live"])
                self.assertEqual(entry["logo_url"], "")

    def test_f1_group_carries_the_24_7_f1_channels(self):
        added = {e["catalog_key"] for e in _entries(("f1",)) if e.get("catalog_group") == "f1"}
        self.assertEqual(added, {"team:f1:f1", *(f"special:{c.key}" for c in F1_SPECIAL_CHANNELS)})
        dazn = next(e for e in _entries(("f1",)) if e["catalog_key"] == "special:dazn_f1")
        self.assertTrue(dazn["always_live"])
        self.assertEqual(catalog._channel_group_title(dazn), "Formula 1")

    def test_sky_sports_f1_is_listed_once_with_the_uk_group_on(self):
        entries = _entries(("f1", UK_SPORTS_GROUP_KEY))
        sky = [e for e in entries if e["catalog_key"] == "special:uk_sky_sports_f1"]
        self.assertEqual(len(sky), 1)
        self.assertEqual(sky[0]["catalog_group"], UK_SPORTS_GROUP_KEY)

    def test_no_schedule_lookup_and_season_windows(self):
        for league in MOTORSPORT_LEAGUES:
            self.assertFalse(schedule_league(league.key).has_schedule)
        self.assertTrue(catalog.is_in_season(F1.key, date(2026, 11, 21)))
        self.assertFalse(catalog.is_in_season(F1.key, date(2026, 1, 10)))
        self.assertFalse(catalog.is_in_season(MOTOGP.key, date(2026, 12, 20)))
        # Formula E's season wraps the new year.
        self.assertTrue(catalog.is_in_season(FORMULA_E.key, date(2027, 1, 10)))
        self.assertFalse(catalog.is_in_season(FORMULA_E.key, date(2026, 9, 1)))


class MatchingTests(unittest.TestCase):
    LISTINGS = {
        "Formula 1: Las Vegas Grand Prix - Race": ("/live/f1/las-vegas-grand-prix", "f1"),
        "Las Vegas Grand Prix": ("/f1/las-vegas-grand-prix/123", "f1"),
        "F1 Qualifying": ("/watch/1", "f1"),
        "Formula One Abu Dhabi GP Practice 1": ("/event/9", "f1"),
        "MotoGP: Valencia Grand Prix": ("/motogp/valencia-grand-prix/1", "motogp"),
        "Moto2 Race": ("/watch/2", "motogp"),
        "Moto GP Sprint": ("/watch/3", "motogp"),
        "Formula E: Sao Paulo E-Prix": ("/live/formula-e/sao-paulo", "formula_e"),
        "Mexico City ePrix": ("/watch/4", "formula_e"),
        "Buffalo Bills vs Miami Dolphins": ("/nfl/bills-dolphins", None),
        "F2 Feature Race": ("/live/f2/race", None),
        "Formula Europe": ("/watch/5", None),
    }

    def test_each_listing_matches_only_its_own_series(self):
        terms = _series_terms()
        for title, (href, series) in self.LISTINGS.items():
            for key in MOTORSPORT_KEYS:
                with self.subTest(title=title, channel=key):
                    self.assertEqual(match_team(terms[key], title, href=href)[0], key == series)


class SpanishFeedTests(unittest.TestCase):
    def _allowed(self, prefs, name):
        with patch.object(stream_language, "preferred_languages", return_value=prefs):
            return stream_language.listing_allowed(name, scrapers._is_non_english_channel)

    def test_a_region_tag_in_a_picked_language_is_kept(self):
        self.assertTrue(self._allowed(["eng", "spa"], "DAZN F1 ES"))
        self.assertTrue(self._allowed(["spa", "eng"], "DAZN F1 ES"))
        self.assertTrue(self._allowed(["spa"], "DAZN F1 ES"))

    def test_other_region_tags_are_still_skipped_for_english_first(self):
        self.assertFalse(self._allowed(["eng"], "DAZN F1 ES"))
        self.assertFalse(self._allowed(["eng", "spa"], "Canal+ F1 FR"))
        self.assertTrue(self._allowed(["eng", "spa"], "Sky Sports F1 UK"))

    def test_region_language(self):
        self.assertEqual(stream_language.region_language("DAZN F1 ES"), "spa")
        self.assertEqual(stream_language.region_language("ES: DAZN F1"), "spa")
        self.assertIsNone(stream_language.region_language("Bestest Sports"))


class CategoryPathTests(unittest.TestCase):
    def setUp(self):
        self.provider = sdk.HtmlAggregatorProvider(
            "Example", "https://example.test", ["/nfl"], ["/watch/"],
            category_paths={"F1": ["/f1"], "motogp": []},
        )

    def _scan(self, category):
        token = scrapers.SEARCH_CATEGORY.set(category)
        try:
            return self.provider.get_scan_urls()
        finally:
            scrapers.SEARCH_CATEGORY.reset(token)

    def test_series_pages_are_read_only_for_that_series(self):
        self.assertEqual(self._scan(""), ["https://example.test", "https://example.test/nfl"])
        self.assertEqual(self._scan("nfl"), ["https://example.test", "https://example.test/nfl"])
        self.assertEqual(
            self._scan("f1"), ["https://example.test", "https://example.test/nfl", "https://example.test/f1"]
        )
        self.assertEqual(self._scan("motogp"), ["https://example.test", "https://example.test/nfl"])

    def test_builtin_event_providers_add_motorsport_pages_only_for_motorsport(self):
        providers = [p for p in scrapers.ACTIVE_PROVIDERS if p not in scrapers.LINEAR_PROVIDERS]
        with_f1 = []
        for provider in providers:
            with self.subTest(provider=provider.name):
                team_urls = set(self._scan_of(provider, "nfl"))
                self.assertEqual(team_urls, set(self._scan_of(provider, "")))
                f1_urls = set(self._scan_of(provider, "f1"))
                self.assertTrue(team_urls <= f1_urls)
                if f1_urls - team_urls:
                    with_f1.append(provider.name)
        self.assertGreaterEqual(len(with_f1), 6)

    def _scan_of(self, provider, category):
        token = scrapers.SEARCH_CATEGORY.set(category)
        try:
            return provider.get_scan_urls()
        finally:
            scrapers.SEARCH_CATEGORY.reset(token)


class MasterScrapeCategoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_providers_see_the_channel_category_during_the_search(self):
        seen = []

        class Provider:
            name = "Fake"

            async def search(self, terms, browser=None, http_client=None):
                seen.append(sdk.current_search_category())
                return []

        async def no_browser():
            return None

        with patch.object(scrapers, "get_healthy_browser", no_browser), \
                patch.object(scrapers, "_providers_for_search", lambda always_live=False: [Provider()]):
            await scrapers.master_scrape("Formula 1", team_name="Formula 1", search_terms=["f1"], category="F1")
            await scrapers.master_scrape("Buffalo Bills", team_name="Buffalo Bills")
        self.assertEqual(seen, ["f1", ""])
        self.assertEqual(scrapers.SEARCH_CATEGORY.get(), "")


if __name__ == "__main__":
    unittest.main()

"""Motorsport session schedules (motorsport_schedule) and their guide blocks."""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import epg
import motorsport_schedule as ms


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc)


F1_RACES = [{
    "raceName": "Las Vegas Grand Prix",
    "Circuit": {"circuitName": "Las Vegas Strip Street Circuit"},
    "date": "2026-11-22", "time": "04:00:00Z",
    "FirstPractice": {"date": "2026-11-20", "time": "00:30:00Z"},
    "SecondPractice": {"date": "2026-11-20", "time": "04:00:00Z"},
    "ThirdPractice": {"date": "2026-11-21", "time": "00:30:00Z"},
    "Qualifying": {"date": "2026-11-21", "time": "04:00:00Z"},
}]


def motogp_session(name, klass, start, end, kind="PRACTICE", short="FP", type_="SESSION"):
    return {"name": name, "shortname": short, "kind": kind, "type": type_,
            "category": {"name": klass}, "date_start": start, "date_end": end}


MOTOGP_EVENTS = [{
    "name": "REPSOL GRAND PRIX OF PORTUGAL ",
    "circuit": {"name": "Autódromo Internacional do Algarve"},
    "broadcasts": [
        motogp_session("Pre-Event Press Conference", "MotoGP", "2026-11-19T16:00:00+0000",
                       "2026-11-19T16:30:00+0000", kind="PRESS", type_="MEDIA"),
        motogp_session("Free Practice Nr. 2", "MotoGP", "2026-11-21T10:10:00+0000", "2026-11-21T10:40:00+0000"),
        motogp_session("Qualifying Nr. 1 ", "MotoGP", "2026-11-21T10:50:00+0000", "2026-11-21T11:05:00+0000",
                       kind="QUALIFYING"),
        motogp_session("Tissot Sprint", "MotoGP", "2026-11-21T15:00:00+0000", "2026-11-21T15:00:00+0000",
                       kind="RACE", short="SPR"),
        motogp_session("Race", "Moto3", "2026-11-22T10:00:00+0000", "2026-11-22T10:00:00+0000",
                       kind="RACE", short="RAC"),
        motogp_session("Grand Prix", "MotoGP", "2026-11-22T13:00:00+0000", "2026-11-22T13:00:00+0000",
                       kind="RACE", short="RAC"),
    ],
}]


class ParseTests(unittest.TestCase):
    def test_f1_weekend_sessions(self):
        sessions = ms.parse_f1_races(F1_RACES)
        titles = [s.title for s in sessions]
        self.assertEqual(titles, [
            "F1 Practice 1: Las Vegas Grand Prix", "F1 Practice 2: Las Vegas Grand Prix",
            "F1 Practice 3: Las Vegas Grand Prix", "F1 Qualifying: Las Vegas Grand Prix",
            "F1 Race: Las Vegas Grand Prix",
        ])
        race = sessions[-1]
        self.assertEqual((race.start, race.stop), (utc(22, 4), utc(22, 6)))
        self.assertEqual(sessions[0].stop - sessions[0].start, timedelta(hours=1))

    def test_motogp_sessions_only_track_sessions_with_race_lengths(self):
        sessions = ms._normalize(ms.parse_motogp_events(MOTOGP_EVENTS))
        by_title = {s.title: s for s in sessions}
        self.assertNotIn("Press", " ".join(by_title))
        self.assertEqual(sorted(by_title), sorted([
            "MotoGP Free Practice 2: Grand Prix of Portugal",
            "MotoGP Qualifying 1: Grand Prix of Portugal",
            "MotoGP Sprint: Grand Prix of Portugal",
            "Moto3 Race: Grand Prix of Portugal",
            "MotoGP Race: Grand Prix of Portugal",
        ]))
        sprint = by_title["MotoGP Sprint: Grand Prix of Portugal"]
        self.assertEqual(sprint.stop - sprint.start, timedelta(minutes=30))
        race = by_title["MotoGP Race: Grand Prix of Portugal"]
        self.assertEqual(race.stop - race.start, timedelta(minutes=50))

    def test_normalize_trims_overlaps(self):
        a = ms.Session(utc(21, 10), utc(21, 11), "A", "")
        b = ms.Session(utc(21, 10, 30), utc(21, 11, 30), "B", "")
        result = ms._normalize([b, a])
        self.assertEqual([(s.title, s.stop) for s in result], [("A", utc(21, 10, 30)), ("B", utc(21, 11, 30))])


class ProgrammeTests(unittest.TestCase):
    def setUp(self):
        self.sessions = ms._normalize(ms.parse_motogp_events(MOTOGP_EVENTS))

    def test_blocks_cover_window_without_gaps(self):
        start, end = utc(21, 8), utc(23, 8)
        blocks = ms.programmes(self.sessions, "MotoGP", False, start, end)
        self.assertEqual(blocks[0][0], start)
        self.assertEqual(blocks[-1][1], end)
        for prev, nxt in zip(blocks, blocks[1:]):
            self.assertEqual(prev[1], nxt[0])

    def test_short_break_stays_with_previous_session(self):
        blocks = ms.programmes(self.sessions, "MotoGP", False, utc(21, 8), utc(21, 12))
        fp2 = next(b for b in blocks if b[2].startswith("MotoGP Free Practice 2"))
        self.assertEqual(fp2[1], utc(21, 10, 50))  # the 10 minutes before Q1 join FP2
        self.assertFalse(any(b[0] == utc(21, 10, 40) for b in blocks))

    def test_filler_names_event_and_24_7_channels(self):
        standby = ms.programmes(self.sessions, "MotoGP", False, utc(21, 8), utc(21, 9))
        self.assertEqual(standby[0][2], "MotoGP Standby")
        self.assertIn("Next: MotoGP Free Practice 2", standby[0][3])
        live = ms.programmes(self.sessions, "DAZN F1", True, utc(21, 8), utc(21, 9))
        self.assertEqual(live[0][2], "DAZN F1 Live")


class ChannelSeriesTests(unittest.TestCase):
    def test_series_for_channels(self):
        self.assertEqual(ms.channel_series({"category": "f1"}), "f1")
        self.assertEqual(ms.channel_series({"category": "motogp"}), "motogp")
        self.assertEqual(ms.channel_series({"category": "special", "catalog_key": "special:dazn_f1"}), "f1")
        self.assertEqual(ms.channel_series({"category": "special", "catalog_key": "special:uk_sky_sports_f1"}), "f1")
        self.assertEqual(ms.channel_series({"category": "formula_e"}), "")
        self.assertEqual(ms.channel_series({"category": "nba"}), "")


class RefreshTests(unittest.TestCase):
    def setUp(self):
        ms._CACHE.clear()
        self.addCleanup(ms._CACHE.clear)

    def test_failed_fetch_keeps_last_good_list(self):
        good = [ms.Session(utc(21, 10), utc(21, 11), "S", "")]
        fetcher = mock.AsyncMock(side_effect=[good, None])
        with mock.patch.dict(ms._FETCHERS, {"f1": fetcher}):
            self.assertEqual(asyncio.run(ms.refresh("f1")), good)
            ms._CACHE["f1"] = (0.0, good)  # expire it
            self.assertEqual(asyncio.run(ms.refresh("f1")), good)
        self.assertEqual(ms.cached_sessions("f1"), good)

    def test_unknown_series_never_fetches(self):
        self.assertIsNone(asyncio.run(ms.refresh("formula_e")))
        self.assertIsNone(ms.cached_sessions("formula_e"))


class GuideIntegrationTests(unittest.TestCase):
    def setUp(self):
        ms._CACHE.clear()
        self.addCleanup(ms._CACHE.clear)
        self.data = {"name": "MotoGP", "category": "motogp", "query": "MotoGP"}

    def _programmes(self, now):
        with mock.patch.object(epg, "_channel_is_off_season", return_value=False):
            return epg._channel_programmes("motogp", self.data, now, now + timedelta(days=2), {})

    def test_guide_uses_sessions_when_cached(self):
        ms._CACHE["motogp"] = (float("inf"), ms._normalize(ms.parse_motogp_events(MOTOGP_EVENTS)))
        titles = [p["title"] for p in self._programmes(utc(21, 8))]
        self.assertIn("MotoGP Sprint: Grand Prix of Portugal", titles)
        self.assertIn("MotoGP Race: Grand Prix of Portugal", titles)

    def test_guide_falls_back_without_schedule(self):
        titles = [p["title"] for p in self._programmes(utc(21, 8))]
        self.assertEqual(titles, ["MotoGP Standby"])

    def test_now_next_reads_sessions(self):
        ms._CACHE["motogp"] = (float("inf"), ms._normalize(ms.parse_motogp_events(MOTOGP_EVENTS)))
        with mock.patch.object(epg, "_channel_is_off_season", return_value=False), \
                mock.patch.object(epg, "datetime", wraps=datetime) as fake_dt:
            fake_dt.now.return_value = utc(21, 15, 10)
            result = epg.now_next_for_channel("motogp", self.data)
        self.assertEqual(result, {"now": "MotoGP Sprint: Grand Prix of Portugal", "next": "Moto3 Race: Grand Prix of Portugal"})


if __name__ == "__main__":
    unittest.main()

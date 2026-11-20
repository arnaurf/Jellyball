"""YouTube live channels, the picture-in-picture Multi-View layout, and the
member delay that syncs the two (a delayed channel playlist served from the
session's history)."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import channels
import epg
import failover
import main
import multiview
import routes_dashboard
import security
import state
import youtube_live
from hls_session import MAX_PLAYLIST_DELAY_SECONDS, ChannelSession, SessionConfig, SessionHooks, SessionSegment
from state import PLACEHOLDER_SESSION_ID, new_channel_state

HOST = "127.0.0.1:8000"


class YouTubeUrlTests(unittest.TestCase):
    def test_accepted_urls_are_canonical(self):
        cases = {
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "youtube.com/watch?v=dQw4w9WgXcQ": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://m.youtube.com/live/dQw4w9WgXcQ?feature=share": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com/@SomeChannel": "https://www.youtube.com/@SomeChannel/live",
            "https://www.youtube.com/@SomeChannel/live": "https://www.youtube.com/@SomeChannel/live",
            "https://www.youtube.com/channel/UC1234567890/streams": "https://www.youtube.com/channel/UC1234567890/live",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(youtube_live.normalize_youtube_url(value), expected)

    def test_anything_else_is_rejected(self):
        for value in (
            "", "https://example.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/watch?v=short",
            "file:///etc/passwd", "https://www.youtube.com.evil.test/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com:8443/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/playlist?list=PL123",
            "https://user@www.youtube.com/watch?v=dQw4w9WgXcQ",
        ):
            with self.subTest(value=value):
                self.assertIsNone(youtube_live.normalize_youtube_url(value))


class YouTubeResolveTests(unittest.IsolatedAsyncioTestCase):
    URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def setUp(self):
        youtube_live._LAST_WARNED.clear()

    async def resolve(self, info=None, error=None):
        def extract(url):
            self.assertEqual(url, self.URL)
            if error is not None:
                raise error
            return info

        with patch.object(youtube_live, "_extract_sync", extract):
            return await youtube_live.resolve_live_stream(self.URL, "Commentary")

    MASTER = "https://manifest.googlevideo.com/api/manifest/hls_variant/id/x/file/index.m3u8"

    async def test_a_live_stream_becomes_its_master_playlist(self):
        # What YouTube returns since 2026: video-only and audio-only HLS
        # formats that share one master playlist (with EXT-X-MEDIA audio).
        streams = await self.resolve({
            "is_live": True, "title": "Live race commentary",
            "requested_formats": [
                {"format_id": "232", "protocol": "m3u8_native", "acodec": "none",
                 "url": "https://manifest.googlevideo.com/api/manifest/hls_playlist/itag/232/index.m3u8",
                 "manifest_url": self.MASTER},
                {"format_id": "234", "protocol": "m3u8_native", "vcodec": "none",
                 "url": "https://manifest.googlevideo.com/api/manifest/hls_playlist/itag/234/index.m3u8",
                 "manifest_url": self.MASTER},
            ],
        })
        self.assertEqual(len(streams), 1)
        stream = streams[0]
        self.assertEqual(stream["url"], self.MASTER)
        self.assertEqual((stream["provider"], stream["match_title"]), ("YouTube", "Live race commentary"))

    async def test_a_single_format_with_audio_is_used_when_there_is_no_master(self):
        muxed = "https://manifest.googlevideo.com/api/manifest/hls_playlist/itag/95/index.m3u8"
        streams = await self.resolve({"is_live": True, "protocol": "m3u8_native", "acodec": "mp4a.40.2", "url": muxed})
        self.assertEqual(streams[0]["url"], muxed)
        # A video-only format alone would play silently: not used.
        self.assertEqual(await self.resolve({"is_live": True, "protocol": "m3u8_native", "acodec": "none", "url": muxed}), [])

    async def test_not_live_or_failing_gives_nothing(self):
        self.assertEqual(await self.resolve({"is_live": False, "live_status": "is_upcoming", "url": "https://x.test/a.m3u8"}), [])
        self.assertEqual(await self.resolve(error=RuntimeError("ERROR: This live event will begin in 2 hours")), [])
        self.assertEqual(await self.resolve({"is_live": True, "protocol": "m3u8_native", "acodec": "aac",
                                             "url": "http://127.0.0.1/private.m3u8"}), [])

    async def test_an_unsupported_url_is_never_resolved(self):
        extract = AsyncMock()
        with patch.object(youtube_live, "_extract_sync", extract):
            self.assertEqual(await youtube_live.resolve_live_stream("https://example.com/live"), [])
        extract.assert_not_called()


def _session(window_segments=6):
    async def unused(*_args, **_kwargs):
        return None

    hooks = SessionHooks(
        fetch=unused, headers_for=lambda _spec: {}, resolve_source=lambda _id: None,
        report_failure=lambda *_a: None, report_incompatible=lambda *_a: None,
    )
    config = SessionConfig(
        min_start_segments=3, window_min_segments=window_segments, window_min_seconds=12.0, window_max_segments=window_segments,
    )
    with patch("hls_session._load_persisted_next_seq", return_value=1000):
        return ChannelSession("yt", hooks, config)


class FakeClock:
    def __init__(self):
        self.now = 10_000.0

    def __call__(self):
        return self.now


class DelayedPlaylistTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        patcher = patch("hls_session.time.monotonic", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.session = _session()

    def publish(self, count, duration=2.0, discontinuity_at=(), renew=0.0):
        """One segment completes every `duration` seconds; `renew` re-requests
        that delay on each one, as a reader polling the delayed playlist does."""
        for index in range(count):
            self.clock.now += duration
            if renew:
                self.session.request_delay(renew)
            self.session._publish(SessionSegment(
                seq=self.session.next_seq, duration=duration,
                discontinuity=index in discontinuity_at, data=b"x" * 188,
            ))

    @staticmethod
    def seqs(playlist):
        return [int(line.split("/")[-1].removesuffix(".ts")) for line in playlist.splitlines() if line.endswith(".ts")]

    def test_a_delay_shorter_than_the_window_is_served_at_once(self):
        self.publish(6)  # 12 s of window
        self.assertEqual(self.session.request_delay(4), 4)
        self.assertTrue(self.session.delayed_ready(4))
        live = self.seqs(self.session.render_playlist("yt/seg/"))
        delayed = self.seqs(self.session.render_playlist("yt/seg/", delay=4))
        self.assertEqual(live[-1], 1005)
        self.assertEqual(delayed[-1], 1003)  # 2 segments (4 s) behind the live edge

    def test_a_longer_delay_builds_history_beyond_the_window(self):
        self.publish(6)
        self.session.request_delay(30)
        self.assertFalse(self.session.delayed_ready(30))
        self.publish(20, renew=30)  # 40 s later
        self.assertTrue(self.session.delayed_ready(30))
        delayed = self.seqs(self.session.render_playlist("yt/seg/", delay=30))
        live = self.seqs(self.session.render_playlist("yt/seg/"))
        self.assertEqual(live[-1] - delayed[-1], 15)  # 30 s at 2 s per segment
        self.assertLess(delayed[0], live[0])  # older than the window...
        for seq in delayed:
            self.assertIsNotNone(self.session.get_segment(seq))  # ...but still served

    def test_history_is_released_once_nobody_asks(self):
        self.publish(3)
        self.session.request_delay(10)
        self.publish(5)
        self.assertTrue(self.session.history)
        self.clock.now += 200
        self.publish(1)
        self.assertFalse(self.session.history)

    def test_history_is_bounded_and_the_delay_capped(self):
        self.assertEqual(self.session.request_delay(10_000), MAX_PLAYLIST_DELAY_SECONDS)
        self.publish(200, renew=10_000)  # 400 s
        oldest = self.clock.now - self.session.history[0].added_at
        self.assertLessEqual(oldest, MAX_PLAYLIST_DELAY_SECONDS + 50)

    def test_discontinuity_sequence_counts_tags_that_left_the_delayed_playlist(self):
        self.session.request_delay(4)
        self.publish(12, discontinuity_at=(2,))
        playlist = self.session.render_playlist("yt/seg/", delay=4)
        self.assertIn("#EXT-X-DISCONTINUITY-SEQUENCE:1", playlist)
        self.assertNotIn("#EXT-X-DISCONTINUITY\n", playlist)


class PipLayoutTests(unittest.TestCase):
    def setUp(self):
        self._backup = dict(state.stream_state)
        state.stream_state.update({"race": {"name": "Race"}, "yt": {"name": "Commentary"}})
        self.addCleanup(self._restore)

    def _restore(self):
        state.stream_state.clear()
        state.stream_state.update(self._backup)

    def data(self, delays=None):
        return {"name": "PiP", "layout": "pip", "member_team_ids": ["race", "yt"], "member_delays": delays or []}

    def test_filter_overlays_the_corner_on_the_main_feed(self):
        graph = multiview._build_xstack_filter("pip", 2)
        self.assertIn("[0:v]scale=1920:1080", graph)
        self.assertIn("[1:v]scale=640:360", graph)
        self.assertIn("[v0][v1]overlay=x=W-w-32:y=H-h-32", graph)
        self.assertTrue(graph.endswith("[vout]"))
        self.assertNotIn("xstack", graph)

    def test_offset_round_trip_and_inputs(self):
        self.assertEqual(multiview.offset_to_member_delays(12.5), [0.0, 12.5])
        self.assertEqual(multiview.offset_to_member_delays(-8), [8.0, 0.0])
        self.assertEqual(multiview.offset_to_member_delays(9999), [0.0, MAX_PLAYLIST_DELAY_SECONDS])
        data = self.data(multiview.offset_to_member_delays(12.5))
        self.assertEqual(multiview.member_offset(data), 12.5)
        args = multiview._build_multiview_ffmpeg_args("mv", data, None, [True, True], "none")
        inputs = [args[i + 1] for i, arg in enumerate(args) if arg == "-i"]
        self.assertTrue(inputs[0].endswith("/stream/race.m3u8"))
        self.assertTrue(inputs[1].endswith("/stream/yt.m3u8?delay=12.5"))
        # A stand-in pane never asks the placeholder for a delay.
        args = multiview._build_multiview_ffmpeg_args(
            "mv", data, None, [True, False], "none", input_ids=["race", PLACEHOLDER_SESSION_ID],
        )
        inputs = [args[i + 1] for i, arg in enumerate(args) if arg == "-i"]
        self.assertTrue(inputs[1].endswith(f"/stream/{PLACEHOLDER_SESSION_ID}.m3u8"))

    def test_stored_delays_are_sanitized(self):
        self.assertEqual(multiview.parse_member_delays("", 2), [0.0, 0.0])
        self.assertEqual(multiview.parse_member_delays("[0, 15]", 2), [0.0, 15.0])
        self.assertEqual(multiview.parse_member_delays('["x", -4, 1e9]', 3), [0.0, 0.0, MAX_PLAYLIST_DELAY_SECONDS])
        self.assertEqual(multiview.parse_member_delays("not json", 2), [0.0, 0.0])

    def test_pip_plays_the_corner_audio_by_default(self):
        self.assertEqual(multiview.default_audio_team_id("pip", ["race", "yt"]), "yt")
        self.assertEqual(multiview.default_audio_team_id("side_by_side_2", ["race", "yt"]), "race")
        self.assertEqual(epg._multiview_pane_labels("pip", 2), ["Main", "Corner"])


class WarmUpDelayTests(unittest.IsolatedAsyncioTestCase):
    class FakeSession:
        def __init__(self, delayed_ok=True):
            self.delayed_ok = delayed_ok
            self.requested = []
            self.has_audio = True
            self.codec_signature = ("h264", 0x0F)

        def touch(self):
            pass

        def request_delay(self, seconds):
            self.requested.append(seconds)
            return seconds

        async def wait_ready(self, _timeout):
            return True

        async def wait_delayed_ready(self, delay, timeout):
            self.waited = (delay, timeout)
            return self.delayed_ok

    class Registry:
        def __init__(self, sessions):
            self.sessions = sessions

        def get(self, session_id):
            return self.sessions[session_id]

        def peek(self, session_id):
            return self.sessions.get(session_id)

    async def warm(self, delayed_ok):
        sessions = {"race": self.FakeSession(), "yt": self.FakeSession(delayed_ok), PLACEHOLDER_SESSION_ID: self.FakeSession()}
        with patch.object(multiview, "SESSIONS", self.Registry(sessions)), \
                patch.dict(state.stream_state, {"race": {}, "yt": {}}):
            inputs = await multiview._warm_multiview_members(["race", "yt"], [0.0, 20.0])
        return sessions, inputs

    async def test_the_delayed_member_buffers_before_ffmpeg_starts(self):
        sessions, inputs = await self.warm(delayed_ok=True)
        self.assertEqual(sessions["yt"].requested[0], 20.0)
        self.assertEqual(sessions["yt"].waited[0], 20.0)
        self.assertEqual(sessions["race"].requested, [])
        self.assertEqual([item.session_id for item in inputs], ["race", "yt"])

    async def test_a_member_that_cannot_buffer_shows_no_signal(self):
        _, inputs = await self.warm(delayed_ok=False)
        self.assertEqual(inputs[1].session_id, PLACEHOLDER_SESSION_ID)
        self.assertTrue(inputs[1].standin)


def _http(method, path, **kwargs):
    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url=f"http://{HOST}", follow_redirects=False,
        ) as client:
            return await client.request(method, path, **kwargs)

    with patch.object(security, "DASHBOARD_PASSWORD", ""), \
            patch.object(routes_dashboard, "trigger_jellyfin_refresh", AsyncMock(return_value=True)):
        return asyncio.run(go())


class DashboardRouteTests(unittest.TestCase):
    def setUp(self):
        self._backup = dict(state.stream_state)
        state.stream_state.clear()
        state.stream_state.update({
            "race": new_channel_state(name="Race", query="race"),
            "yt": new_channel_state(name="Commentary", query="https://www.youtube.com/@x/live", category="youtube"),
        })
        self.save = AsyncMock()
        patcher = patch.object(routes_dashboard, "save_multiview_channel_async", self.save)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._restore)

    def _restore(self):
        multiview._forget_multiview_channel("multiview_f1_pip")
        state.stream_state.clear()
        state.stream_state.update(self._backup)

    def create(self, **extra):
        return _http("POST", "/multiview/create", data={
            "name": "F1 PiP", "layout": "pip", "member_team_ids": "race,yt", **extra,
        })

    def test_create_pip_with_an_offset(self):
        response = self.create(offset_seconds="-6.5")
        self.assertEqual(response.status_code, 303)
        data = state.stream_state["multiview_f1_pip"]
        self.assertEqual((data["layout"], data["active_audio_team_id"]), ("pip", "yt"))
        self.assertEqual(data["member_delays"], [6.5, 0.0])
        self.assertEqual(self.save.await_args.kwargs["member_delays"], [6.5, 0.0])

    def test_out_of_range_offset_is_rejected(self):
        self.assertEqual(self.create(offset_seconds="500").status_code, 400)
        self.assertEqual(self.create(offset_seconds="soon").status_code, 400)

    def test_set_offset_restarts_and_set_audio_keeps_it(self):
        self.create()
        restart = AsyncMock(return_value=True)
        with patch.object(routes_dashboard, "_restart_multiview", restart):
            response = _http("POST", "/multiview/multiview_f1_pip/set-offset", data={"offset_seconds": "15"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(state.stream_state["multiview_f1_pip"]["member_delays"], [0.0, 15.0])
        restart.assert_awaited_once()
        self.assertFalse(restart.await_args.kwargs["count_toward_backoff"])
        response = _http("POST", "/multiview/multiview_f1_pip/set-audio", data={"active_audio_team_id": "race"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.save.await_args.kwargs["member_delays"], [0.0, 15.0])

    def test_offset_needs_a_two_member_view(self):
        state.stream_state["grid"] = new_channel_state(
            name="Grid", type="multiview", layout="grid_2x2", member_team_ids=["race", "yt", "a", "b"],
        )
        response = _http("POST", "/multiview/grid/set-offset", data={"offset_seconds": "5"})
        self.assertEqual(response.status_code, 400)

    def test_add_youtube_channel(self):
        save = AsyncMock()
        with patch.object(channels, "save_team_async", save), \
                patch.object(channels, "_start_team_scrape_loop") as start:
            response = _http("POST", "/add_youtube", data={"name": "F1 Commentary", "youtube_url": "https://youtu.be/dQw4w9WgXcQ"})
            bad = _http("POST", "/add_youtube", data={"name": "x", "youtube_url": "https://example.com/v"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(bad.status_code, 400)
        data = state.stream_state["youtube_f1_commentary"]
        self.assertEqual((data["category"], data["query"], data["always_live"]),
                         ("youtube", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", True))
        self.assertTrue(save.await_args.kwargs["always_live"])
        start.assert_called_once_with("youtube_f1_commentary")


class YouTubeScrapeTests(unittest.TestCase):
    def test_a_youtube_channel_resolves_instead_of_searching_providers(self):
        channel_state = {"yt": new_channel_state(
            name="Commentary", query="https://www.youtube.com/@x/live", category="youtube", always_live=True,
        )}
        stream = {"url": "https://manifest.googlevideo.com/a.m3u8", "provider": "YouTube", "match_score": 100}
        resolve = AsyncMock(return_value=[stream])
        search = AsyncMock(return_value=[])
        with patch.object(failover, "stream_state", channel_state), \
                patch.object(failover, "master_scrape", search), \
                patch.object(failover, "update_team_meta_async", AsyncMock()), \
                patch.object(failover, "resolve_espn_logo", return_value=""), \
                patch.object(youtube_live, "resolve_live_stream", resolve):
            asyncio.run(failover._trigger_scrape_unlocked("yt", force=True))
        resolve.assert_awaited_once_with("https://www.youtube.com/@x/live", "Commentary")
        search.assert_not_awaited()
        self.assertEqual(channel_state["yt"]["candidates"][0]["url"], stream["url"])


class DelayPersistenceTests(unittest.TestCase):
    def test_round_trip(self):
        import tempfile
        from pathlib import Path

        import db

        with tempfile.TemporaryDirectory() as tmp, patch.object(db, "DB_FILE", Path(tmp) / "j.db"):
            db.init_db()
            db.save_multiview_channel("mv", "PiP", "pip", ["race", "yt"], "yt", member_delays=[0.0, 12.0])
            db.save_multiview_channel("mv2", "Grid", "side_by_side_2", ["race", "yt"], "race")
            rows = {row.channel_id: row for row in db.load_multiview_channels()}
            db.close_all_db_connections()
        self.assertEqual(json.loads(rows["mv"].member_delays), [0.0, 12.0])
        self.assertEqual(rows["mv2"].member_delays, "")


if __name__ == "__main__":
    unittest.main()

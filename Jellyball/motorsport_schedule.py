"""Motorsport session schedules (F1, MotoGP) for the guide.

ESPN has no per-session racing schedule, so a motorsport channel's guide comes
from each series' own public timetable instead:

  F1      Jolpica (the Ergast successor): every practice, sprint, qualifying
          and race session of the season, start times in UTC.
  MotoGP  the official MotoGP API: every MotoGP, Moto2 and Moto3 session of
          each event, with start and end times.

Formula E has no reachable public timetable, so its channel keeps the generic
guide. Schedules are cached per series (successes for
MOTORSPORT_SCHEDULE_CACHE_SECONDS, failures briefly). The guide reads the
cache synchronously (cached_sessions); refresh() fills it, and is awaited by
the XMLTV build and each motorsport channel's scrape loop.
"""

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Awaitable, Dict, List, Optional, Tuple

import httpx

from config import LOGGER, _log_failure
from network_safety import bounded_float, safe_get
import state


MOTORSPORT_SCHEDULE_CACHE_SECONDS = bounded_float(
    os.getenv("MOTORSPORT_SCHEDULE_CACHE_SECONDS", "3600"), 3600.0, 300.0, 21600.0
)
_FAILURE_CACHE_SECONDS = 300.0

F1_SCHEDULE_URL = "https://api.jolpi.ca/ergast/f1/{year}/races/"
MOTOGP_SCHEDULE_URL = "https://api.motogp.pulselive.com/motogp/v1/events"

# Special channels (catalog key) that carry a series' sessions.
_SPECIAL_CHANNEL_SERIES = {
    "uk_sky_sports_f1": "f1",
    "f1_tv": "f1",
    "dazn_f1": "f1",
}


@dataclass(frozen=True)
class Session:
    start: datetime
    stop: datetime
    title: str
    desc: str


_CACHE: Dict[str, Tuple[float, Optional[List[Session]]]] = {}


def channel_series(data: dict) -> str:
    """The series ("f1", "motogp") whose sessions this channel shows, or ""."""
    category = str(data.get("category") or "")
    if category in _FETCHERS:
        return category
    catalog_key = str(data.get("catalog_key") or "")
    if catalog_key.startswith("special:"):
        return _SPECIAL_CHANNEL_SERIES.get(catalog_key.removeprefix("special:"), "")
    return ""


def cached_sessions(series: str) -> Optional[List[Session]]:
    """The series' cached sessions (possibly stale), or None when never fetched
    successfully. Never fetches: the dashboard and guide call it synchronously."""
    cached = _CACHE.get(series)
    return cached[1] if cached else None


async def refresh(series: str) -> Optional[List[Session]]:
    """Fetch the series' sessions unless the cache is fresh; keeps the last good
    list when a fetch fails."""
    fetcher = _FETCHERS.get(series)
    if fetcher is None:
        return None
    cached = _CACHE.get(series)
    if cached and time.monotonic() < cached[0]:
        return cached[1]
    sessions = await _with_client(fetcher)
    if sessions is None:
        previous = cached[1] if cached else None
        _CACHE[series] = (time.monotonic() + _FAILURE_CACHE_SECONDS, previous)
        return previous
    _CACHE[series] = (time.monotonic() + MOTORSPORT_SCHEDULE_CACHE_SECONDS, sessions)
    return sessions


async def _with_client(fetcher: Callable[[httpx.AsyncClient], Awaitable[Optional[List[Session]]]]):
    owns_client = state.SHARED_HTTP_CLIENT is None
    client = state.SHARED_HTTP_CLIENT or httpx.AsyncClient(timeout=10.0, follow_redirects=False)
    try:
        return await fetcher(client)
    except Exception as exc:  # noqa: BLE001 - a guide source must never break the guide
        _log_failure("fetch motorsport schedule", exc)
        return None
    finally:
        if owns_client:
            await client.aclose()


async def _get_json(client: httpx.AsyncClient, url: str, **kwargs):
    resp = await safe_get(client, url, timeout=10.0, **kwargs)
    if resp is None or resp.status_code != 200:
        LOGGER.warning("Motorsport schedule request %s status=%s", url.split("/")[2],
                       resp.status_code if resp is not None else "fetch-failed")
        return None
    return resp.json()


# --- F1 ---------------------------------------------------------------------

# Jolpica session key -> (guide title, block length).
_F1_SESSIONS = (
    ("FirstPractice", "Practice 1", timedelta(hours=1)),
    ("SecondPractice", "Practice 2", timedelta(hours=1)),
    ("ThirdPractice", "Practice 3", timedelta(hours=1)),
    ("SprintQualifying", "Sprint Qualifying", timedelta(minutes=45)),
    ("SprintShootout", "Sprint Shootout", timedelta(minutes=45)),
    ("Sprint", "Sprint", timedelta(hours=1)),
    ("Qualifying", "Qualifying", timedelta(hours=1)),
)
_F1_RACE_LENGTH = timedelta(hours=2)


def _f1_time(block: dict) -> Optional[datetime]:
    day, clock = block.get("date"), block.get("time")
    if not day or not clock:
        return None
    return datetime.fromisoformat(f"{day}T{clock.replace('Z', '+00:00')}").astimezone(timezone.utc)


def parse_f1_races(races: List[dict]) -> List[Session]:
    sessions: List[Session] = []
    for race in races:
        name = str(race.get("raceName") or "Grand Prix")
        circuit = str((race.get("Circuit") or {}).get("circuitName") or "")
        where = f" at {circuit}" if circuit else ""
        for key, label, length in _F1_SESSIONS:
            start = _f1_time(race.get(key) or {})
            if start:
                sessions.append(Session(start, start + length, f"F1 {label}: {name}",
                                        f"Formula 1 {label}, {name}{where}."))
        start = _f1_time(race)
        if start:
            sessions.append(Session(start, start + _F1_RACE_LENGTH, f"F1 Race: {name}",
                                    f"Formula 1 {name}{where}."))
    return sessions


async def _fetch_f1(client: httpx.AsyncClient) -> Optional[List[Session]]:
    year = datetime.now(timezone.utc).year
    data = await _get_json(client, F1_SCHEDULE_URL.format(year=year), params={"limit": "100"})
    if data is None:
        return None
    races = data.get("MRData", {}).get("RaceTable", {}).get("Races", [])
    return _normalize(parse_f1_races(races))


# --- MotoGP -----------------------------------------------------------------

# Block length when the API gives a race only a start time (end == start).
_MOTOGP_RACE_LENGTH = {"MotoGP": timedelta(minutes=50), "Moto2": timedelta(minutes=45),
                       "Moto3": timedelta(minutes=45)}
_MOTOGP_SPRINT_LENGTH = timedelta(minutes=30)
_MOTOGP_DEFAULT_LENGTH = timedelta(minutes=45)


def _motogp_event_name(raw: str) -> str:
    name = " ".join(raw.split()).title().replace(" Of ", " of ")
    # Drop the title sponsor: "Repsol Grand Prix of Portugal" -> "Grand Prix of Portugal".
    match = re.search(r"Grand Prix.*", name)
    return match.group(0) if match else name


def _motogp_session_name(raw: str) -> str:
    name = re.sub(r"\s*Nr\.\s*", " ", " ".join(raw.split())).strip()
    if "sprint" in name.casefold():
        return "Sprint"  # "Tissot Sprint": drop the sponsor
    if name.casefold() == "grand prix":
        return "Race"
    return name


def parse_motogp_events(events: List[dict]) -> List[Session]:
    sessions: List[Session] = []
    for event in events:
        event_name = _motogp_event_name(str(event.get("name") or "Grand Prix"))
        circuit = str((event.get("circuit") or {}).get("name") or "")
        where = f" at {circuit}" if circuit else ""
        for item in event.get("broadcasts") or []:
            if item.get("type") != "SESSION":
                continue
            klass = str((item.get("category") or {}).get("name") or "MotoGP")
            try:
                start = datetime.fromisoformat(str(item["date_start"])).astimezone(timezone.utc)
                stop = datetime.fromisoformat(str(item["date_end"])).astimezone(timezone.utc)
            except (KeyError, TypeError, ValueError):
                continue
            session = _motogp_session_name(str(item.get("name") or item.get("shortname") or "Session"))
            if stop <= start:
                if item.get("shortname") == "SPR" or "sprint" in session.casefold():
                    stop = start + _MOTOGP_SPRINT_LENGTH
                elif item.get("kind") == "RACE":
                    stop = start + _MOTOGP_RACE_LENGTH.get(klass, _MOTOGP_DEFAULT_LENGTH)
                else:
                    stop = start + _MOTOGP_DEFAULT_LENGTH
            sessions.append(Session(start, stop, f"{klass} {session}: {event_name}",
                                    f"{klass} {session}, {event_name}{where}."))
    return sessions


async def _fetch_motogp(client: httpx.AsyncClient) -> Optional[List[Session]]:
    year = datetime.now(timezone.utc).year
    data = await _get_json(client, MOTOGP_SCHEDULE_URL, params={"seasonYear": str(year)})
    if not isinstance(data, list):
        return None
    return _normalize(parse_motogp_events(data))


def _normalize(sessions: List[Session]) -> List[Session]:
    """Sorted, with any overlap trimmed so guide blocks never overlap."""
    result: List[Session] = []
    for session in sorted(sessions, key=lambda s: s.start):
        if result and session.start < result[-1].stop:
            last = result[-1]
            result[-1] = Session(last.start, session.start, last.title, last.desc)
            if result[-1].stop <= result[-1].start:
                result.pop()
        result.append(session)
    return result


_FETCHERS: Dict[str, Callable[[httpx.AsyncClient], Awaitable[Optional[List[Session]]]]] = {
    "f1": _fetch_f1,
    "motogp": _fetch_motogp,
}


_SHORT_GAP = timedelta(minutes=30)


def programmes(
    sessions: List[Session],
    name: str,
    always_live: bool,
    guide_start: datetime,
    guide_end: datetime,
) -> List[Tuple[datetime, datetime, str, str, str]]:
    """(start, stop, title, desc, category) blocks covering the whole guide
    window: the sessions inside it, with filler in between ("<name> Live" on a
    24/7 channel, "<name> Standby" on an event channel)."""
    blocks: List[Tuple[datetime, datetime, str, str, str]] = []
    inside = [s for s in sessions if s.stop > guide_start and s.start < guide_end]

    def filler(start: datetime, stop: datetime, upcoming: Optional[Session]) -> None:
        if stop <= start:
            return
        nxt = f" Next: {upcoming.title}." if upcoming else ""
        if always_live:
            blocks.append((start, stop, f"{name} Live", f"Live broadcast on {name}.{nxt}", "Sports"))
        else:
            blocks.append((start, stop, f"{name} Standby", f"No session on track right now.{nxt}", ""))

    cursor = guide_start
    session_ended_at: Optional[datetime] = None
    for session in inside:
        start, stop = max(session.start, guide_start), min(session.stop, guide_end)
        if session_ended_at == cursor and start - cursor < _SHORT_GAP:
            # A short break between two sessions (MotoGP runs them minutes
            # apart) stays with the session before it rather than a sliver of filler.
            last = blocks[-1]
            blocks[-1] = (last[0], start, last[2], last[3], last[4])
            cursor = start
        filler(cursor, start, session)
        blocks.append((start, stop, session.title, session.desc, "Sports"))
        cursor = session_ended_at = stop
    later = next((s for s in sessions if s.start >= guide_end), None)
    filler(cursor, guide_end, later)
    return blocks

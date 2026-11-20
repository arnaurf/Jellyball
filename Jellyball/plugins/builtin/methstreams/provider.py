"""Built-in provider plugin: MethStreamsScraper."""

import os

from plugins.sdk import HtmlAggregatorProvider


class MethStreamsScraper(HtmlAggregatorProvider):
    def __init__(self):
        super().__init__(
            "MethStreams",
            os.getenv("AGGREGATOR_3_URL", "https://methstreams.click"),
            # The home page only links the per-sport schedules; the events
            # (/live/<sport>/<slug>) are listed on these pages (issue #49).
            ["/nflstreams", "/nbastreams", "/nhlstreams", "/mlbstreams", "/cfbstreams", "/ncaabstreams", "/soccerstreams"],
            ["/game/", "/match/", "/live/"],
            # Read only while searching for that series' channel; its events
            # are /live/<sport>/<slug> like every other sport's.
            category_paths={"f1": ["/f1streams"], "motogp": ["/motogpstreams"]},
        )

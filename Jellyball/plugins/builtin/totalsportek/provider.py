"""Built-in provider plugin: TotalSportekScraper."""

import os

from plugins.sdk import HtmlAggregatorProvider


class TotalSportekScraper(HtmlAggregatorProvider):
    def __init__(self):
        super().__init__(
            "TotalSportek",
            # One of several numbered mirrors (live2, live4, ...); its home
            # page lists every sport, motorsport included.
            os.getenv("AGGREGATOR_13_URL", "https://live2.totalsportek.gives"),
            [],
            [],
            # Event pages are /<Event-Slug>-vs-Live/<numeric id>, e.g.
            # /MotoGP-Portuguese-Grand-Prix-vs-Live/59101; each has a player.
            event_path_patterns=[r"^/[A-Za-z0-9-]+-vs-Live/\d+/?$"],
        )

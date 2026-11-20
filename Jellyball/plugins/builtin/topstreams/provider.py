"""Built-in provider plugin: TopStreamsScraper."""

import os

from plugins.sdk import HtmlAggregatorProvider


class TopStreamsScraper(HtmlAggregatorProvider):
    def __init__(self):
        super().__init__(
            "TopStreams",
            os.getenv("AGGREGATOR_12_URL", "https://topstreams.info"),
            ["/nfl", "/nba", "/nhl", "/mlb", "/soccer"],
            ["/watch/", "/match/", "/live/"],
            # Read only while searching for that series' channel.
            category_paths={"f1": ["/f1"], "motogp": ["/motogp"]},
        )

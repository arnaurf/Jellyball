"""Built-in provider plugin: ISportSurgeScraper."""

import os

from plugins.sdk import HtmlAggregatorProvider


class ISportSurgeScraper(HtmlAggregatorProvider):
    def __init__(self):
        super().__init__(
            "iSportSurge",
            os.getenv("AGGREGATOR_1_URL", "https://isportsurge.ws"),
            [
                "/cfb/livestreams2", "/nfl/livestreams3", "/mlb/livestreams2",
                "/nba/livestreams3", "/nhl/livestreams3", "/soccer/livestreams",
            ],
            ["/watch/", "/event/", "/title-game/"],
            # Read only while searching for that series' channel.
            category_paths={"f1": ["/f1/livestreams"], "motogp": ["/motogp/livestreams"]},
        )

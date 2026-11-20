"""Built-in provider plugin: FootybiteScraper."""

import os

from plugins.sdk import HtmlAggregatorProvider


class FootybiteScraper(HtmlAggregatorProvider):
    def __init__(self):
        super().__init__(
            "Footybite",
            os.getenv("AGGREGATOR_9_URL", "https://footybite.im"),
            [],
            # Event pages moved to /game/<slug> (issue #49).
            ["/watch/", "/stream/", "/live/", "/match/", "/game/"],
            # An event page has no player: it lists other sites' streams
            # ("Available Streams") under #streams.
            outbound_link_selector="#streams a[href]",
            # Motorsport sits under "More" in the menu, on its own pages.
            category_paths={"f1": ["/f1-stream"], "motogp": ["/motogp-stream"]},
        )

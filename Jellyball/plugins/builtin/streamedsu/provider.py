"""Built-in provider plugin: StreamedSuScraper."""

import os

from plugins.sdk import HtmlAggregatorProvider


class StreamedSuScraper(HtmlAggregatorProvider):
    def __init__(self):
        super().__init__(
            "Streamed",
            os.getenv("AGGREGATOR_11_URL", "https://streamed.su"),
            ["/category/football", "/category/american-football", "/category/basketball", "/category/baseball", "/category/hockey"],
            ["/watch/", "/live/"],
            # Read only while searching for a motorsport channel: Streamed
            # lists every racing series under motor sports.
            category_paths={
                "f1": ["/category/motor-sports"],
                "motogp": ["/category/motor-sports"],
                "formula_e": ["/category/motor-sports"],
            },
        )

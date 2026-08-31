"""Example parser for schema example.v1.

A parser is a pure function of the response bytes. It runs at derive time,
never at capture time — a bug here is fixed by bumping PARSER_VERSION and
re-parsing the archive.

In a domain repo, keep parsers in a `parsers/` package and run:

    wss derive --parsers parsers.example_v1
"""

import json

from wss import derive

PARSER_VERSION = "1"


def parse(body: bytes, ctx: derive.ParseContext):
    data = json.loads(body)
    for item in data["items"]:
        yield derive.Observation(
            entity_id=item["id"],
            metric="count",
            value=int(item["count"]),
            unit="count",
            # observed_at stays None → derive fills in each row's fetch time.
            # Set it only when the payload itself carries the observation time:
            # observed_at=item["as_of"],
        )


derive.register("example.v1", parse, PARSER_VERSION)

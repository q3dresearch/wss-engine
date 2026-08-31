"""EXAMPLE PARSER for schema_id `example.v1` — copy, edit, delete this one.

A parser is a pure function of the archived response bytes. It runs at derive
time, never at capture time, so a bug here is fixed by bumping
PARSER_VERSION and re-parsing the archive — never by re-fetching a page that
has since changed.

The workflows auto-discover every module in this package, so adding a parser
needs no workflow edit. Locally:

    snapshotter derive --parsers parsers.example_v1
"""

import json

from snapshotter import derive

PARSER_VERSION = "1"


def parse(body: bytes, ctx: derive.ParseContext):
    data = json.loads(body)
    for item in data["items"]:
        yield derive.Observation(
            entity_id=item["id"],
            metric="count",
            value=int(item["count"]),
            unit="count",
            # Leave observed_at unset and derive stamps each row with the
            # fetch time of the manifest row it came from. Set it only when
            # the payload itself carries the observation time:
            #   observed_at=item["as_of"],
        )


derive.register("example.v1", parse, PARSER_VERSION)

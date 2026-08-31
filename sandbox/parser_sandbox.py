"""Parser for schema sandbox.v1 — the synthetic listing shape."""

import json

from wss import derive

PARSER_VERSION = "1"


def parse(body: bytes, ctx: derive.ParseContext):
    for item in json.loads(body):
        yield derive.Observation(entity_id=item["id"], metric="downloads_30d", value=item["downloads"], unit="count")
        yield derive.Observation(entity_id=item["id"], metric="likes", value=item["likes"], unit="count")


derive.register("sandbox.v1", parse, PARSER_VERSION)

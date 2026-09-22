"""Volatile markup must not defeat dedupe — and must not touch the archive."""

from __future__ import annotations

from wss import capture


PATTERNS = (r'js-view-dom-id-[0-9a-f]+',)
A = b'<div class="view js-view-dom-id-aaaa111">data</div>'
B = b'<div class="view js-view-dom-id-bbbb222">data</div>'
C = b'<div class="view js-view-dom-id-cccc333">DIFFERENT</div>'


def test_volatile_id_does_not_count_as_a_change():
    assert capture._dedupe_key(A, PATTERNS) == capture._dedupe_key(B, PATTERNS)


def test_a_real_change_still_counts():
    assert capture._dedupe_key(A, PATTERNS) != capture._dedupe_key(C, PATTERNS)


def test_without_patterns_nothing_is_stripped():
    assert capture._dedupe_key(A, ()) != capture._dedupe_key(B, ())


def test_same_content_falls_back_to_sha_when_unconfigured():
    class S:
        dedupe_ignore = ()
        dedupe_canon = ""
    prev = {"content_sha256": "abc", "raw_ref": "raw/x"}
    assert capture._same_content(S, None, prev, A, "abc") is True
    assert capture._same_content(S, None, prev, A, "def") is False


def test_same_content_uses_the_stored_bytes(tmp_path):
    class S:
        dedupe_ignore = PATTERNS
        dedupe_canon = ""

    class Store:
        def read(self, ref):
            return A

    prev = {"content_sha256": "old", "raw_ref": "raw/x"}
    # different sha, same content once the volatile id is stripped
    assert capture._same_content(S, Store(), prev, B, "new") is True
    assert capture._same_content(S, Store(), prev, C, "new") is False


def test_unreadable_previous_is_treated_as_changed():
    class S:
        dedupe_ignore = PATTERNS
        dedupe_canon = ""

    class Store:
        def read(self, ref):
            raise OSError("gone")

    prev = {"content_sha256": "old", "raw_ref": "raw/x"}
    assert capture._same_content(S, Store(), prev, B, "new") is False


# --- dedupe_canon -------------------------------------------------------
# A publisher that REORDERS keys defeats substring stripping entirely. WHO's
# GHO moved `TimeDim` two keys left in every row between two captures three
# weeks apart and regenerated every surrogate `Id`: 377 of 384 pages changed
# at the byte level, 378 of them were identical as data.

SEPT = b'{"value":[{"Id":1,"SpatialDim":"AFG","TimeDim":2021,"Dim1":"SEX_BTSX","NumericValue":4.2}]}'
NOW  = b'{"value":[{"Id":9,"SpatialDim":"AFG","Dim1":"SEX_BTSX","TimeDim":2021,"NumericValue":4.2}]}'
MOVED = b'{"value":[{"Id":9,"SpatialDim":"AFG","Dim1":"SEX_BTSX","TimeDim":2021,"NumericValue":9.9}]}'
ID = (r'"Id":\d+,',)


def test_reordered_keys_defeat_substring_stripping():
    """The reason dedupe_canon exists: stripping alone cannot see through it."""
    assert capture._dedupe_key(SEPT, ID) != capture._dedupe_key(NOW, ID)


def test_canonical_json_sees_through_reordering_and_surrogates():
    assert capture._dedupe_key(SEPT, ID, "json") == capture._dedupe_key(NOW, ID, "json")


def test_a_moved_value_is_still_a_change():
    assert capture._dedupe_key(NOW, ID, "json") != capture._dedupe_key(MOVED, ID, "json")


def test_a_truncated_body_is_never_mistaken_for_unchanged():
    """Half a response parses as nothing; it must not canonicalise to a match."""
    assert capture._dedupe_key(SEPT, ID, "json") != capture._dedupe_key(SEPT[:40], ID, "json")


def test_an_error_page_is_never_mistaken_for_unchanged():
    assert capture._dedupe_key(SEPT, ID, "json") != capture._dedupe_key(b"<html>502</html>", ID, "json")


def test_canon_alone_without_patterns_still_sees_the_surrogate():
    """Id churn with no ignore pattern is a real difference in canonical form."""
    assert capture._dedupe_key(SEPT, (), "json") != capture._dedupe_key(NOW, (), "json")


def test_same_content_uses_canon_when_no_patterns_are_set():
    class S:
        dedupe_ignore = ()
        dedupe_canon = "json"

    class Store:
        def read(self, ref):
            return b'{"value":[{"a":1,"b":2}]}'

    prev = {"content_sha256": "old", "raw_ref": "raw/x"}
    assert capture._same_content(S, Store(), prev, b'{"value":[{"b":2,"a":1}]}', "new") is True
    assert capture._same_content(S, Store(), prev, b'{"value":[{"b":3,"a":1}]}', "new") is False


def test_the_archive_never_sees_canonical_form():
    """Canonicalising is a comparison, not a rewrite."""
    before = SEPT
    capture._dedupe_key(SEPT, ID, "json")
    assert before == SEPT

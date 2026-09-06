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
    prev = {"content_sha256": "abc", "raw_ref": "raw/x"}
    assert capture._same_content(S, None, prev, A, "abc") is True
    assert capture._same_content(S, None, prev, A, "def") is False


def test_same_content_uses_the_stored_bytes(tmp_path):
    class S:
        dedupe_ignore = PATTERNS

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

    class Store:
        def read(self, ref):
            raise OSError("gone")

    prev = {"content_sha256": "old", "raw_ref": "raw/x"}
    assert capture._same_content(S, Store(), prev, B, "new") is False

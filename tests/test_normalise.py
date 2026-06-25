"""Normalisation + track_pk determinism (RC1 §S12)."""

from app.ingestion.normalise import normalise_title, compute_track_pk, normalise_token
from tests.conftest import make_token


def test_strips_version_noise():
    assert "original mix" not in normalise_title("Strobe (Original Mix)").lower()
    assert "radio edit" not in normalise_title("Sandstorm (Radio Edit)").lower()
    assert "official audio" not in normalise_title("Track (Official Audio)").lower()


def test_strips_catalogue_prefix():
    assert normalise_title("DFTD001 - Real Title").lower().strip() == "real title"


def test_preserves_remix_credit():
    out = normalise_title("Avalon (Surgeon Remix)").lower()
    assert "surgeon" in out and "remix" in out


def test_pk_is_deterministic():
    a = make_token(title="Foo", artist="Bar", duration_ms=123456)
    b = make_token(title="Foo", artist="Bar", duration_ms=123456)
    assert a.track_pk == b.track_pk
    assert a.track_pk.startswith("synthetic:")


def test_isrc_pk_vs_synthetic():
    with_isrc = make_token(isrc="gbabc1234567")
    without = make_token(isrc=None)
    assert with_isrc.track_pk == "isrc:GBABC1234567"
    assert without.track_pk.startswith("synthetic:")


def test_duration_changes_synthetic_pk():
    a = make_token(duration_ms=200000)
    b = make_token(duration_ms=205000)
    assert a.track_pk != b.track_pk

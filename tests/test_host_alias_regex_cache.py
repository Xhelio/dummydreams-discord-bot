import db


def test_host_alias_pattern_is_cached_and_matches_possessive_suffix(monkeypatch):
    db._compile_host_alias_pattern.cache_clear()
    monkeypatch.setattr(
        db,
        "get_host_aliases",
        lambda *, block=True: [("luna", "Luna")],
    )

    assert db.detect_host_match_from_welcome_message(
        "Welcome to Luna's live!", block=False
    ) == ("Luna", "luna")
    first = db._compile_host_alias_pattern.cache_info()

    assert db.detect_host_match_from_welcome_message(
        "Welcome to Luna’s live!", block=False
    ) == ("Luna", "luna")
    second = db._compile_host_alias_pattern.cache_info()

    assert first.misses == 1
    assert second.misses == 1
    assert second.hits == 1


def test_host_alias_pattern_keeps_token_boundaries(monkeypatch):
    monkeypatch.setattr(
        db,
        "get_host_aliases",
        lambda *, block=True: [("luna", "Luna")],
    )

    assert db.detect_host_match_from_welcome_message(
        "superluna is here", block=False
    ) == (None, None)

from types import SimpleNamespace

import observer


def test_offline_and_unavailable_checks_do_not_increase_failure_streak():
    assert observer._next_consecutive_failures(61, is_offline_error=True) == 0
    assert observer._next_consecutive_failures(12, is_unavailable=True) == 0


def test_unexpected_observer_error_increases_failure_streak():
    assert observer._next_consecutive_failures(4) == 5


def test_text_from_value_normalizes_strings_bytes_and_nested_dicts():
    assert observer._text_from_value("  Hello live  ") == "Hello live"
    assert observer._text_from_value(b"  Halo  ") == "Halo"
    assert observer._text_from_value({"content": "  Intro text  "}) == "Intro text"
    assert observer._text_from_value({"default_pattern": "Pattern"}) == "Pattern"
    assert observer._text_from_value(None) == ""


def test_text_from_value_reads_text_proto_like_attributes():
    value = SimpleNamespace(default_pattern="  Proto intro  ")

    assert observer._text_from_value(value) == "Proto intro"


def test_collect_room_info_uses_only_intro_named_fields():
    payload = {
        "title": "This must not be used",
        "room": {
            "description": "Also ignored",
            "live_intro": {"content": "Welcome to the live"},
            "nested": [{"intro_text": "Second intro"}],
        },
    }

    result = observer._collect_intro_texts_from_room_info(payload)

    assert result == [
        ("room.live_intro", "Welcome to the live"),
        ("room.nested[0].intro_text", "Second intro"),
    ]


def test_collect_room_info_respects_depth_and_result_limits():
    payload = {
        "intro_one": "one",
        "intro_two": "two",
        "intro_three": "three",
        "intro_four": "four",
        "intro_five": "five",
        "intro_six": "six",
    }

    result = observer._collect_intro_texts_from_room_info(payload)

    assert len(result) == 5
    assert [item[1] for item in result] == ["one", "two", "three", "four", "five"]


def test_extract_live_intro_text_reads_only_intro_event_fields():
    event = SimpleNamespace(
        content="Event intro",
        description="Description candidate",
        room_title="Must not be selected",
    )

    text, field_name, candidates = observer._extract_live_intro_text(event)

    assert text == "Event intro"
    assert field_name == "content"
    assert candidates[0] == ("content", "Event intro")
    assert all("Must not be selected" not in item[1] for item in candidates)


def test_extract_live_intro_text_supports_intro_named_dict_fields():
    event = {
        "room_intro": {"text": "Dictionary intro"},
        "title": "Unrelated title",
    }

    text, field_name, candidates = observer._extract_live_intro_text(event)

    assert text == "Dictionary intro"
    assert field_name == "room_intro"
    assert candidates == [("room_intro", "Dictionary intro")]


def test_extract_live_intro_text_returns_empty_when_no_intro_exists():
    event = SimpleNamespace(content="", title="Ordinary room title")

    assert observer._extract_live_intro_text(event) == ("", "", [])


def test_safe_preview_truncates_and_handles_unprintable_values():
    assert observer._safe_preview("line one\nline two") == "line one line two"
    assert observer._safe_preview("x" * 200, limit=20) == ("x" * 17) + "..."

def test_extract_comment_username_prefers_unique_id_and_normalizes():
    event = SimpleNamespace(user=SimpleNamespace(unique_id="@Creator", display_id="creator_display"))
    assert observer._extract_comment_username(event) == "creator"


def test_extract_comment_username_falls_back_to_display_id():
    event = SimpleNamespace(user=SimpleNamespace(display_id="@DisplayName"))
    assert observer._extract_comment_username(event) == "displayname"


def test_extract_comment_username_supports_dict_payloads():
    assert observer._extract_comment_username({"user": {"display_id": "@Viewer"}}) == "viewer"


def test_extract_comment_username_returns_empty_for_missing_user_id():
    assert observer._extract_comment_username(SimpleNamespace(user=SimpleNamespace())) == ""
    assert observer._extract_comment_username(SimpleNamespace(user=None)) == ""


def test_exception_chain_types_reports_causes_without_messages():
    try:
        try:
            raise ValueError("sensitive response body")
        except ValueError as cause:
            raise RuntimeError("outer failure") from cause
    except RuntimeError as error:
        assert observer._exception_chain_types(error) == "RuntimeError -> ValueError"
        assert "sensitive response body" not in observer._exception_chain_types(error)

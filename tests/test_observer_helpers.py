from types import SimpleNamespace

import observer


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

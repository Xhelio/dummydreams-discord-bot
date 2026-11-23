from pathlib import Path
import re


def test_render_blueprint_keeps_error_channels_isolated():
    blueprint = (Path(__file__).resolve().parents[1] / "render.yaml").read_text(
        encoding="utf-8"
    )
    services = re.split(r"(?m)^  - type: web\s*$", blueprint)
    service_by_name = {}
    for service in services:
        name_match = re.search(r"(?m)^    name: (.+)$", service)
        channel_match = re.search(
            r'(?m)^      - key: LOG_ERROR_CHANNEL_ID\s*\n        value: "?(\d+)"?$',
            service,
        )
        if name_match:
            service_by_name[name_match.group(1)] = (
                channel_match.group(1) if channel_match else None
            )

    assert service_by_name["dummydreams-discord-bot"] == "1572123726839947305"
    assert service_by_name["dummydreams-test"] == "1571473445714133075"

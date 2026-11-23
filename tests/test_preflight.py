import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = REPO_ROOT / "scripts" / "preflight.py"


def _run_preflight(bot_env):
    env = {
        "BOT_ENV": bot_env,
        "DISCORD_TOKEN": "test-token",
    }
    return subprocess.run(
        [sys.executable, str(PREFLIGHT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_stable_preflight_fails_without_remote_database_configuration():
    result = _run_preflight("stable")

    assert result.returncode == 1
    assert "stable deployment uses Turso, not local SQLite" in result.stdout


def test_test_preflight_allows_local_sqlite_fallback():
    result = _run_preflight("test")

    assert result.returncode == 0
    assert "TURSO_DATABASE_URL not set -> local SQLite fallback" in result.stdout



def test_preflight_adds_repository_root_for_application_imports():
    code = (
        "import runpy, sys; "
        "sys.path[0] = 'scripts'; "
        "runpy.run_path('scripts/preflight.py'); "
        "import db"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env={"BOT_ENV": "test"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_render_services_run_preflight_before_starting_bot():
    render_config = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")

    assert render_config.count(
        "startCommand: python scripts/preflight.py && python main.py"
    ) == 2
    assert "    branch: staging" in render_config
    assert "    branch: test" not in render_config
    assert "name: dummydreams-discord-bot" in render_config
    assert "name: dummydreams-stable" not in render_config
    assert render_config.count("    plan: free") == 2

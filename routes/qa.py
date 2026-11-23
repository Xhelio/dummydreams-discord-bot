"""Staging-only QA dashboard and safe simulator launcher."""

import json
import os
import subprocess
import sys
from pathlib import Path

from flask import Blueprint, abort, jsonify, render_template_string, request

qa_bp = Blueprint("staging_qa", __name__, url_prefix="/admin/qa")
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Only fixed, reviewed commands are executable from the dashboard. Never accept
# a user-supplied path, shell fragment, module name, or arbitrary CLI argument.
SCENARIOS = {
    "gift-all": ("scripts/qa_simulator.py", "all"),
    "gift-success": ("scripts/qa_simulator.py", "gift"),
    "gift-rollback": ("scripts/qa_simulator.py", "rollback"),
    "tiktok-all": ("scripts/tiktok_response_simulator.py", "all"),
    "tiktok-live": ("scripts/tiktok_response_simulator.py", "live"),
    "tiktok-offline": ("scripts/tiktok_response_simulator.py", "offline"),
    "tiktok-empty": ("scripts/tiktok_response_simulator.py", "empty"),
    "tiktok-malformed": ("scripts/tiktok_response_simulator.py", "malformed"),
    "tiktok-invalid-json": ("scripts/tiktok_response_simulator.py", "invalid-json"),
    "tiktok-failed-parse": ("scripts/tiktok_response_simulator.py", "failed-parse"),
    "tiktok-timeout": ("scripts/tiktok_response_simulator.py", "timeout"),
    "tiktok-http-500": ("scripts/tiktok_response_simulator.py", "http-500"),
}

DASHBOARD_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="csrf-token" content="{{ csrf_token() }}">
  <title>Staging QA Console</title>
  <style>
    body{font:15px system-ui,sans-serif;background:#101827;color:#e5edf7;margin:0;padding:24px}
    main{max-width:900px;margin:auto}
    h1{font-size:1.5rem}.muted{color:#9eafc4}
    section{background:#192538;border:1px solid #30435b;border-radius:12px;padding:18px;margin:16px 0}
    button,select{font:inherit;border-radius:8px;padding:10px 12px}
    button{background:#2563eb;color:white;border:0;cursor:pointer}
    button:disabled{opacity:.55;cursor:wait}
    select{background:#101827;color:#e5edf7;border:1px solid #52647a;max-width:100%;width:100%;margin:10px 0}
    pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#0b1220;padding:14px;border-radius:8px;min-height:80px}
    .row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
    .pill{font-size:.8rem;border:1px solid #52647a;border-radius:99px;padding:3px 9px}
  </style>
</head>
<body>
<main>
  <h1>Staging QA Console</h1>
  <p class="muted">Runs fixed offline test scenarios in isolated child processes. No TikTok, Discord, or live database requests are made.</p>
  <section>
    <h2>Gift transaction tests</h2>
    <p class="muted">Uses a temporary SQLite database to verify gift writes, deduplication, and rollback.</p>
    <label for="gift-scenario">Scenario</label>
    <select id="gift-scenario">
      <option value="gift-all">All gift tests</option>
      <option value="gift-success">Successful write + duplicate delivery</option>
      <option value="gift-rollback">Forced failure + rollback</option>
    </select>
    <button data-select="gift-scenario">Run gift test</button>
  </section>
  <section>
    <h2>TikTok response tests</h2>
    <p class="muted">Exercises the real room-probe classifier with an in-memory fake transport.</p>
    <label for="tiktok-scenario">Scenario</label>
    <select id="tiktok-scenario">
      <option value="tiktok-all">All TikTok response tests</option>
      <option value="tiktok-live">LIVE response</option>
      <option value="tiktok-offline">Offline response</option>
      <option value="tiktok-empty">Empty response</option>
      <option value="tiktok-malformed">Malformed JSON envelope</option>
      <option value="tiktok-invalid-json">Invalid JSON response</option>
      <option value="tiktok-failed-parse">FailedParseRoomIdError</option>
      <option value="tiktok-timeout">Timeout</option>
      <option value="tiktok-http-500">HTTP 500</option>
    </select>
    <button data-select="tiktok-scenario">Run TikTok test</button>
  </section>
  <section>
    <div class="row"><h2 style="margin:0">Execution result</h2><span class="pill" id="status">Idle</span></div>
    <pre id="output">Choose a test scenario to begin.</pre>
  </section>
</main>
<script>
(() => {
  const output = document.getElementById("output");
  const status = document.getElementById("status");
  document.querySelectorAll("button[data-select]").forEach(button => {
    button.addEventListener("click", async () => {
      const select = document.getElementById(button.dataset.select);
      button.disabled = true;
      status.textContent = "Running";
      output.textContent = "Starting isolated test process…";
      try {
        const response = await fetch("/admin/qa/run", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content,
            "Sec-Fetch-Site": "same-origin"
          },
          body: JSON.stringify({scenario: select.value})
        });
        const data = await response.json();
        output.textContent = data.output || data.error || JSON.stringify(data, null, 2);
        status.textContent = data.passed ? "PASS" : "FAILED";
      } catch (error) {
        output.textContent = String(error);
        status.textContent = "ERROR";
      } finally {
        button.disabled = false;
      }
    });
  });
})();
</script>
</body>
</html>
"""


def _dashboard_enabled() -> bool:
    environment = (os.environ.get("BOT_ENV") or "").strip().lower()
    enabled = (os.environ.get("STAGING_QA_DASHBOARD_ENABLED") or "").strip()
    return environment == "test" and enabled == "1"


def _clean_subprocess_environment() -> dict:
    """Pass no app secrets or production/staging credentials to test children."""
    clean = {
        "BOT_ENV": "test",
        "PYTHONPATH": str(PROJECT_ROOT),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if os.environ.get("PATH"):
        clean["PATH"] = os.environ["PATH"]
    return clean


@qa_bp.get("")
def dashboard():
    if not _dashboard_enabled():
        abort(404)
    return render_template_string(DASHBOARD_HTML)


@qa_bp.post("/run")
def run_scenario():
    if not _dashboard_enabled():
        abort(404)

    payload = request.get_json(silent=True) or {}
    scenario_name = payload.get("scenario")
    command = SCENARIOS.get(scenario_name)
    if command is None:
        return jsonify({"ok": False, "passed": False, "error": "Unknown QA scenario"}), 400

    script, scenario = command
    script_path = (PROJECT_ROOT / script).resolve()
    if PROJECT_ROOT not in script_path.parents or not script_path.is_file():
        return jsonify({"ok": False, "passed": False, "error": "QA runner unavailable"}), 503

    argv = [
        sys.executable,
        str(script_path),
        "--scenario",
        scenario,
    ]
    try:
        completed = subprocess.run(
            argv,
            cwd=str(PROJECT_ROOT),
            env=_clean_subprocess_environment(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return jsonify({
            "ok": False,
            "passed": False,
            "error": "QA runner exceeded the 30-second limit",
        }), 504
    except OSError:
        return jsonify({
            "ok": False,
            "passed": False,
            "error": "Unable to start QA runner",
        }), 503

    output = (completed.stdout or "").strip()
    if completed.stderr:
        output += "\n\n[stderr]\n" + completed.stderr[-3000:]
    return jsonify({
        "ok": completed.returncode == 0,
        "passed": completed.returncode == 0,
        "scenario": scenario_name,
        "exit_code": completed.returncode,
        "output": output[-12000:],
    }), 200 if completed.returncode == 0 else 500

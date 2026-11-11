"""Admin panel for bot toggles, channels, hosts, aliases and role tiers."""

import json
import os
import re
from datetime import datetime
from urllib.parse import quote_plus
import pytz
from flask import Blueprint, render_template_string, request, redirect

def _url_q(text: str) -> str:
    return quote_plus(str(text or ""))

from db import (
    get_setting,
    set_setting,
    get_bool_setting,
    get_json_list_setting,
    get_announcement_channel_ids,
    get_all_hosts,
    get_host_aliases,
    add_host_alias,
    delete_host_alias,
    get_db_connection,
    add_role_tier,
    delete_role_tier,
    get_role_tiers,
    get_tier_settings,
    get_channel_display_name,
    set_host_priority,
    log_activity_event,
)
from verification import fetch_tiktok_display_name
from styles import BASE_CSS, NAV_HTML, AUTO_REFRESH_JS

settings_bp = Blueprint("settings", __name__, url_prefix="/admin")

_verification_refresh_handler = None
_presence_refresh_handler = None


def set_verification_refresh_handler(handler):
    global _verification_refresh_handler
    _verification_refresh_handler = handler


def set_presence_refresh_handler(handler):
    global _presence_refresh_handler
    _presence_refresh_handler = handler


def _parse_id_list(raw: str) -> list:
    values = []
    for part in re.split(r"[\s,]+", raw or ""):
        part = part.strip()
        if part.isdigit():
            values.append(int(part))
    return list(dict.fromkeys(values))


def _checkbox(name: str) -> str:
    return "1" if request.form.get(name) else "0"


@settings_bp.route("/settings", methods=["GET"])
def view_settings():
    master = get_bool_setting("master_enabled", True)
    debug_mode = get_bool_setting("debug_mode", False)
    feature_keys = [
        ("feature_announcements", "📣 Live Announcements", "Send and remove live notifications in the announcement channels."),
        ("feature_verification", "🔐 Verification Bot", "Allow members to verify their TikTok account."),
        ("feature_role_tiers", "🏅 Role Tiers", "Grant and update coin tier roles."),
        ("feature_leaderboard", "🏆 Leaderboard Updater", "Post and periodically update the leaderboard message."),
        ("feature_live_tracking", "🎁 Gift & Live Tracking", "Record gifts and live session activity."),
    ]
    features = [
        {
            "key": key,
            "label": label,
            "help": help_text,
            "stored": get_bool_setting(key, True),
            "effective": master and get_bool_setting(key, True),
        }
        for key, label, help_text in feature_keys
    ]

    setting_values = {
        "verified_role_id": get_setting("verified_role_id", "") or "",
        "admin_role_id": get_setting("admin_role_id", "") or "",
        "verification_channel_id": get_setting("verification_channel_id", "") or "",
        "announcement_channel_ids": "\n".join(str(x) for x in get_announcement_channel_ids()),
        "leaderboard_channel_id": get_setting("leaderboard_channel_id", "") or "",
        "allowed_channel_ids": "\n".join(str(x) for x in get_json_list_setting("allowed_channel_ids")),
        "allowed_channels_enabled": get_bool_setting("allowed_channels_enabled", False),
    }
    tier_cfg = get_tier_settings()
    tier_cfg["announce_channel_id"] = get_setting("role_tier_announce_channel_id", "") or ""
    tier_cfg["last_reset"] = get_setting("role_tier_last_reset", "") or "-"

    hosts = get_all_hosts()
    aliases = get_host_aliases()
    tiers_reset = get_role_tiers("reset")
    tiers_permanent = get_role_tiers("permanent")
    saved = request.args.get("saved") == "1"
    flash_msg = request.args.get("msg", "").strip()
    signer_configured = bool((os.environ.get("SIGN_API_KEY") or os.environ.get("TIKTOK_SIGN_API_KEY") or "").strip())
    flash_type = request.args.get("msg_type", "success").strip() or "success"

    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Bot Settings</title>
        <style>BASE_CSS_PLACEHOLDER</style>
    </head>
    <body>
        <div class="header">
            <h1>⚙️ Bot Settings</h1>
        </div>
        NAV_HTML_PLACEHOLDER

        {% if flash_msg %}
        <div class="notice-{{ flash_type if flash_type in ['success','info','warning','danger'] else 'success' }}">{{ flash_msg }}</div>
        {% elif saved %}
        <div class="notice-success">✅ Settings saved. Changes are applied by the bot immediately.</div>
        {% endif %}

        <form action="/admin/settings/save" method="POST">
            <div class="card">
                <h2>🎛️ Feature Controls</h2>
                <div class="switch-row master-row">
                    <label class="switch-label" for="master_enabled">
                        <input type="checkbox" id="master_enabled" name="master_enabled" value="1" {% if master %}checked{% endif %}>
                        <span>Master Discord Bot</span>
                    </label>
                    <span class="switch-help">Turn the whole Discord bot on or off. Turning it off remembers every feature state below.</span>
                </div>
                <div class="switch-row">
                    <label class="switch-label" for="debug_mode">
                        <input type="checkbox" id="debug_mode" name="debug_mode" value="1" {% if debug_mode %}checked{% endif %}>
                        <span>🧪 Debug Mode</span>
                    </label>
                    <span class="switch-help">Show the current push/version code in the bot presence (admin toggle).</span>
                </div>
                <div id="feature-list">
                    {% for f in features %}
                    <div class="switch-row feature-row">
                        <label class="switch-label">
                            <input type="checkbox" name="{{ f.key }}" value="1"
                                   data-stored="{{ '1' if f.stored else '0' }}"
                                   {% if f.effective %}checked{% endif %}>
                            <span>{{ f.label }}</span>
                        </label>
                        <span class="switch-help">{{ f.help }}</span>
                    </div>
                    {% endfor %}
                </div>
            </div>

            <div class="card">
                <h2>🆔 Discord Channels &amp; Roles</h2>
                <div class="form-grid two-col">
                    <div>
                        <label class="field-label">Verified Role ID</label>
                        <input type="text" name="verified_role_id" value="{{ s.verified_role_id }}" placeholder="Role granted after verification" inputmode="numeric">
                    </div>
                    <div>
                        <label class="field-label">Admin Role ID</label>
                        <input type="text" name="admin_role_id" value="{{ s.admin_role_id }}" placeholder="Only this role can use admin commands" inputmode="numeric">
                    </div>
                    <div>
                        <label class="field-label">Verification Channel ID</label>
                        <input type="text" name="verification_channel_id" value="{{ s.verification_channel_id }}" placeholder="Verification panel channel" inputmode="numeric">
                        <small class="field-help">When this ID changes, the old panel message is deleted and a new one is posted.</small>
                    </div>
                    <div>
                        <label class="field-label">Leaderboard Channel ID</label>
                        <input type="text" name="leaderboard_channel_id" value="{{ s.leaderboard_channel_id }}" placeholder="Leaderboard message channel" inputmode="numeric">
                    </div>
                    <div class="full-col">
                        <label class="field-label">Announcement Channel IDs</label>
                        <textarea name="announcement_channel_ids" rows="2" placeholder="One channel ID per line">{{ s.announcement_channel_ids }}</textarea>
                        <small class="field-help">One live message per target account in every channel listed. Removed channels are cleaned up automatically.</small>
                    </div>
                    <div class="full-col">
                        <label class="field-label">Allowed Bot Channel IDs</label>
                        <textarea name="allowed_channel_ids" rows="2" placeholder="One channel ID per line">{{ s.allowed_channel_ids }}</textarea>
                        <label class="inline-check">
                            <input type="checkbox" name="allowed_channels_enabled" value="1" {% if s.allowed_channels_enabled %}checked{% endif %}>
                            Restrict commands to these channels (off = every channel is allowed)
                        </label>
                    </div>
                </div>
            </div>

            <div class="card">
                <h2>🏅 Role Tier Settings</h2>
                <div class="form-grid two-col">
                    <div>
                        <label class="field-label">Tier Mode</label>
                        <select name="role_tiers_mode">
                            <option value="replace" {% if tier_cfg.mode == 'replace' %}selected{% endif %}>Replace (only the highest tier)</option>
                            <option value="stacked" {% if tier_cfg.mode == 'stacked' %}selected{% endif %}>Stacked (keep every earned tier)</option>
                        </select>
                    </div>
                    <div>
                        <label class="field-label">Tier-up Announcement Channel ID</label>
                        <input type="text" name="role_tier_announce_channel_id" value="{{ tier_cfg.announce_channel_id }}" placeholder="Tier up announcement channel" inputmode="numeric">
                    </div>
                    <div class="full-col">
                        <label class="inline-check">
                            <input type="checkbox" name="role_tiers_enabled" value="1" {% if tier_cfg.enabled %}checked{% endif %}>
                            Enable role tiers (reset group)
                        </label>
                        <label class="inline-check">
                            <input type="checkbox" name="role_tier_permanent_enabled" value="1" {% if tier_cfg.permanent_enabled %}checked{% endif %}>
                            Enable "all of the time" tiers (never reset)
                        </label>
                        <label class="inline-check">
                            <input type="checkbox" name="role_tier_reset_enabled" value="1" {% if tier_cfg.reset_enabled %}checked{% endif %}>
                            Enable scheduled reset for the reset group
                        </label>
                        <label class="inline-check">
                            <input type="checkbox" name="role_tier_announce" value="1" {% if tier_cfg.announce %}checked{% endif %}>
                            Announce tier upgrades
                        </label>
                    </div>
                    <div>
                        <label class="field-label">Reset Every</label>
                        <div class="inline-fields">
                            <input type="number" name="role_tier_reset_interval" min="1" value="{{ tier_cfg.reset_interval }}">
                            <select name="role_tier_reset_unit">
                                <option value="weeks" {% if tier_cfg.reset_unit == 'weeks' %}selected{% endif %}>Weeks</option>
                                <option value="months" {% if tier_cfg.reset_unit == 'months' %}selected{% endif %}>Months</option>
                            </select>
                        </div>
                        <small class="field-help">Last reset: {{ tier_cfg.last_reset }}</small>
                    </div>
                </div>
            </div>

            <button type="submit" class="btn-save">💾 Save All Settings</button>
        </form>

        <div class="card">
            <h2>🏅 Reset Tier List</h2>
            <p class="muted">Roles in this list are granted by coin amount and revoked by the scheduled reset.</p>
            <form action="/admin/settings/tier/add" method="POST" class="form-grid tier-form">
                <input type="hidden" name="group_key" value="reset">
                <input type="text" name="role_id" placeholder="Role ID" required inputmode="numeric">
                <input type="number" name="min_coins" placeholder="Minimum coins" min="0" required>
                <button type="submit">+ Add Reset Tier</button>
            </form>
            <div class="table-scroll">
                <table>
                    <tr><th>Tier</th><th>Role ID</th><th>Minimum Coins</th><th>Action</th></tr>
                    {% for t in tiers_reset %}
                    <tr>
                        <td><strong>{{ t.tier_name }}</strong></td>
                        <td><code>{{ t.role_id }}</code></td>
                        <td><span class="badge" style="background:#ca8a04;">{{ t.min_coins }} c</span></td>
                        <td>
                            <form action="/admin/settings/tier/delete/{{ t.id }}" method="POST" class="inline-form" onsubmit="return confirm('Delete {{ t.tier_name }}?');">
                                <button type="submit" class="btn-action btn-del">Delete</button>
                            </form>
                        </td>
                    </tr>
                    {% else %}
                    <tr><td colspan="4" class="muted">No reset tiers yet.</td></tr>
                    {% endfor %}
                </table>
            </div>
        </div>

        <div class="card">
            <h2>♾️ All-the-Time Tier List</h2>
            <p class="muted">These roles use the same coin rules but are never removed by a reset.</p>
            <form action="/admin/settings/tier/add" method="POST" class="form-grid tier-form">
                <input type="hidden" name="group_key" value="permanent">
                <input type="text" name="role_id" placeholder="Role ID" required inputmode="numeric">
                <input type="number" name="min_coins" placeholder="Minimum coins" min="0" required>
                <button type="submit">+ Add All-the-Time Tier</button>
            </form>
            <div class="table-scroll">
                <table>
                    <tr><th>Tier</th><th>Role ID</th><th>Minimum Coins</th><th>Action</th></tr>
                    {% for t in tiers_permanent %}
                    <tr>
                        <td><strong>{{ t.tier_name }}</strong></td>
                        <td><code>{{ t.role_id }}</code></td>
                        <td><span class="badge" style="background:#15803d;">{{ t.min_coins }} c</span></td>
                        <td>
                            <form action="/admin/settings/tier/delete/{{ t.id }}" method="POST" class="inline-form" onsubmit="return confirm('Delete {{ t.tier_name }}?');">
                                <button type="submit" class="btn-action btn-del">Delete</button>
                            </form>
                        </td>
                    </tr>
                    {% else %}
                    <tr><td colspan="4" class="muted">No all-the-time tiers yet.</td></tr>
                    {% endfor %}
                </table>
            </div>
        </div>

        <div class="card">
            <h2>🎯 Target Hosts & Prioritas Koneksi</h2>
            <div class="{{ 'notice-success' if signer_configured else 'notice-warning' }}">
                {% if signer_configured %}
                ✅ EulerStream sign API key terdeteksi (<code>SIGN_API_KEY</code>). Rate limit per akun harian lebih tinggi.
                {% else %}
                ⚠️ <strong>Belum ada EulerStream API key.</strong> Free tier membatasi koneksi per akun per hari
                (<code>rate_limit_account_day</code>) sehingga websocket bisa gagal connect dan host tidak terdeteksi.
                Set environment variable <code>SIGN_API_KEY</code> di Render (key dari
                <a href="https://www.eulerstream.com/pricing" target="_blank" rel="noopener">eulerstream.com/pricing</a>),
                lalu redeploy. Bot otomatis memakainya.
                {% endif %}
            </div>
            <p class="muted">Pilih akun prioritas untuk koneksi/deteksi rate normal. Akun non-prioritas akan mengikuti mode di bawah (lambat/non-aktif) untuk mengurangi beban/rate limit.</p>
            <form action="/admin/settings/host-priority-mode" method="POST" class="form-grid" style="grid-template-columns: 1fr 1fr auto; margin-bottom: 14px; align-items:end;">
                <div>
                    <label for="non_priority_mode" class="muted" style="display:block; margin-bottom:4px;">Mode akun non-prioritas</label>
                    <select id="non_priority_mode" name="non_priority_mode">
                        <option value="slow" {% if non_priority_mode == 'slow' %}selected{% endif %}>Lambat (slow rate)</option>
                        <option value="off" {% if non_priority_mode == 'off' %}selected{% endif %}>Non-aktif (tidak di-detect/connect)</option>
                    </select>
                </div>
                <div>
                    <label for="non_priority_slow_seconds" class="muted" style="display:block; margin-bottom:4px;">Interval lambat (detik)</label>
                    <input id="non_priority_slow_seconds" type="number" name="non_priority_slow_seconds" min="60" step="10" value="{{ non_priority_slow_seconds }}">
                </div>
                <button type="submit">Simpan Mode Prioritas</button>
            </form>
            <form action="/admin/settings/host/add" method="POST" class="form-grid" style="grid-template-columns: 1fr auto; margin-bottom: 12px;">
                <input type="text" name="tiktok_username" placeholder="TikTok username (@...) — channel name is fetched automatically" required>
                <button type="submit">+ Add Host</button>
            </form>
            <form action="/admin/settings/hosts/save" method="POST">
                <div class="table-scroll">
                    <table>
                        <tr><th>ID</th><th>Username</th><th>Channel Name</th><th style="text-align:center;">Active</th><th style="text-align:center;">Prioritas</th><th>Delete</th></tr>
                        {% for h in hosts %}
                        <tr>
                            <td>{{ h[0] }}</td>
                            <td>@{{ h[1] }}</td>
                            <td>{{ h[2] }}</td>
                            <td style="text-align:center;">
                                <input type="checkbox" name="active_{{ h[0] }}" value="1" {% if h[3] %}checked{% endif %}>
                            </td>
                            <td style="text-align:center;">
                                <input type="checkbox" name="priority_{{ h[0] }}" value="1" {% if h[4] %}checked{% endif %}>
                            </td>
                            <td>
                                <label style="display:inline-flex; align-items:center; gap:6px; color:#fca5a5; font-size:0.85rem;">
                                    <input type="checkbox" name="delete_{{ h[0] }}" value="1"> Hapus
                                </label>
                            </td>
                        </tr>
                        {% else %}
                        <tr><td colspan="6" class="muted">No hosts yet.</td></tr>
                        {% endfor %}
                    </table>
                </div>
                <div style="margin-top:12px; display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
                    <button type="submit">Simpan Perubahan Host (Active/Prioritas/Hapus)</button>
                    <span class="muted">Ubah banyak host sekaligus lalu klik simpan sekali (tanpa toggle satu-satu).</span>
                </div>
            </form>
            <p class="muted" style="margin-top:8px;">Akun prioritas: connect/deteksi rate normal. Non-prioritas: <strong>{{ 'lambat (' ~ non_priority_slow_seconds ~ 's)' if non_priority_mode == 'slow' else 'non-aktif' }}</strong>.</p>
        </div>

        <div class="card">
            <h2>🏷️ Host Aliases</h2>
            <p class="muted">Case-insensitive names detected in live welcome messages and chat. Example: <code>mommy</code> → <strong>Sinta</strong>.</p>
            <form action="/admin/settings/alias/add" method="POST" class="form-grid alias-form">
                <input type="text" name="alias" placeholder="Alias (e.g. mommy)" required>
                <input type="text" name="host_name" placeholder="Host name (e.g. Sinta)" required>
                <button type="submit">+ Add Alias</button>
            </form>
            <div class="table-scroll">
                <table>
                    <tr><th>Alias</th><th>Detected Host</th><th>Action</th></tr>
                    {% for alias, host in aliases %}
                    <tr>
                        <td><code>{{ alias }}</code></td>
                        <td><strong>{{ host }}</strong></td>
                        <td>
                            <form action="/admin/settings/alias/delete" method="POST" class="inline-form" onsubmit="return confirm('Delete alias {{ alias }}?');">
                                <input type="hidden" name="alias" value="{{ alias }}">
                                <button type="submit" class="btn-action btn-del">Delete</button>
                            </form>
                        </td>
                    </tr>
                    {% else %}
                    <tr><td colspan="3" class="muted">No aliases configured.</td></tr>
                    {% endfor %}
                </table>
            </div>
        </div>

        <script>
            (function () {
                const master = document.getElementById('master_enabled');
                const rows = Array.from(document.querySelectorAll('.feature-row input[type="checkbox"]'));
                if (!master) return;
                function storeStates() {
                    rows.forEach(cb => cb.dataset.saved = cb.checked ? '1' : '0');
                }
                function applyMaster() {
                    rows.forEach(cb => {
                        cb.disabled = !master.checked;
                        if (master.checked) {
                            cb.checked = cb.dataset.saved !== '0' && cb.dataset.stored === '1';
                        } else {
                            cb.checked = false;
                        }
                    });
                }
                rows.forEach(cb => { cb.dataset.saved = cb.dataset.stored; });
                applyMaster();
                master.addEventListener('change', function () {
                    if (!master.checked) {
                        storeStates();
                    }
                    applyMaster();
                });
            })();
        </script>
        AUTO_REFRESH_JS_PLACEHOLDER
    </body>
    </html>
    """.replace("BASE_CSS_PLACEHOLDER", BASE_CSS).replace("NAV_HTML_PLACEHOLDER", NAV_HTML).replace("AUTO_REFRESH_JS_PLACEHOLDER", AUTO_REFRESH_JS)

    non_priority_mode = str(get_setting("non_priority_mode", "slow") or "slow").lower()
    if non_priority_mode not in ("slow", "off"):
        non_priority_mode = "slow"
    try:
        non_priority_slow_seconds = max(60, int(get_setting("non_priority_slow_seconds", "300") or 300))
    except Exception:
        non_priority_slow_seconds = 300

    return render_template_string(
        html,
        master=master,
        debug_mode=debug_mode,
        features=features,
        s=setting_values,
        tier_cfg=tier_cfg,
        hosts=hosts,
        aliases=aliases,
        tiers_reset=tiers_reset,
        tiers_permanent=tiers_permanent,
        saved=saved,
        flash_msg=flash_msg,
        signer_configured=signer_configured,
        flash_type=flash_type,
        non_priority_mode=non_priority_mode,
        non_priority_slow_seconds=non_priority_slow_seconds,
    )


@settings_bp.route("/settings/save", methods=["POST"])
def save_settings():
    master = _checkbox("master_enabled")
    old_debug_mode = get_bool_setting("debug_mode", False)
    set_setting("master_enabled", master)
    set_setting("debug_mode", _checkbox("debug_mode"))

    # Only overwrite feature flags while the master switch is on. While it is
    # off the UI shows every feature as off but their previous state is kept
    # so turning the master switch back on restores them.
    if master == "1":
        for key in (
            "feature_announcements",
            "feature_verification",
            "feature_role_tiers",
            "feature_leaderboard",
            "feature_live_tracking",
        ):
            set_setting(key, _checkbox(key))

    old_verification_channel = get_setting("verification_channel_id", "") or ""
    new_verification_channel = request.form.get("verification_channel_id", "").strip()
    for key in ("verified_role_id", "admin_role_id", "leaderboard_channel_id", "role_tier_announce_channel_id"):
        set_setting(key, request.form.get(key, "").strip())

    set_setting("verification_channel_id", new_verification_channel)
    set_setting("allowed_channels_enabled", _checkbox("allowed_channels_enabled"))
    set_setting(
        "allowed_channel_ids",
        json.dumps(_parse_id_list(request.form.get("allowed_channel_ids", "")))
    )
    set_setting(
        "announcement_channel_ids",
        json.dumps(_parse_id_list(request.form.get("announcement_channel_ids", "")))
    )

    set_setting("role_tiers_mode", request.form.get("role_tiers_mode", "replace"))
    old_reset_enabled = get_bool_setting("role_tier_reset_enabled", False)
    old_interval = get_setting("role_tier_reset_interval", "4")
    old_unit = get_setting("role_tier_reset_unit", "weeks")
    set_setting("role_tiers_enabled", _checkbox("role_tiers_enabled"))
    set_setting("role_tier_permanent_enabled", _checkbox("role_tier_permanent_enabled"))
    set_setting("role_tier_reset_enabled", _checkbox("role_tier_reset_enabled"))
    set_setting("role_tier_announce", _checkbox("role_tier_announce"))
    try:
        interval = max(1, int(request.form.get("role_tier_reset_interval", "4")))
    except ValueError:
        interval = 4
    set_setting("role_tier_reset_interval", str(interval))
    unit = request.form.get("role_tier_reset_unit", "weeks")
    set_setting("role_tier_reset_unit", unit)

    # Start a fresh reset cycle when the schedule is enabled or changed so the
    # bot does not reset roles immediately after a long disabled period.
    reset_now_enabled = _checkbox("role_tier_reset_enabled") == "1"
    if reset_now_enabled and (
        not old_reset_enabled
        or str(old_interval) != str(interval)
        or old_unit != unit
    ):
        wib = pytz.timezone("Asia/Jakarta")
        set_setting("role_tier_last_reset", datetime.now(wib).strftime("%Y-%m-%d %H:%M:%S"))

    if new_verification_channel and new_verification_channel != old_verification_channel:
        if _verification_refresh_handler:
            try:
                _verification_refresh_handler(old_verification_channel, new_verification_channel)
            except Exception as e:
                print(f"[Settings] Verification panel refresh failed: {e}")

    if get_bool_setting("debug_mode", False) != old_debug_mode and _presence_refresh_handler:
        try:
            _presence_refresh_handler()
        except Exception as e:
            print(f"[Settings] Presence refresh failed: {e}")

    return redirect("/admin/settings?msg=" + _url_q("✅ Settings disimpan. Perubahan diterapkan.") + "&msg_type=success")


@settings_bp.route("/settings/tier/add", methods=["POST"])
def add_tier():
    group_key = request.form.get("group_key", "reset")
    role_id = request.form.get("role_id", "").strip()
    try:
        min_coins = max(0, int(request.form.get("min_coins", "0")))
    except ValueError:
        min_coins = 0
    if role_id.isdigit():
        add_role_tier(group_key, role_id, min_coins)
        return redirect("/admin/settings?msg=" + _url_q("Tier ditambahkan.") + "&msg_type=success")
    return redirect("/admin/settings?msg=" + _url_q("Role ID tidak valid.") + "&msg_type=warning")


@settings_bp.route("/settings/tier/delete/<int:tier_id>", methods=["POST"])
def remove_tier(tier_id):
    delete_role_tier(tier_id)
    return redirect("/admin/settings?msg=" + _url_q("Tier dihapus.") + "&msg_type=success")


@settings_bp.route("/settings/host/add", methods=["POST"])
def add_target_host():
    username = request.form.get("tiktok_username", "").strip().lower().lstrip("@")
    if username:
        display_name = fetch_tiktok_display_name(username)
        if not display_name or display_name == username:
            display_name = get_channel_display_name(username) or username
        conn = get_db_connection()
        try:
            conn.execute("""
                INSERT INTO hosts (tiktok_username, display_name, is_active)
                VALUES (?, ?, 1)
                ON CONFLICT(tiktok_username) DO UPDATE SET display_name = excluded.display_name
            """, (username, display_name))
            conn.commit()
        finally:
            conn.close()
    return redirect("/admin/settings?msg=" + _url_q("Host ditambahkan/diperbarui.") + "&msg_type=success")


@settings_bp.route("/settings/host/delete/<int:host_id>", methods=["POST"])
def remove_target_host(host_id):
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM hosts WHERE host_id = ?", (host_id,))
        conn.commit()
    finally:
        conn.close()
    return redirect("/admin/settings")


@settings_bp.route("/settings/host-priority-mode", methods=["POST"])
def save_host_priority_mode():
    mode = (request.form.get("non_priority_mode", "slow") or "slow").strip().lower()
    if mode not in ("slow", "off"):
        mode = "slow"
    try:
        slow_seconds = max(60, int(request.form.get("non_priority_slow_seconds", "300") or 300))
    except Exception:
        slow_seconds = 300
    set_setting("non_priority_mode", mode)
    set_setting("non_priority_slow_seconds", str(slow_seconds))
    log_activity_event("system", f"Host priority mode updated: non-priority={mode}, slow_seconds={slow_seconds}.", "")
    return redirect("/admin/settings?msg=" + _url_q("Mode prioritas host disimpan.") + "&msg_type=success")


@settings_bp.route("/settings/hosts/save", methods=["POST"])
def save_hosts_batch():
    conn = get_db_connection()
    changed = 0
    deleted = 0
    try:
        rows = conn.execute("SELECT host_id, tiktok_username, COALESCE(is_active,1), COALESCE(is_priority,1) FROM hosts").fetchall()
        for host_id, username, is_active, is_priority in rows:
            hid = str(host_id)
            if request.form.get(f"delete_{hid}") == "1":
                conn.execute("DELETE FROM hosts WHERE host_id = ?", (host_id,))
                deleted += 1
                changed += 1
                continue
            new_active = 1 if request.form.get(f"active_{hid}") == "1" else 0
            new_priority = 1 if request.form.get(f"priority_{hid}") == "1" else 0
            if int(is_active or 0) != new_active or int(is_priority or 0) != new_priority:
                conn.execute(
                    "UPDATE hosts SET is_active = ?, is_priority = ? WHERE host_id = ?",
                    (new_active, new_priority, host_id),
                )
                changed += 1
        conn.commit()
    finally:
        conn.close()
    log_activity_event("system", f"Batch host settings saved (changed={changed}, deleted={deleted}).", "")
    return redirect("/admin/settings?msg=" + _url_q(f"Perubahan host disimpan (changed={changed}, deleted={deleted}).") + "&msg_type=success")


@settings_bp.route("/toggle-host-priority/<int:host_id>")
def toggle_host_priority_route(host_id):
    # Kept for backward compatibility; settings UI now uses batch save.
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT tiktok_username, COALESCE(is_priority,1) FROM hosts WHERE host_id = ?", (host_id,)).fetchone()
        if row:
            new_val = 0 if int(row[1] or 0) == 1 else 1
            conn.execute("UPDATE hosts SET is_priority = ? WHERE host_id = ?", (new_val, host_id))
            conn.commit()
            state = "priority (normal)" if new_val == 1 else "non-priority"
            log_activity_event("system", f"Host @{(row[0] or '').lower()} set to {state}.", (row[0] or "").lower())
    finally:
        conn.close()
    return redirect("/admin/settings?msg=" + _url_q("Host dihapus.") + "&msg_type=success")


@settings_bp.route("/settings/alias/add", methods=["POST"])
def add_alias():
    add_host_alias(request.form.get("alias", ""), request.form.get("host_name", ""))
    return redirect("/admin/settings?msg=" + _url_q("Alias disimpan.") + "&msg_type=success")


@settings_bp.route("/settings/alias/delete", methods=["POST"])
def remove_alias():
    delete_host_alias(request.form.get("alias", ""))
    return redirect("/admin/settings?msg=" + _url_q("Alias dihapus.") + "&msg_type=success")

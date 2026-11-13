import asyncio
import os
import time
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional, Tuple

import aiohttp


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def is_chatbot_enabled() -> bool:
    """Chatbot aktif jika URL+model dikonfigurasi (dan tidak dimatikan eksplisit)."""
    if _env_bool("CHATBOT_ENABLED", False):
        return bool((os.environ.get("OLLAMA_BASE_URL") or "").strip() and (os.environ.get("OLLAMA_MODEL") or "").strip())
    # Auto-enable ketika konfigurasi minimal tersedia, kecuali dimatikan eksplisit.
    if str(os.environ.get("CHATBOT_ENABLED", "")).strip().lower() in ("0", "false", "no", "off"):
        return False
    return bool((os.environ.get("OLLAMA_BASE_URL") or "").strip() and (os.environ.get("OLLAMA_MODEL") or "").strip())


def get_chatbot_config() -> dict:
    base_url = (os.environ.get("OLLAMA_BASE_URL") or os.environ.get("LLM_BASE_URL") or "http://localhost:11434").strip().rstrip("/")
    model = (os.environ.get("OLLAMA_MODEL") or os.environ.get("LLM_MODEL") or "").strip()
    api_key = (os.environ.get("LLM_API_KEY") or os.environ.get("OLLAMA_API_KEY") or "").strip()
    system_prompt = (os.environ.get("CHATBOT_SYSTEM_PROMPT") or "").strip() or (
        "You are the friendly assistant of the Dummy Dreams Discord community. "
        "Be helpful, concise, and respectful. Answer in the same language the user uses. "
        "If you don't know something, say so clearly."
    )
    try:
        max_history = max(0, min(40, int(os.environ.get("CHATBOT_MAX_HISTORY", "10"))))
    except Exception:
        max_history = 10
    try:
        timeout_s = max(5, min(300, int(os.environ.get("CHATBOT_TIMEOUT_SECONDS", "90"))))
    except Exception:
        timeout_s = 90
    try:
        cooldown_s = max(0, min(120, int(os.environ.get("CHATBOT_COOLDOWN_SECONDS", "3"))))
    except Exception:
        cooldown_s = 3
    return {
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
        "system_prompt": system_prompt,
        "max_history": max_history,
        "timeout_s": timeout_s,
        "cooldown_s": cooldown_s,
    }


# (channel_id or user_id) -> deque of {"role","content"}
_HISTORY: Dict[int, Deque[dict]] = defaultdict(lambda: deque(maxlen=20))
_LAST_REPLY_AT: Dict[int, float] = {}
_LOCK = asyncio.Lock()


def _history_key(message) -> int:
    # Per-channel history in guilds, per-user in DMs.
    if getattr(message, "guild", None) and getattr(message, "channel", None):
        return int(message.channel.id)
    author = getattr(message, "author", None)
    return int(author.id) if author else 0


def _cooldown_key(message) -> int:
    author = getattr(message, "author", None)
    channel = getattr(message, "channel", None)
    if getattr(message, "guild", None) and channel:
        return int(channel.id)
    return int(author.id) if author else 0


def clear_history(key: Optional[int] = None):
    if key is None:
        _HISTORY.clear()
    else:
        _HISTORY.pop(key, None)


async def should_respond(message, bot_user) -> bool:
    if not is_chatbot_enabled():
        return False
    if message.author.bot or (bot_user and message.author.id == bot_user.id):
        return False
    content = (message.content or "").strip()
    if not content:
        return False

    # DM: respond to messages (unless explicitly disabled by config logic elsewhere)
    if message.guild is None:
        return True

    # Guild: respond when mentioned or when replying to the bot.
    if bot_user and bot_user in getattr(message, "mentions", []):
        return True
    ref = getattr(message, "reference", None)
    if ref and getattr(ref, "resolved", None):
        resolved = ref.resolved
        author = getattr(resolved, "author", None)
        if bot_user and author and getattr(author, "id", None) == bot_user.id:
            return True
    return False


def _extract_user_text(message, bot_user) -> str:
    text = (message.content or "").strip()
    if bot_user:
        # Remove bot mention tokens like <@123> / <@!123>
        for token in (f"<@{bot_user.id}>", f"<@!{bot_user.id}>"):
            text = text.replace(token, "")
    return " ".join(text.split()).strip()


async def generate_reply(message, bot_user) -> Tuple[Optional[str], Optional[str]]:
    """Return (reply_text, error_message). reply_text is None on failure/skip."""
    cfg = get_chatbot_config()
    if not cfg["model"] or not cfg["base_url"]:
        return None, "chatbot not configured"

    user_text = _extract_user_text(message, bot_user)
    if not user_text:
        return None, None

    now = time.monotonic()
    cd_key = _cooldown_key(message)
    async with _LOCK:
        last = _LAST_REPLY_AT.get(cd_key, 0.0)
        if cfg["cooldown_s"] and (now - last) < cfg["cooldown_s"]:
            return None, None
        _LAST_REPLY_AT[cd_key] = now

    hkey = _history_key(message)
    async with _LOCK:
        history = list(_HISTORY[hkey])[-cfg["max_history"]:] if cfg["max_history"] else []

    messages: List[dict] = [{"role": "system", "content": cfg["system_prompt"]}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model": cfg["model"],
        "messages": messages,
        "stream": False,
    }

    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"

    url = f"{cfg['base_url']}/api/chat"
    timeout = aiohttp.ClientTimeout(total=cfg["timeout_s"])

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    return None, f"ollama http {resp.status}: {body[:200]}"
                data = await resp.json(content_type=None)
    except asyncio.TimeoutError:
        return None, "timeout"
    except Exception as e:
        return None, str(e)

    reply = ""
    try:
        msg = data.get("message") or {}
        reply = (msg.get("content") or "").strip()
    except Exception:
        reply = ""
    if not reply:
        return None, "empty response"

    async with _LOCK:
        dq = _HISTORY[hkey]
        # keep deque bounded; store the exchange
        maxlen = max(2, cfg["max_history"] * 2) if cfg["max_history"] else 20
        if dq.maxlen != maxlen:
            newdq = deque(dq, maxlen=maxlen)
            _HISTORY[hkey] = newdq
            dq = newdq
        dq.append({"role": "user", "content": user_text})
        dq.append({"role": "assistant", "content": reply})

    # Discord message limit safety
    if len(reply) > 1900:
        reply = reply[:1900] + "…"
    return reply, None

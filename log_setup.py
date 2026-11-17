"""Central logging, request tracing, and error notification.

Everything the bot does is written to ``logs/bot.log`` (rotated) and mirrored
to stdout so Render keeps showing it. WARNING/ERROR records are also queued
and sent by the Discord bot to ``LOG_ERROR_CHANNEL_ID`` so crashes, failed
requests and timeouts survive ephemeral hosting restarts.

The module is imported early from ``main.py``; ``setup_logging()`` must be
called once at startup before the bot connects.
"""

from __future__ import annotations

import atexit
import asyncio
import contextvars
import json
import logging
import os
import platform
import queue
import re
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime
from typing import Optional

try:
    import aiohttp
except ImportError:  # discord.py bundles aiohttp; keep this module importable in tests
    aiohttp = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_PATH = os.path.join(LOG_DIR, "bot.log")
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 4

LOG_ERROR_CHANNEL_ID = 1572123726839947305
try:
    _configured_error_channel = int(str(os.environ.get("LOG_ERROR_CHANNEL_ID", "")).strip() or 0)
    if _configured_error_channel:
        LOG_ERROR_CHANNEL_ID = _configured_error_channel
except (TypeError, ValueError):
    pass

_ORIGINAL_STDOUT = sys.stdout
_ORIGINAL_STDERR = sys.stderr

_env = (os.environ.get("BOT_ENV") or os.environ.get("APP_ENV") or "stable").strip().lower()
_version = (
    os.environ.get("PUSH_CODE")
    or os.environ.get("BOT_VERSION")
    or (os.environ.get("RENDER_GIT_COMMIT") or "")[:7]
    or "unknown"
)

_setup_done = False
_writer: Optional["_LogFileWriter"] = None
_console_stream = None
_crash_reported = False
_notify_queue: "queue.Queue[dict]" = queue.Queue(maxsize=500)
_recent: dict[str, list] = {}
_recent_lock = threading.Lock()
_SUPPRESS_NOTIFY = contextvars.ContextVar("log_suppress_notify", default=False)

_REDACT_PATTERNS = (
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(\S+)"), r"\1***"),
    (
        re.compile(
            r"(?i)\b(token|password|secret|api[_-]?key|auth[_-]?token|turso[_-]?auth[_-]?token)\b"
            r"\s*[:=]\s*['\"]?([^'\"\s,&]+)"
        ),
        r"\1=***",
    ),
    (re.compile(r"(discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/)[\w-]+"), r"\1***"),
    (re.compile(r"\beyJ[\w-]{20,}\.[\w-]{10,}\.[\w-]{10,}\b"), "***JWT***"),
    (re.compile(r"\b[MN][\w-]{23,}\.[\w-]{6}\.[\w-]{27,}\b"), "***TOKEN***"),
)


def redact(text) -> str:
    """Remove credentials from anything before it is written or sent."""
    if text is None:
        return ""
    out = str(text)
    for pattern, replacement in _REDACT_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def set_build_info(*, env: str = None, version: str = None) -> None:
    global _env, _version
    if env:
        _env = str(env)
    if version:
        _version = str(version)


def get_build_info() -> dict:
    return {"env": _env or "stable", "version": _version or "unknown"}


def get_logger(name: str = "bot") -> logging.Logger:
    return logging.getLogger(name)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class _LogFileWriter:
    """Single background writer for the log file (rotation + no loop blocking)."""

    def __init__(self, path: str, max_bytes: int, backups: int):
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=20000)
        self._fh = None
        self._size = 0
        self._open()
        threading.Thread(target=self._run, name="log-file-writer", daemon=True).start()

    def _open(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8", errors="replace")
        try:
            self._size = os.path.getsize(self.path)
        except OSError:
            self._size = 0

    def _rotate(self) -> None:
        try:
            if self._fh:
                self._fh.close()
        except Exception:
            pass
        try:
            oldest = f"{self.path}.{self.backups}"
            if os.path.exists(oldest):
                os.remove(oldest)
            for index in range(self.backups - 1, 0, -1):
                src = f"{self.path}.{index}"
                dst = f"{self.path}.{index + 1}"
                if os.path.exists(src):
                    os.replace(src, dst)
            if os.path.exists(self.path):
                os.replace(self.path, f"{self.path}.1")
        except Exception as exc:  # never crash the writer
            _ORIGINAL_STDERR.write(f"[LogRotate] failed: {exc}\n")
        finally:
            self._open()

    def write(self, text: str) -> None:
        if not text:
            return
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            pass

    def flush(self) -> None:
        return None

    def close(self) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    def _run(self) -> None:
        while True:
            chunk = self._queue.get()
            if chunk is None:
                try:
                    if self._fh:
                        self._fh.flush()
                        self._fh.close()
                except Exception:
                    pass
                return
            try:
                if self._size + len(chunk.encode("utf-8", "replace")) > self.max_bytes:
                    self._rotate()
                self._fh.write(chunk)
                self._fh.flush()
                self._size += len(chunk.encode("utf-8", "replace"))
            except Exception as exc:
                _ORIGINAL_STDERR.write(f"[LogWriter] write failed: {exc}\n")


class _Tee:
    """Mirror raw stdout/stderr to the console and the log file."""

    def __init__(self, original, writer: _LogFileWriter, name: str):
        self._original = original
        self._writer = writer
        self._name = name
        self._at_line_start = True

    def write(self, text):
        if not text:
            return 0
        try:
            self._original.write(text)
        except Exception:
            pass
        try:
            stamped = []
            for part in text.splitlines(keepends=True):
                if self._at_line_start:
                    stamped.append(f"[{_now()}] {part}")
                else:
                    stamped.append(part)
                self._at_line_start = part.endswith("\n")
            self._writer.write("".join(stamped))
        except Exception:
            pass
        return len(text)

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass

    @property
    def encoding(self):
        return getattr(self._original, "encoding", "utf-8")

    @property
    def errors(self):
        return getattr(self._original, "errors", "replace")

    @property
    def buffer(self):
        return getattr(self._original, "buffer", None)

    def writable(self):
        return True

    def isatty(self):
        return False

    def fileno(self):
        try:
            return self._original.fileno()
        except Exception:
            raise OSError("no fileno")


class _ConsoleAndFileHandler(logging.Handler):
    def __init__(self, writer: _LogFileWriter, level=logging.DEBUG):
        super().__init__(level)
        self._writer = writer
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = redact(self.format(record))
            try:
                _ORIGINAL_STDOUT.write(message + "\n")
            except Exception:
                pass
            self._writer.write(message + "\n")
        except Exception:
            self.handleError(record)


class _NotifyHandler(logging.Handler):
    """Queue WARNING+ records for delivery to the Discord error channel."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if _SUPPRESS_NOTIFY.get():
                return
            if record.name.startswith("bot.notify") or record.name == "log_setup":
                return
            message = redact(record.getMessage())
            tb = ""
            if record.exc_info:
                tb = redact("".join(traceback.format_exception(*record.exc_info)))
            payload = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "logger": record.name,
                "message": message[:1500],
                "traceback": tb[-2500:],
            }
            _notify_queue.put_nowait(payload)
        except Exception:
            pass


class _NotifyFilter(logging.Filter):
    """Drop noisy library warnings that are not actionable."""

    _SKIP = (
        "rate limited",
        "rate limit",
        "PyNaCl is not installed",
        "voice will NOT be supported",
        "Unclosed client session",
        "Unclosed connector",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        text = str(record.getMessage()).lower()
        return not any(token.lower() in text for token in self._SKIP)


def setup_logging() -> None:
    global _setup_done, _writer, _console_stream
    if _setup_done:
        return
    _setup_done = True

    os.makedirs(LOG_DIR, exist_ok=True)
    _writer = _LogFileWriter(LOG_PATH, LOG_MAX_BYTES, LOG_BACKUPS)
    _console_stream = _Tee(_ORIGINAL_STDOUT, _writer, "stdout")
    sys.stdout = _console_stream
    sys.stderr = _Tee(_ORIGINAL_STDERR, _writer, "stderr")

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.DEBUG)

    handler = _ConsoleAndFileHandler(_writer, level=logging.DEBUG)
    root.addHandler(handler)

    notify = _NotifyHandler(level=logging.WARNING)
    notify.addFilter(_NotifyFilter())
    root.addHandler(notify)

    # Keep third-party noise reasonable while preserving useful diagnostics.
    logging.getLogger("discord").setLevel(logging.INFO)
    logging.getLogger("discord.http").setLevel(logging.INFO)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("waitress").setLevel(logging.INFO)

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook

    logger = logging.getLogger("bot.startup")
    logger.info("=" * 78)
    logger.info("BOT PROCESS STARTED")
    logger.info("env=%s version=%s pid=%s", _env, _version, os.getpid())
    logger.info("python=%s platform=%s", sys.version.replace("\n", " "), platform.platform())
    logger.info("cwd=%s log=%s", os.getcwd(), LOG_PATH)
    logger.info("=" * 78)

    atexit.register(_writer.close)


def _excepthook(exc_type, exc_value, exc_tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        return
    logging.getLogger("bot.crash").critical(
        "Uncaught exception", exc_info=(exc_type, exc_value, exc_tb)
    )
    # The process may exit before the async notifier can run, so report fatal
    # crashes directly over HTTP as a last resort.
    _send_crash_report_sync(
        "Uncaught exception",
        "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
    )


def _send_crash_report_sync(title: str, detail: str) -> None:
    global _crash_reported
    if _crash_reported:
        return
    _crash_reported = True
    token = (os.environ.get("DISCORD_TOKEN") or "").strip()
    if not token or not LOG_ERROR_CHANNEL_ID:
        return
    info = get_build_info()
    text = (
        f"💥 **CRASH** · `{info['env']}` · `{info['version']}`\n"
        f"**{title}**\n```{redact(detail)[-1700:]}```"
    )
    payload = json.dumps({"content": text[:1990]}).encode("utf-8")
    request = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{LOG_ERROR_CHANNEL_ID}/messages",
        data=payload,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DummyDreamsBot (crash-report, 1.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
    except Exception as exc:
        _ORIGINAL_STDERR.write(f"[CrashReport] direct send failed: {type(exc).__name__}: {exc}\n")


def _thread_excepthook(args) -> None:
    if args.exc_type is SystemExit:
        return
    logging.getLogger("bot.thread").critical(
        "Uncaught thread exception in %s",
        getattr(args.thread, "name", "?"),
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def install_asyncio_exception_handler(loop) -> None:
    logger = logging.getLogger("bot.asyncio")

    def _handler(loop, context):
        exc = context.get("exception")
        message = context.get("message") or "Unhandled asyncio error"
        if isinstance(exc, asyncio.CancelledError):
            return
        if exc is not None:
            logger.error(
                "Asyncio: %s | task=%r",
                message,
                context.get("task") or context.get("future"),
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        else:
            logger.error("Asyncio: %s | context=%r", message, {
                k: v for k, v in context.items() if k not in ("exception", "future", "task")
            })

    loop.set_exception_handler(_handler)


def build_http_trace():
    """Trace outgoing Discord HTTP requests (mutations, slow calls, failures)."""
    if aiohttp is None:
        return None
    logger = logging.getLogger("bot.http")
    trace = aiohttp.TraceConfig()

    async def on_request_start(session, ctx, params):
        ctx.start = time.perf_counter()

    async def on_request_end(session, ctx, params):
        try:
            duration = (time.perf_counter() - getattr(ctx, "start", time.perf_counter())) * 1000
            method = str(params.method).upper()
            url = redact(str(params.url))
            status = getattr(params.response, "status", "?")
            if status >= 400 or duration >= 2000:
                logger.warning(
                    "Discord HTTP %s %s -> %s in %.0fms", method, url, status, duration
                )
            elif method != "GET":
                logger.info("Discord HTTP %s %s -> %s in %.0fms", method, url, status, duration)
            else:
                logger.debug("Discord HTTP %s %s -> %s in %.0fms", method, url, status, duration)
        except Exception:
            pass

    async def on_request_exception(session, ctx, params):
        try:
            logger.error(
                "Discord HTTP EXCEPTION %s %s: %r",
                str(params.method).upper(),
                redact(str(params.url)),
                params.exception,
            )
        except Exception:
            pass

    trace.on_request_start.append(on_request_start)
    trace.on_request_end.append(on_request_end)
    trace.on_request_exception.append(on_request_exception)
    return trace


def notification_queue_size() -> int:
    return _notify_queue.qsize()


def _format_notification(item: dict, repeats: int = 0) -> str:
    info = get_build_info()
    head = f"🚨 **{item['level']}** · `{info['env']}` · `{info['version']}`"
    if repeats > 1:
        head += f" · repeated {repeats}×"
    lines = [head, f"**Source:** `{item['logger']}`", f"**Time:** {item['time']}", ""]
    lines.append(f"```{item['message']}```")
    if item.get("traceback"):
        lines.append(f"```{item['traceback']}```")
    text = "\n".join(lines)
    if len(text) > 1900:
        text = text[:1890] + "\n… truncated"
    return text


def _dedupe_key(item: dict) -> str:
    raw = str(item.get("message") or "")[:160]
    raw = re.sub(r"\d+(?:\.\d+)?ms\b", "#ms", raw)
    raw = re.sub(r"\d+(?:\.\d+)?s\b", "#s", raw)
    raw = re.sub(r"(?i)failures?[=:]\s*\d+", "failures=#", raw)
    raw = re.sub(r"\b[0-9a-f]{8,}\b", "#", raw)
    return f"{item.get('logger')}|{raw}"


async def notify_loop(bot) -> None:
    """Deliver queued warnings/errors to the Discord error channel."""
    logger = logging.getLogger("bot.notify")
    window_started = time.monotonic()
    sent_in_window = 0
    while True:
        try:
            await asyncio_sleep(1.0)
            if not LOG_ERROR_CHANNEL_ID:
                await asyncio_sleep(60.0)
                continue
            if not bot.is_ready():
                continue
            now = time.monotonic()
            if now - window_started >= 60.0:
                window_started = now
                sent_in_window = 0
            if sent_in_window >= 12:
                # Protect the channel from error storms; queued items are kept.
                await asyncio_sleep(5.0)
                continue
            batch = []
            skipped = 0
            while len(batch) < 3 and not _notify_queue.empty():
                item = _notify_queue.get_nowait()
                key = _dedupe_key(item)
                now = time.monotonic()
                with _recent_lock:
                    last, count = _recent.get(key, [0.0, 0])
                    if last and now - last < 180.0:
                        _recent[key] = [last, count + 1]
                        skipped += 1
                        continue
                    repeats = count + 1
                    _recent[key] = [now, 0]
                batch.append(_format_notification(item, repeats))
            if not batch:
                continue
            text = "\n\n".join(batch)
            if len(text) > 1950:
                text = text[:1940] + "\n… truncated"
            token = _SUPPRESS_NOTIFY.set(True)
            try:
                channel = bot.get_channel(LOG_ERROR_CHANNEL_ID)
                if channel is None:
                    channel = await bot.fetch_channel(LOG_ERROR_CHANNEL_ID)
                await channel.send(content=text)
            finally:
                _SUPPRESS_NOTIFY.reset(token)
            sent_in_window += len(batch)
            logger.info("Sent %d notification(s) to error channel (skipped %d duplicates).", len(batch), skipped)
            await asyncio_sleep(1.5)
        except Exception as exc:
            _ORIGINAL_STDERR.write(f"[LogNotify] failed: {type(exc).__name__}: {exc}\n")
            await asyncio_sleep(5.0)


async def asyncio_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def tail_log(lines: int = 300, level: str = "all", search: str = None):
    """Return ``(text, matched_count)`` from the end of the log file."""
    try:
        count = max(1, min(int(lines or 300), 3000))
    except (TypeError, ValueError):
        count = 300

    if not os.path.exists(LOG_PATH):
        return "Log file not found.", 0

    try:
        with open(LOG_PATH, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            data = b""
            while position > 0 and data.count(b"\n") <= count * 2 and len(data) < 4 * 1024 * 1024:
                read_size = min(65536, position)
                position -= read_size
                handle.seek(position)
                data = handle.read(read_size) + data
        text = data.decode("utf-8", "replace")
    except Exception as exc:
        return f"Could not read log file: {exc}", 0

    level_key = str(level or "all").strip().lower()
    if level_key in ("", "all", "any", "*"):
        tokens = None
    elif level_key.startswith("err"):
        tokens = ("error", "critical", "traceback", "exception", "gagal", "failed", "timeout")
    elif level_key.startswith("warn"):
        tokens = ("warning", "warn", "error", "critical", "traceback", "gagal", "failed", "timeout")
    else:
        tokens = (level_key,)

    needle = (search or "").strip().lower()
    selected = []
    for line in text.splitlines():
        lowered = line.lower()
        if tokens and not any(token in lowered for token in tokens):
            continue
        if needle and needle not in lowered:
            continue
        selected.append(redact(line))

    return "\n".join(selected[-count:]), len(selected)

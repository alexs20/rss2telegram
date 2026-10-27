#!/usr/bin/env python3
"""rss2tg - poll Atom/RSS feeds and push new items to Telegram.

Each feed carries its own bot token and destination chat id, so one feed can go
to one group, another feed to another group, through different bots.

Duplicate suppression is SQLite-backed: every (feed, chat, item) triple is
recorded in /data/rss2tg.db once Telegram has accepted the message. A row is
written only after a 200 response, so a failed send is retried on the next
round instead of being silently lost.

Usage:
    python rss2tg.py                       # run forever on /app/config.yml
    python rss2tg.py /app/config.yml       # run forever on an explicit config
    python rss2tg.py --once                # single polling round, then exit
    python rss2tg.py --check               # validate config + open the database
"""

from __future__ import annotations

import argparse
import hashlib
import html
import logging
import os
import re
import signal
import sqlite3
import sys
import threading
import time

import feedparser
import requests
import yaml

log = logging.getLogger("rss2tg")

TG_API = "https://api.telegram.org/bot{token}/{method}"
MAX_TEXT = 4000
USER_AGENT = "rss2tg/1.0 (+https://core.telegram.org/bots/api)"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# XenForo-style feeds (e.g. RFD forums) append a meta line to every
# description: "Statistics: Posted by <user> — <date>". Stripped unless the
# feed sets include_statistics: true.
_STATS_RE = re.compile(r"\s*Statistics:\s*(?:Posted by|posted on).*", re.DOTALL)

_stop = threading.Event()


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
class ConfigError(Exception):
    """Raised when config.yml is missing, malformed or inconsistent."""


def clean(value: str, limit: int) -> str:
    """Strip tags and entities, collapse whitespace, truncate with an ellipsis."""
    text = html.unescape(_TAG_RE.sub(" ", value or ""))
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "\u2026"
    return text


class Feed:
    __slots__ = (
        "name", "url", "bot_token", "chat_id", "key", "interval",
        "include_description", "include_statistics", "disable_preview",
    )

    def __init__(self, raw, index):
        if not isinstance(raw, dict):
            raise ConfigError(f"feeds[{index}] must be a mapping, got {type(raw).__name__}")

        label = f"feeds[{index}]"
        self.url = str(raw.get("url") or "").strip()
        self.bot_token = str(raw.get("bot_token") or "").strip()
        chat_id = raw.get("chat_id")

        if not self.url:
            raise ConfigError(f"{label}: 'url' is required")
        if not self.bot_token:
            raise ConfigError(f"{label} ({self.url}): 'bot_token' is required")
        if chat_id is None or str(chat_id).strip() == "":
            raise ConfigError(f"{label} ({self.url}): 'chat_id' is required")
        if ":" not in self.bot_token:
            raise ConfigError(
                f"{label} ({self.url}): 'bot_token' must look like '123456789:AA...'"
            )

        # str() keeps the leading minus of supergroup ids such as -1001234567890
        self.chat_id = str(chat_id).strip()
        self.name = str(raw.get("name") or self.url).strip()

        # Dedupe scope: same feed delivered to two different chats counts as two
        # separate deliveries, so the chat id is part of the key.
        self.key = f"{self.url}\x1f{self.chat_id}"

        # Per-feed interval override; None means "use the global interval".
        interval = raw.get("interval")
        self.interval = None if interval is None else max(30, int(interval))

        self.include_description = bool(raw.get("include_description", True))
        self.include_statistics = bool(raw.get("include_statistics", False))
        self.disable_preview = bool(raw.get("disable_preview", True))


class Config:
    def __init__(self, raw, path="-"):
        raw = raw or {}
        self.path = path
        self.interval = max(30, int(raw.get("interval", 900)))
        self.send_delay = max(0.0, float(raw.get("send_delay", 2.0)))
        self.state_db = str(raw.get("state_db") or "/data/rss2tg.db")
        self.timeout = float(raw.get("http_timeout", 20))
        self.retention_days = int(raw.get("retention_days", 90))
        self.log_level = str(raw.get("log_level") or os.environ.get("LOG_LEVEL", "INFO")).upper()
        self.bootstrap = str(raw.get("bootstrap", "skip")).strip().lower()

        if self.bootstrap not in ("skip", "send"):
            raise ConfigError("'bootstrap' must be 'skip' or 'send'")

        feeds = raw.get("feeds")
        if not isinstance(feeds, list) or not feeds:
            raise ConfigError("'feeds' must be a non-empty list")

        self.feeds = [Feed(item, i) for i, item in enumerate(feeds)]

        seen_pairs = {}
        for feed in self.feeds:
            if feed.key in seen_pairs:
                raise ConfigError(
                    f"duplicate target: feeds '{seen_pairs[feed.key]}' and '{feed.name}' "
                    f"both send {feed.url} to chat {feed.chat_id}; each item would be "
                    f"delivered twice"
                )
            seen_pairs[feed.key] = feed.name

    @classmethod
    def load(cls, path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = yaml.safe_load(handle)
        except FileNotFoundError as exc:
            raise ConfigError(f"config file not found: {path}") from exc
        except PermissionError as exc:
            raise ConfigError(
                f"config file not readable: {path} ({exc.strerror}); this process runs "
                f"as uid {os.getuid()} gid {os.getgid()}, so the file must be readable "
                f"by that user (chown or chmod it on the host)"
            ) from exc
        except OSError as exc:
            raise ConfigError(f"cannot read config {path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
        return cls(raw, path)

    def interval_for(self, feed):
        return feed.interval if feed.interval is not None else self.interval


# --------------------------------------------------------------------------- #
# dedupe store
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS sent (
    feed_key  TEXT    NOT NULL,
    item_key  TEXT    NOT NULL,
    title     TEXT,
    chat_id   TEXT    NOT NULL,
    sent_at   INTEGER NOT NULL,
    PRIMARY KEY (feed_key, item_key)
);
CREATE INDEX IF NOT EXISTS sent_at_idx ON sent (sent_at);
"""


def open_db(path):
    """Open (creating if needed) the SQLite dedupe database."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        # WAL keeps readers from blocking the writer; fall back to the default
        # journal if the filesystem refuses WAL (some network/shared mounts do).
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError as exc:
        log.warning("WAL unavailable on %s (%s), using default journal", path, exc)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def item_key(entry) -> str:
    """Stable identity for an entry: Atom <id>, else RSS guid, else link, else hash."""
    for field in ("id", "guid", "link"):
        value = (entry.get(field) or "").strip()
        if value:
            return value
    digest = hashlib.sha1(
        "\x1f".join(
            [
                entry.get("title") or "",
                entry.get("published") or "",
                entry.get("updated") or "",
            ]
        ).encode("utf-8")
    ).hexdigest()
    return f"sha1:{digest}"


def count_sent(conn, feed_key):
    row = conn.execute("SELECT COUNT(*) FROM sent WHERE feed_key = ?", (feed_key,)).fetchone()
    return row[0] if row else 0


def already_sent(conn, feed_key, key):
    row = conn.execute(
        "SELECT 1 FROM sent WHERE feed_key = ? AND item_key = ? LIMIT 1", (feed_key, key)
    ).fetchone()
    return row is not None


def mark_sent(conn, feed_key, key, title, chat_id):
    conn.execute(
        "INSERT OR IGNORE INTO sent (feed_key, item_key, title, chat_id, sent_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (feed_key, key, title, chat_id, int(time.time())),
    )


def prune(conn, retention_days):
    """Drop history older than the retention window so the file stays small."""
    if retention_days <= 0:
        return 0
    cutoff = int(time.time()) - retention_days * 86400
    cur = conn.execute("DELETE FROM sent WHERE sent_at < ?", (cutoff,))
    conn.commit()
    return cur.rowcount or 0


# --------------------------------------------------------------------------- #
# feed handling
# --------------------------------------------------------------------------- #
def fetch_entries(feed, timeout):
    response = requests.get(feed.url, timeout=timeout, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    if parsed.get("bozo") and not parsed.entries:
        raise RuntimeError(f"unparsable feed: {parsed.get('bozo_exception')}")
    return list(parsed.entries)


def chronological(entries):
    """Oldest first so a backlog is replayed in publication order."""
    stamps = []
    for entry in entries:
        stamp = None
        for field in ("published_parsed", "updated_parsed"):
            value = entry.get(field)
            if value:
                try:
                    stamp = time.mktime(tuple(value))
                except (TypeError, OverflowError, ValueError):
                    stamp = None
                break
        if stamp is None:
            # No reliable dates in this feed: keep feed order, reversed so the
            # caller's normal iteration ends up oldest-first.
            return list(reversed(entries))
        stamps.append(stamp)
    return [entry for _, entry in sorted(zip(stamps, entries), key=lambda pair: pair[0])]


def render(feed, entry) -> str:
    title = clean(entry.get("title") or "(no title)", 300)
    parts = [f"<b>{html.escape(title)}</b>"]

    if feed.include_description:
        summary = entry.get("summary") or ""
        if not summary:
            content = entry.get("content") or []
            if isinstance(content, list) and content:
                summary = content[0].get("value", "") or ""
        summary = clean(summary, 600)
        if not feed.include_statistics:
            summary = _STATS_RE.sub("", summary).strip()
        if summary:
            parts.append(html.escape(summary))

    link = (entry.get("link") or "").strip()
    if link:
        parts.append(f'<a href="{html.escape(link, quote=True)}">open</a>')
    return "\n\n".join(parts)[:MAX_TEXT]


def send_message(feed, text, session, timeout) -> bool:
    """POST one message. Returns True only when Telegram accepted it."""
    url = TG_API.format(token=feed.bot_token, method="sendMessage")
    payload = {
        "chat_id": feed.chat_id,
        "text": text[:MAX_TEXT],
        "parse_mode": "HTML",
        "disable_web_page_preview": feed.disable_preview,
    }

    for attempt in range(4):
        if _stop.is_set():
            return False
        try:
            response = session.post(url, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            log.warning("%s: transport error (%s), attempt %d/4", feed.name, exc, attempt + 1)
            _stop.wait(2 ** attempt)
            continue

        if response.status_code == 200:
            return True

        try:
            body = response.json()
        except ValueError:
            body = {}

        if response.status_code == 429:
            retry_after = int((body.get("parameters") or {}).get("retry_after", 5))
            log.warning("%s: rate limited by Telegram, sleeping %ss", feed.name, retry_after)
            _stop.wait(retry_after)
            continue

        if response.status_code == 400 and "parse_mode" in payload:
            # Usually an HTML entity Telegram dislikes: retry as plain text once.
            log.warning("%s: HTML rejected, resending as plain text", feed.name)
            payload.pop("parse_mode")
            payload["text"] = clean(text, MAX_TEXT)
            continue

        description = body.get("description") or response.text
        log.error("%s: Telegram API %s (%s)", feed.name, response.status_code, description[:200])
        return False

    return False


def process_feed(cfg, feed, conn, session) -> int:
    """Poll one feed and push whatever is new. Returns messages sent."""
    try:
        entries = fetch_entries(feed, cfg.timeout)
    except Exception as exc:  # noqa: BLE001 - one bad feed must not stop the round
        log.error("%s: fetch failed: %s", feed.name, exc)
        return 0

    # First sight of this feed: record the archive instead of flooding the chat.
    if cfg.bootstrap == "skip" and count_sent(conn, feed.key) == 0:
        for entry in entries:
            mark_sent(conn, feed.key, item_key(entry), clean(entry.get("title"), 200), feed.chat_id)
        conn.commit()
        log.info("%s: baseline recorded (%d items, nothing sent)", feed.name, len(entries))
        return 0

    fresh = [e for e in entries if not already_sent(conn, feed.key, item_key(e))]
    if not fresh:
        log.debug("%s: no new items", feed.name)
        return 0

    # Every unseen entry is processed. Nothing is capped or discarded: a large
    # backlog simply takes as long as send_delay allows.
    backlog = chronological(fresh)
    log.info("%s: %d new item(s) to deliver", feed.name, len(backlog))

    sent = 0
    for entry in backlog:
        if _stop.is_set():
            break
        key = item_key(entry)
        title = clean(entry.get("title"), 200)
        if not send_message(feed, render(feed, entry), session, cfg.timeout):
            log.error("%s: delivery failed for %r, will retry next round", feed.name, title)
            continue
        mark_sent(conn, feed.key, key, title, feed.chat_id)
        conn.commit()  # commit per message so a crash never re-sends this one
        sent += 1
        log.info("%s -> %s: %s", feed.name, feed.chat_id, title)
        if cfg.send_delay:
            _stop.wait(cfg.send_delay)
    return sent


def due_feeds(cfg, last_run, now):
    """Feeds whose own interval has elapsed (per-feed interval overrides global)."""
    due = []
    for feed in cfg.feeds:
        interval = cfg.interval_for(feed)
        if now - last_run.get(feed.key, 0.0) >= interval:
            due.append(feed)
    return due


# --------------------------------------------------------------------------- #
# entry points
# --------------------------------------------------------------------------- #
# The config lives on the mounted data volume, so a compose file never has to
# bind-mount or name it. One fixed name, one fixed place.
DEFAULT_CONFIG_PATH = "/data/config.yml"


def resolve_config_path(explicit=None):
    """Return the config path to use, or raise ConfigError explaining what is missing."""
    if explicit:
        return explicit
    from_env = os.environ.get("CONFIG_PATH")
    if from_env:
        return from_env
    if os.path.isfile(DEFAULT_CONFIG_PATH):
        return DEFAULT_CONFIG_PATH

    hint = ""
    if os.path.isdir("/data"):
        try:
            present = sorted(os.listdir("/data"))
            hint = f" (/data contains: {', '.join(present) or 'nothing'})"
        except OSError as exc:
            # Distinguish "empty" from "cannot see into it" - the fixes differ.
            hint = f" (/data cannot be listed: {exc.strerror})"
    raise ConfigError(
        f"no config file at {DEFAULT_CONFIG_PATH}{hint}; put config.yml in the "
        f"mounted /data directory"
    )


def run_once(cfg, conn, session, last_run=None):
    last_run = last_run if last_run is not None else {}
    now = time.time()
    total = 0
    for feed in due_feeds(cfg, last_run, now):
        if _stop.is_set():
            break
        last_run[feed.key] = now
        total += process_feed(cfg, feed, conn, session)
    return total


def run_forever(cfg, conn, session):
    last_run = {}
    tick = 5  # scheduler granularity, not a network call rate
    log.info(
        "rss2tg up: %d feed(s) from %s, default interval %ss, dedupe db %s",
        len(cfg.feeds), cfg.path, cfg.interval, cfg.state_db,
    )
    while not _stop.is_set():
        try:
            sent = run_once(cfg, conn, session, last_run)
            if sent:
                log.info("round done: %d message(s) delivered", sent)
        except Exception:  # noqa: BLE001 - the loop must outlive any single failure
            log.exception("round failed, continuing")
        _stop.wait(tick)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Poll Atom/RSS feeds and push new items to Telegram.")
    parser.add_argument(
        "config",
        nargs="?",
        default=None,
        help=f"path to the config file (default: {DEFAULT_CONFIG_PATH}; "
        f"$CONFIG_PATH overrides)",
    )
    parser.add_argument("--once", action="store_true", help="run one round and exit")
    parser.add_argument("--check", action="store_true", help="validate config and database, then exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        config_path = resolve_config_path(args.config)
        cfg = Config.load(config_path)
    except ConfigError as exc:
        log.critical("config error: %s", exc)
        return 2

    logging.getLogger().setLevel(
        getattr(logging, cfg.log_level, logging.INFO) if isinstance(cfg.log_level, str) else logging.INFO
    )

    if args.check:
        try:
            conn = open_db(cfg.state_db)
        except sqlite3.Error as exc:
            log.critical("database error at %s: %s", cfg.state_db, exc)
            return 3
        log.info("config OK: %d feed(s) from %s", len(cfg.feeds), config_path)
        for feed in cfg.feeds:
            log.info(
                "  %-24s interval=%ss chat=%s history=%d",
                feed.name[:24], cfg.interval_for(feed), feed.chat_id, count_sent(conn, feed.key),
            )
        conn.close()
        return 0

    def _shutdown(signum, _frame):
        log.info("signal %s received, finishing current message", signum)
        _stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        conn = open_db(cfg.state_db)
    except sqlite3.Error as exc:
        log.critical("cannot open database %s: %s", cfg.state_db, exc)
        return 3

    session = requests.Session()
    try:
        removed = prune(conn, cfg.retention_days)
        if removed:
            log.info("pruned %d history row(s) older than %d days", removed, cfg.retention_days)

        if args.once:
            sent = run_once(cfg, conn, session)
            log.info("single round done: %d message(s) delivered", sent)
            return 0

        run_forever(cfg, conn, session)
        return 0
    finally:
        session.close()
        conn.close()
        log.info("stopped")


if __name__ == "__main__":
    sys.exit(main())

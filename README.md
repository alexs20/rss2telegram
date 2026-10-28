# rss2telegram

Polls Atom/RSS feeds in Docker and pushes each new item to a Telegram group, one
message at a time, with SQLite-backed duplicate suppression.

- **Per-feed targeting** — every feed has its own `bot_token` and `chat_id`, so
  feed A can go to group A through bot A while feed B goes to group B through
  bot B.
- **Intervals** — one global `interval`, overridable per feed.
- **No duplicates** — every delivered item is recorded in `/data/rss2tg.db`
  (SQLite). A row is written **only after** Telegram returns 200, so a failed
  send is retried on the next round instead of being lost or double-posted.
- **One by one, all of them** — every unseen entry is delivered oldest-first
  with `send_delay` between messages. There is no per-round cap: nothing is
  ever discarded, so a large backlog just takes as long as the delay allows.

## Requirements

Docker Engine with the Compose v2 plugin. Verified on Docker 28.1.1 and
Compose v2.35.1 (host `xu20-pdev-vb`).

## Files

| File | Purpose |
|---|---|
| `rss2tg.py` | the service |
| `config.yml` | feeds, bot tokens, chat ids, intervals — **edit this** |
| `Dockerfile` | image definition (Python 3.12-slim, non-root user `app`) |
| `docker-compose.yml` | volume for `/data`, read-only config mount, log rotation |
| `.env.example` | optional container-user override |
| `requirements.txt` | `feedparser`, `requests`, `PyYAML` |

## Run it

```bash
cd /host/ws/rss2telegram
cp .env.example .env          # optional, see "Running unprivileged" below

# 1. put your real bot tokens and chat ids into config.yml
$EDITOR config.yml

# 2. validate the config before starting (no network calls, no messages sent)
docker compose run --rm rss2tg --check

# 3. start
docker compose up -d --build
```

Watch it work:

```bash
docker compose logs -f --tail 20        # follow
docker compose ps                       # health status
```

Expected `--check` output:

```
INFO    config OK: 2 feed(s)
INFO      https://forums.redflagde interval=900s chat=-100XXXXXXXXXXX history=0
INFO      https://hnrss.org/frontp interval=3600s chat=-100YYYYYYYYYYY history=0
```

Stop / restart / remove:

```bash
docker compose stop          # graceful: finishes the current message
docker compose restart       # picks up config.yml edits
docker compose down          # keeps the rss2tg-data volume and its history
docker compose down -v       # also deletes the dedupe history
```

## Config reference

`config.yml`, top level:

| Key | Default | Meaning |
|---|---|---|
| `interval` | `900` | seconds between polls for feeds without their own interval |
| `send_delay` | `2.0` | seconds between two consecutive messages |
| `state_db` | `/data/rss2tg.db` | SQLite dedupe file, inside the container |
| `retention_days` | `90` | how long delivery history is kept |
| `bootstrap` | `skip` | `skip` = record the existing archive silently; `send` = post it all |
| `http_timeout` | `20` | feed download timeout, seconds |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `CRITICAL` |

Each entry under `feeds`:

| Key | Required | Meaning |
|---|---|---|
| `url` | yes | Atom or RSS URL (both handled by feedparser) |
| `bot_token` | yes | `123456789:AA...` from @BotFather |
| `chat_id` | yes | group/supergroup id, keep it quoted: `"-100XXXXXXXXXXX"` |
| `interval` | no | per-feed poll interval in seconds |
| `include_description` | no | `true` (default) adds a cleaned summary line |
| `include_statistics` | no | `false` (default) strips the XenForo meta line `Statistics: Posted by ...` from the summary; `true` keeps it |
| `disable_preview` | no | `true` (default) suppresses link previews |

Two feeds with the same `url` **and** the same `chat_id` are rejected at startup,
because that combination would post every item twice. The same URL to two
different chats is allowed and tracked separately per chat.

## The `bootstrap` setting

On the very first run a feed's existing items are either recorded silently or
posted in full, controlled by `bootstrap`:

- `skip` (default) — history is filled, nothing is sent. No flood of old posts.
- `send` — every item currently in the feed is pushed, oldest first.
  Use this only when you intentionally want the backlog in the group.

## Getting a chat id

1. Add the bot to the group (channels: add it as an **administrator**).
2. Post any message in the group.
3. Read the id back:

```bash
curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" \
  | grep -o '"chat":{"id":-\?[0-9]*' | head
```

Group and supergroup ids are negative (`-100...`). Keep the quotes in
`config.yml` so YAML never reinterprets them.

## Install a published release

Every `v*.*.*` tag builds the image on GitHub Actions, pushes a multi-arch
image to the registry and attaches ready-to-run files to the release page. No
repo clone and no local build:

```bash
# 1. pull the image and the compose file for a specific version
IMG=ghcr.io/alexs20/rss2telegram:1.2.3
docker pull "$IMG"

# 2. config lives outside the image
mkdir -p data
cp config.example.yml data/config.yml   # from the release page
$EDITOR data/config.yml                 # bot tokens + chat ids
sudo chown -R 10001:10001 data          # the container runs as uid 10001

# 3. validate, then start
docker compose -f docker-compose.release.yml run --rm rss2tg --check
docker compose -f docker-compose.release.yml up -d
```

The release page carries:

| Asset | Use |
|---|---|
| `docker-compose.yml` | compose file, image pinned to that exact version |
| `rss2telegram-<ver>-linux-amd64.tar.gz` | `gunzip -c ... \| docker load`, for hosts without registry access |
| `rss2telegram-<ver>-linux-arm64.tar.gz` | same, for Raspberry Pi / arm64 NAS |
| `config.example.yml` | template, copy to `data/config.yml` |
| `SHA256SUMS.txt` | `sha256sum -c SHA256SUMS.txt` |
| `INSTALL.txt` | the same instructions as plain text |

The tarball is the same image as the registry one, `docker save`d. Both tags
(`rss2telegram:<ver>` and `rss2telegram:latest`) are restored by `docker load`,
so the tarball path and the registry path are interchangeable.

`docker-compose.release.yml` in the repo is the template: it uses
`image: ${RSS2TG_IMAGE:-ghcr.io/alexs20/rss2telegram:latest}`, so you can track
`latest` or pin with `RSS2TG_IMAGE=ghcr.io/alexs20/rss2telegram:1.2.3`.

> **One-time step after the first release:** a GHCR package starts out **private**
> even on a public repo. If `docker pull` returns `denied`, open the package
> page -> Package settings -> Change visibility -> Public, then pull again.
> Release assets and the tarballs are unaffected and work either way.

## Cutting a release

Releases are tag-driven; nothing is published from `main`.

```bash
git tag -a v1.2.3 -m "rss2telegram 1.2.3"
git push origin v1.2.3
```

`.github/workflows/release.yml` then:

1. builds `linux/amd64` and `linux/arm64` from the same Dockerfile;
2. smoke-tests the image (`--check` against the example config, plus a check
   that it still runs as uid 10001);
3. exports one tarball per architecture;
4. pushes a multi-arch manifest to `ghcr.io/alexs20/rss2telegram` tagged
   `1.2.3`, `1.2` and `latest`;
5. creates the release and uploads the assets, with `--clobber` on re-runs, so a
   failed run can be re-triggered from the Actions tab or with
   `gh workflow run release.yml -f tag=v1.2.3`.

`.github/workflows/ci.yml` runs on every push to `main` and on pull requests:
Python syntax check, image build, the same smoke test, compose validation, and a
guard that fails if a real `bot_token` or a tracked `data/config.yml` ever
reaches the tree.

## Using a release tarball instead of the registry

For an air-gapped host, or when you would rather not pull from a registry, the
same image is attached to every release as a tarball:

```bash
# pick the arch that matches `uname -m`: x86_64 -> amd64, aarch64 -> arm64
gunzip -c rss2telegram-1.2.3-linux-amd64.tar.gz | docker load
# -> Loaded image: rss2telegram:1.2.3
#    Loaded image: rss2telegram:latest

sha256sum -c SHA256SUMS.txt

# run it exactly like the registry image
RSS2TG_IMAGE=rss2telegram:latest \
  docker compose -f docker-compose.release.yml up -d
```

`docker save` records both tags, so the loaded `rss2telegram:latest` is
interchangeable with `ghcr.io/alexs20/rss2telegram:latest` - nothing else in the
compose file or the data directory changes.

## Data and persistence

The dedupe database is `/data/rss2tg.db` inside the container, backed by the
named volume `rss2tg-data`. It survives `docker compose down`, image rebuilds
and restarts.

```bash
# inspect delivery history
docker compose exec rss2tg python -c \
  "import sqlite3;c=sqlite3.connect('/data/rss2tg.db');print(c.execute('select count(*) from sent').fetchone()[0],'items')"

# back it up
docker run --rm -v rss2telegram_rss2tg-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/rss2tg-data.tgz -C /data .
```

Deleting the volume means every currently-listed feed item counts as new again —
with `bootstrap: skip` they are silently re-recorded, not re-sent.

Why a named volume rather than `./data`: SQLite locking misbehaves on shared and
network filesystems (vboxsf, NFS, SMB). If you prefer a host bind mount, use a
real local disk path:

```yaml
volumes:
  - ./data:/data
```
and make sure it is writable by the container user.

## Behaviour details worth knowing

- **Dedupe key** is the Atom `<id>`, else RSS `<guid>`, else `<link>`, else a
  SHA-1 of title + dates. Scoped per `(feed, chat)` pair.
- **Retries** — 4 attempts per message with exponential backoff, `retry_after`
  honoured on HTTP 429, and one automatic plain-text retry if Telegram rejects
  the HTML markup with 400.
- **Ordering** — a backlog is replayed oldest-first, so a group reads in
  publication order rather than reversed.
- **Interrupted backlogs resume** — each message is committed to SQLite as soon
  as Telegram accepts it, so stopping the service mid-backlog continues with the
  remaining items on the next start rather than repeating the whole feed.
- **Isolation** — one unreachable or malformed feed logs an error and is skipped;
  the rest of the round continues. Nothing kills the loop.
- **Graceful stop** — SIGTERM finishes the in-flight message and exits (measured
  at ~0.4s when idle).
- **Health check** — the image's `HEALTHCHECK` runs `rss2tg.py --check` every
  5 minutes: it re-parses the config and opens the database. It never contacts
  Telegram, so it won't rate-limit you.

## Troubleshooting

**`unparsable feed: not well-formed (invalid token)`**
The URL returned HTML, not XML. This is what `forums.redflagdeals.com/feed/`
does — it redirects to the homepage. Use `https://forums.redflagdeals.com/feed.php`
instead. Any URL whose content starts with `<!DOCTYPE html>` is a web page, not a
feed; check with `curl -sL <url> | head -c 100`.

**`Telegram API 401 (Unauthorized)`**
The bot token is wrong or was revoked. Note this leaves no history row, so the
same item is retried after you fix the token — no message is lost.

**`Telegram API 400 ... chat not found`**
The bot was never added to the group, `chat_id` has a typo (missing `-100`), or
for a channel the bot is not an administrator.

**`config file not readable: /app/config.yml`**
The bind-mounted file is not readable by the container user. Either set
`RSS2TG_UID`/`RSS2TG_GID` in `.env` to the file's owner (`stat -c '%u:%g'
config.yml`), or `chmod o+r config.yml` on the host. On VirtualBox shared folders
(`vboxsf`) `chmod` has no effect — the mount is forced to `0770 root:vboxsf` —
which is why the compose file defaults to `user: "0:0"`.

**Nothing is sent on the first run**
Expected with `bootstrap: skip`: the first poll only records history. New items
that appear afterwards are delivered.

**Group went quiet / messages missing**
Check `docker compose logs` for `429`; if you lowered `send_delay` below ~2s and
post many items, raise it again.

**A large backlog takes a while**
Nothing is discarded, so N new items need roughly N × `send_delay` seconds
(20 items at 2s = about 40s). The log line `<feed>: N new item(s) to deliver`
states the size up front, and a long backlog resumes where it left off if the
service restarts.

## Running unprivileged

The image itself runs as uid 10001 (`USER app`). The compose file overrides that
to `0:0` by default so a bind-mounted `config.yml` from a shared folder is
readable. To run unprivileged instead:

```bash
stat -c '%u:%g' config.yml          # e.g. 1000:1000
printf 'RSS2TG_UID=1000\nRSS2TG_GID=1000\n' >> .env
docker compose down
docker volume rm rss2telegram_rss2tg-data   # recreated owned by the new uid
docker compose up -d
```

The config file must be readable by that uid, and `/data` must be writable by it.

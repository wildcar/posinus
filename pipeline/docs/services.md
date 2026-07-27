# Services: preparer and publisher

Operational reference for the two pipeline services that turn selected news into
published posts. For *what* they produce (the behavior contract) see `AGENTS/SPEC.md`,
sections «Подготовка отобранных новостей» and «Публикация». This file is *how they run*:
units, config, storage, commands, and gotchas.

Both are single-file, stdlib-only Python scripts deployed at `/opt/posinus/pipeline/`
next to `evaluator.py`, run as the `posinus-pipeline` user (supplementary group
`posinus`) by systemd oneshot services plus timers. Everything is installed and
updated by `sudo bash deploy/install.sh` (the owner runs it — agents do not create
system users).

## Pipeline at a glance

```
crawler ──> evaluator ──> preparer ──> publisher
            (score +      (retelling   (post to
             select)       + images)    platforms)
```

| Service | Script | Timer (unit) | Cadence | Calls the model? |
|---------|--------|--------------|---------|------------------|
| evaluator | `evaluator.py` | `posinus-evaluator.timer` | every 10 min | yes |
| preparer  | `preparer.py`  | `posinus-preparer.timer`  | every 15 min | yes |
| publisher | `publisher.py` | `posinus-publisher.timer` | every 30 min | no |

## Shared storage (evaluator-owned DB)

`/var/lib/posinus/pipeline/evaluator.sqlite3` (owner `posinus-pipeline`, mode 0750). The
crawler exchange contract forbids clients from writing any table but its two exchange
tables, so all prepared artifacts and publication state live here, keyed by `news_id`.

- `prepared_item(news_id PK, status, retold_title, retold_body_md, model_id, prepared_at, published_at, error)`
  — `status`: `prepared` → `published`, or `error`.
- `illustration(id, news_id, position, file_path, caption, source_url, downloaded_at)`
- `publication(news_id, platform, status, url, error, attempts, updated_at)` — one row per
  `(news_id, platform)`; `status` is `ok` or `error`.

The DB runs in WAL with a 30-second busy timeout, so a reader cannot block the write
that records an already-sent post. That write retries on a lock and, if it still fails,
drops an `unrecorded-<news_id>-<platform>-<timestamp>.txt` marker next to the DB and
fails the run: the post is public with no row, and the next run would send it twice.

Images live in `/var/lib/posinus/pipeline/media/<news_id>/`. `illustration.file_path` is
absolute, so it goes stale when the media directory moves: the posinus rename left 336 rows
pointing at `/var/lib/news-evaluator/media/...`, and those items published as text with no
picture. The publisher now falls back to `MEDIA_DIR/<news_id>/<filename>` when the stored path
is gone and logs the substitution, so a move heals itself; a picture that is missing in both
places logs a warning and the post goes out without it. There are no HTML pages: the
retelling is markdown in `prepared_item.retold_body_md`. An older DB auto-migrates on open
(`migrate_own_db` adds `retold_body_md` and backfills it from the previously stored HTML,
no model calls, no manual step).

## preparer.py

Takes selected news (`exchange_latest_reviews.decision = 'positive'`) that are not yet
prepared, in batches. For each item it re-fetches the article (respecting robots),
extracts illustrations with captions (og:image, `figure`/`figcaption`, lazy `img`), asks
the model for a fresh lively Russian retelling (JSON `{title, body[]}`), stores a markdown
document plus the downloaded images, and marks it «Подготовлено». On a per-item failure it
writes `status='error'` with the message; the item re-enters the queue on the next run.

Config (in `/etc/posinus/pipeline.env`):

- `PREP_BATCH` (default 5) — selected news prepared per run.
- `EVALUATOR_DB_PATH`, `MEDIA_DIR`, `NEWS_DB_PATH`, `PREPARER_USER_AGENT`.
- Model routing: `ROUTER_AUTH_TOKEN`, `ROUTER_MCP_URL`, `EVALUATOR_PROVIDER`,
  `EVALUATOR_MODEL` (empty → router picks), `EVALUATOR_TIER`.
- `PREPARER_ROUTER_USER_ID` (default `news-preparer`) — `external_user_id` of the retelling
  calls, so the router does not bill them to `news-evaluator`.

## publisher.py

Takes prepared news (`prepared_item.status = 'prepared'`) and posts each to every
configured platform, rendering the platform-specific format from the stored markdown. No
model calls. Reads only the evaluator's own DB. A `(news_id, platform)` send is recorded
in `publication`; a re-run skips platforms already `ok` and retries only failures. When
all enabled platforms are settled the item is marked «Опубликовано».

Platforms — each turns on only when its secret is present, otherwise it is skipped (so the
timer runs harmlessly until at least one is configured):

| Platform | How | Required config |
|----------|-----|-----------------|
| `wildcar_org` | writes the page + pictures into the content dir, regenerates the section index and the Dzen RSS feed, touches the rebuild marker, waits until the page is live (see below) | `WILDCAR_ORG_BASE_URL`; `WILDCAR_ORG_CONTENT_DIR`, `WILDCAR_ORG_SECTION`, `WILDCAR_ORG_WAIT_SECONDS` |
| `telegram` | `sendMessage` with the FULL retelling (≤4096 visible chars) to @posinus; the picture is a link preview pointing at its wildcar.org copy. Without `wildcar_org`: `sendPhoto` + caption (≤1024) | `TELEGRAM_BOT_TOKEN`; `TELEGRAM_CHAT_ID` (default `-1003795927410`), `TELEGRAM_CHANNEL_USERNAME` |
| `site` | wildcar.ru on Эгея: login → image upload → `note-process` → `note-publish` → verify | `EGEYA_PASSWORD` (login `EGEYA_LOGIN`, default `wildcar`); `EGEYA_BASE_URL`, `EGEYA_TAGS` |
| `vk` | community wall: photo upload + `wall.post` from the group | `VK_ACCESS_TOKEN` **and** `VK_GROUP_ID`; `VK_API_VERSION` |

`wildcar_org` runs first in the platform order on purpose: telegram links its picture
from the wildcar.org copy, so within one run the page must already be live. While it is
not (the build has not caught up, or the platform is broken), telegram records an error
and is retried on the next run.

### wildcar.org and Дзен

wildcar.org is a static MkDocs site built from `~keeper/repo/wildcar-site` into
`/var/www/wildcar.org` **on this host**. The publisher cannot run MkDocs (different user,
different venv), so the handshake goes through files:

1. `publish_wildcar_org` writes `news/<news_id>/index.md` plus the pictures into
   `WILDCAR_ORG_CONTENT_DIR` (default `/var/lib/posinus/wildcar-org`), regenerates
   `news/index.md` (the section index), `news/.nav.yml` and `news/rss.xml` from the own
   DB, and touches `rebuild-wildcar-org` in the request mailbox.
2. `posinus-wildcar-org-build.path` sees the marker and starts
   `posinus-wildcar-org-build.service` (user `keeper`, `SupplementaryGroups=posinus`),
   which runs `deploy/wildcar-org-build.sh`: rsync the content into the site checkout
   (`docs/news/`, gitignored there) and `mkdocs build` into `/var/www/wildcar.org`.
3. The publisher polls `https://wildcar.org/news/<news_id>/` for up to
   `WILDCAR_ORG_WAIT_SECONDS` (90) and only then reports the platform `ok`.

Everything under `news/` in the content dir is regenerated on retries, so a
half-finished run heals itself. Unlike the other platforms nothing is "sent": deleting a
page means deleting its directory from the content dir and touching the marker.

**Дзен has no posting API** — it polls `https://wildcar.org/news/rss.xml` (every 2–5
minutes) once the feed is connected in the channel settings. The feed carries the last
30 items with the full text in `content:encoded`, absolute picture URLs, and the fields
Дзен requires (title without a trailing period, link, guid, RFC-822 pubDate, category).
So dzen.ru gets the news by publishing to `wildcar_org`; there is no `dzen` platform in
the `publication` table. Дзен ignores items older than 7 days and treats a changed
`guid` as a new post — the guid is the page URL, which never changes.

Pacing and robustness (config):

- `PUB_BATCH` (default 1) — max **new** items started per run. Retries of already-public
  items are not limited by this.
- `PUB_MIN_INTERVAL_MINUTES` (default 120, **60 on prod** since 2026-07-25) — a **new** item
  is published at most this often, measured from the last successful post to any platform.
  Finishing an already-public item on its remaining platforms is not throttled (it is the
  same news). Prod runs 60 because the prepared backlog reached 118 items.
- `MEDIA_DIR` — where the preparer put the images; the publisher uses it to find an
  illustration whose stored absolute path no longer exists.
- `PUB_MAX_ATTEMPTS` (default 8) — a failing platform is retried this many times, then
  given up on; the item is finalized «Опубликовано» best-effort with whatever platforms
  succeeded. This is why a broken platform can never block the rest of the queue.

- `PUB_WINDOW_START` / `PUB_WINDOW_END` / `PUB_WINDOW_TZ` (default `08:00`, `22:00`,
  `Europe/Moscow`) — a **new** item only appears inside this local window; a post at 03:40 is
  lost reach. A start later than the end wraps past midnight; an empty start switches the
  window off. Retries of an already-public item ignore the window.
- `REQUESTS_DIR` (default `/var/lib/posinus/pipeline/requests`) — the request mailbox, see
  below.

The service exits 0 even when some platform sends failed (they are recorded and retried),
so systemd does not flip to `failed` on transient errors.

### The stop cock and the request mailbox

`REQUESTS_DIR` is a directory the web UI can write to (owner `posinus-pipeline`, group
`posinus`, mode 2770 — `deploy/install.sh` creates it). It carries two things.

**`pause` — the stop cock.** While the file exists the publisher sends nothing at all, not
even a retry of a half-published item, and the queue simply grows. Lines inside:

```text
until=2026-07-25T23:00:00+03:00
reason=день траура
```

`until` is optional: without it the pause holds until the file is removed. The publisher
deletes the file itself once `until` has passed. A file it cannot read, or one with an
unparsable `until`, counts as an active pause — a safety catch that fails towards «send» is
not a safety catch. Written by hand:

```bash
sudo -u posinus tee /var/lib/posinus/pipeline/requests/pause <<'EOF'
reason=разбираемся с площадкой
EOF
```

**`run-evaluator` / `run-preparer` / `run-publisher` / `run-evaluator-backfill` — run now.** A
`posinus-<name>-run.path` unit watches for the file and starts the matching service within a
second; the service removes the file in `ExecStartPre` before doing any work, so the path unit
does not retrigger and a second click during a run is ignored by systemd. This is how the
operator UI runs a service without sudo and without a second job queue.

`posinus-apply-edits.service` has no timer either: `posinus-apply-edits-run.path` watches for
`edit-*.json` and runs `apply_edits.py`, which reads each request, applies it and removes the
file itself (no `ExecStartPre` here — deleting the requests up front would lose the edits).

`posinus-evaluator-backfill.service` has no timer: it exists only for that last request file.
It runs `evaluator.py --backfill --rescore-all`, which re-applies the selection thresholds in
force to every news item already scored and writes a correcting event only where the verdict
changed. No model calls, so a full recomputation of the corpus costs seconds and no money.

### VK: the token type matters

Wall posting needs a **classic user access token** of a group admin (the token string
starts with `vk1.`, scope `photos,wall,groups`). The other token types are dead ends, and
each fails with its own error code — worth knowing so the code tells you which mistake you
made:

- **community** token (the one the community admin page hands you, so the easy wrong turn)
  → `wall.post` error 214 and `photos.getWallUploadServer` error 27, both "method is
  unavailable with group auth".
- **VK ID** token (string starts with `vk2.a.`, issued by the `id.vk.ru` OAuth 2.1 / PKCE
  flow — i.e. "Log in with VK") → error 1051 "method is unavailable with current profile
  type". It authenticates a person; it does not call VK API methods at all.

Getting a classic token is the awkward part in 2025+: VK ID no longer mints API-capable
tokens for freshly created apps, and the old `oauth.vk.ru` endpoint rejects apps created on
`id.vk.ru`. The route that still works is the legacy implicit flow through a grandfathered
app_id — Kate Mobile (`2685278`) — authorized in a browser by the group admin. It returns a
non-expiring `vk1.` token that `wall.post` and `photos.*` accept:

    https://oauth.vk.ru/authorize?client_id=2685278&scope=wall,groups,photos,offline&response_type=token&display=page&redirect_uri=https://oauth.vk.ru/blank.html&v=5.199

The token lands in the address bar after the redirect to `blank.html`. Put it in
`VK_ACCESS_TOKEN` (with `VK_GROUP_ID`, the positive numeric id). It is broad-scoped and
long-lived — treat it like a password, keep it only in the env file.

If a user token is ever unavailable, switch VK to a link-card post (attach the wildcar.ru
article URL, which works with a community token) or blank `VK_ACCESS_TOKEN` to disable VK.
A failing VK does not hold up Telegram or the site — it is retried `PUB_MAX_ATTEMPTS` times
and given up on.

## notify.py

Two timers, one script. `posinus-notify.timer` runs `notify.py` hourly and sends an alarm only
for a platform that failed at least three times, a full day with no post inside an open window,
or a queue empty for three days. `posinus-notify-digest.timer` sends one sentence at 09:00
Moscow time. The same alarm is not repeated within 12 hours (`notification` table).

`NOTIFY_CHAT_ID` is the owner's own chat — never the public channel, because this carries
diagnostics. With it empty nothing is sent at all, which is the safe default. `--dry-run` builds
the message and logs it without sending.

## Operational commands

```bash
# status of all three timers
systemctl list-timers 'news-*.timer'

# recent logs
sudo journalctl -u posinus-preparer.service -n 50
sudo journalctl -u posinus-publisher.service -n 50

# run one batch right now
sudo systemctl start posinus-preparer.service
sudo systemctl start posinus-publisher.service

# preview without side effects (env must be loaded for tokens / enabled platforms)
sudo -u posinus-pipeline bash -c 'set -a; . /etc/posinus/pipeline.env; set +a; \
  python3 /opt/posinus/pipeline/publisher.py --dry-run --news-id N'

# publish one specific item now, ignoring the rate limit
sudo -u posinus-pipeline bash -c 'set -a; . /etc/posinus/pipeline.env; set +a; \
  python3 /opt/posinus/pipeline/publisher.py --news-id N'

# inspect state
sudo python3 - <<'PY'
import sqlite3
c = sqlite3.connect('/var/lib/posinus/pipeline/evaluator.sqlite3'); c.row_factory = sqlite3.Row
for r in c.execute("SELECT status, COUNT(*) n FROM prepared_item GROUP BY status"):
    print(r['status'], r['n'])
for r in c.execute("SELECT news_id, platform, status, attempts FROM publication ORDER BY updated_at DESC LIMIT 10"):
    print(r['news_id'], r['platform'], r['status'], 'attempts=' + str(r['attempts']))
PY
```

Edits to `/etc/posinus/pipeline.env` apply on the next timer run — no
restart, no redeploy.

## Tuning

- Faster or slower publishing: `PUB_MIN_INTERVAL_MINUTES` (cadence of new posts).
- Drain a backlog quickly: lower `PUB_MIN_INTERVAL_MINUTES` temporarily.
- Silence a persistently failing platform sooner: lower `PUB_MAX_ATTEMPTS`, or blank its
  secret to disable it.

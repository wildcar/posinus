# Services: preparer and publisher

Operational reference for the two pipeline services that turn selected news into
published posts. For *what* they produce (the behavior contract) see `AGENTS/SPEC.md`,
sections «Подготовка отобранных новостей» and «Публикация». This file is *how they run*:
units, config, storage, commands, and gotchas.

Both are single-file, stdlib-only Python scripts deployed at `/opt/news-evaluator/`
next to `evaluator.py`, run as the `newsevaluator` user (supplementary group
`newscrawler`) by systemd oneshot services plus timers. Everything is installed and
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
| evaluator | `evaluator.py` | `news-evaluator.timer` | every 10 min | yes |
| preparer  | `preparer.py`  | `news-preparer.timer`  | every 15 min | yes |
| publisher | `publisher.py` | `news-publisher.timer` | every 30 min | no |

## Shared storage (evaluator-owned DB)

`/var/lib/news-evaluator/evaluator.sqlite3` (owner `newsevaluator`, mode 0750). The
crawler exchange contract forbids clients from writing any table but its two exchange
tables, so all prepared artifacts and publication state live here, keyed by `news_id`.

- `prepared_item(news_id PK, status, retold_title, retold_body_md, model_id, prepared_at, published_at, error)`
  — `status`: `prepared` → `published`, or `error`.
- `illustration(id, news_id, position, file_path, caption, source_url, downloaded_at)`
- `publication(news_id, platform, status, url, error, attempts, updated_at)` — one row per
  `(news_id, platform)`; `status` is `ok` or `error`.

Images live in `/var/lib/news-evaluator/media/<news_id>/`. There are no HTML pages: the
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

Config (in `/etc/news-evaluator/news-evaluator.env`):

- `PREP_BATCH` (default 5) — selected news prepared per run.
- `EVALUATOR_DB_PATH`, `MEDIA_DIR`, `NEWS_DB_PATH`, `PREPARER_USER_AGENT`.
- Model routing: `ROUTER_AUTH_TOKEN`, `ROUTER_MCP_URL`, `EVALUATOR_PROVIDER`,
  `EVALUATOR_MODEL` (empty → router picks), `EVALUATOR_TIER`.

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
| `telegram` | `sendPhoto` + HTML caption (≤1024 chars) to @posinus | `TELEGRAM_BOT_TOKEN`; `TELEGRAM_CHAT_ID` (default `-1003795927410`), `TELEGRAM_CHANNEL_USERNAME` |
| `site` | wildcar.ru on Эгея: login → image upload → `note-process` → `note-publish` → verify | `EGEYA_PASSWORD` (login `EGEYA_LOGIN`, default `wildcar`); `EGEYA_BASE_URL`, `EGEYA_TAGS` |
| `vk` | community wall: photo upload + `wall.post` from the group | `VK_ACCESS_TOKEN` **and** `VK_GROUP_ID`; `VK_API_VERSION` |

Pacing and robustness (config):

- `PUB_BATCH` (default 1) — max **new** items started per run. Retries of already-public
  items are not limited by this.
- `PUB_MIN_INTERVAL_MINUTES` (default 120) — a **new** item is published at most this
  often, measured from the last successful post to any platform. Finishing an
  already-public item on its remaining platforms is not throttled (it is the same news).
- `PUB_MAX_ATTEMPTS` (default 8) — a failing platform is retried this many times, then
  given up on; the item is finalized «Опубликовано» best-effort with whatever platforms
  succeeded. This is why a broken platform can never block the rest of the queue.

The service exits 0 even when some platform sends failed (they are recorded and retried),
so systemd does not flip to `failed` on transient errors.

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

## Operational commands

```bash
# status of all three timers
systemctl list-timers 'news-*.timer'

# recent logs
sudo journalctl -u news-preparer.service -n 50
sudo journalctl -u news-publisher.service -n 50

# run one batch right now
sudo systemctl start news-preparer.service
sudo systemctl start news-publisher.service

# preview without side effects (env must be loaded for tokens / enabled platforms)
sudo -u newsevaluator bash -c 'set -a; . /etc/news-evaluator/news-evaluator.env; set +a; \
  python3 /opt/news-evaluator/publisher.py --dry-run --news-id N'

# publish one specific item now, ignoring the rate limit
sudo -u newsevaluator bash -c 'set -a; . /etc/news-evaluator/news-evaluator.env; set +a; \
  python3 /opt/news-evaluator/publisher.py --news-id N'

# inspect state
sudo python3 - <<'PY'
import sqlite3
c = sqlite3.connect('/var/lib/news-evaluator/evaluator.sqlite3'); c.row_factory = sqlite3.Row
for r in c.execute("SELECT status, COUNT(*) n FROM prepared_item GROUP BY status"):
    print(r['status'], r['n'])
for r in c.execute("SELECT news_id, platform, status, attempts FROM publication ORDER BY updated_at DESC LIMIT 10"):
    print(r['news_id'], r['platform'], r['status'], 'attempts=' + str(r['attempts']))
PY
```

Edits to `/etc/news-evaluator/news-evaluator.env` apply on the next timer run — no
restart, no redeploy.

## Tuning

- Faster or slower publishing: `PUB_MIN_INTERVAL_MINUTES` (cadence of new posts).
- Drain a backlog quickly: lower `PUB_MIN_INTERVAL_MINUTES` temporarily.
- Silence a persistently failing platform sooner: lower `PUB_MAX_ATTEMPTS`, or blank its
  secret to disable it.

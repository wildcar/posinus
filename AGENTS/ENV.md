# Environment

Host facts, tools, credential pointers and the command cheat-sheet. One machine runs
everything. Update whenever a new tool, credential or host quirk is learned.

Per-service operational detail lives closer to the code: `pipeline/docs/services.md` for the
pipeline units, timers and their gotchas; `docs/deployment.md` for the full install.

## Host

- **Dev**: Linux (kernel 6.8), bash, user `keeper`, repository at `/home/keeper/repo/posinus`.
- **Prod**: the same machine. Checkout at `/opt/posinus`, config in `/etc/posinus`, shared
  state in `/var/lib/posinus`, crawler logs in `/var/log/posinus`.
- The crawler also supports Windows for development; `crawler/scripts/install.ps1` and
  `register-windows-tasks.ps1` exist for that. CI covers Ubuntu and Windows.
- `/home/keeper` is `750`, so service users cannot read the repository. Prod runs from
  `/opt/posinus`, never from the home checkout.

## Service accounts

| Account | Belongs to | Runs |
|---|---|---|
| `posinus` | group `posinus` | `posinus-web.service`, `posinus-worker.service` |
| `posinus-pipeline` | own group + supplementary `posinus` | the three pipeline timers |

The `posinus` group is what grants access to the crawler database. Clients run with
`umask 0007` so the SQLite `-wal` and `-shm` sidecars stay group-accessible.

## Tools

- git; commit identity `wildcar <wildcar@mail.ru>`; GitHub push via `gh` (account `wildcar`).
- Python `>=3.12,<3.15`. The crawler venv lives at `/opt/posinus/crawler/.venv`; the pipeline
  uses `/usr/bin/python3` directly.
- Playwright Chromium at `/var/lib/posinus/playwright`, installed with `--with-deps`.
- ffmpeg at `/usr/bin/ffmpeg` (distro package): `preparer.shrink_image` shells out to it
  to re-encode heavy illustrations — the one non-Python binary the pipeline depends on.
- model-router-mcp: MCP server for model access, unit `model-router-mcp.service`, Streamable
  HTTP endpoint `http://127.0.0.1:8088/mcp`, deployed at `/opt/model-router-mcp`, sources at
  `~/repo/model-router-mcp`. Registered deepseek models: `deepseek-v4-pro`, `deepseek-v4-flash`. DeepSeek retired `deepseek-chat` and `deepseek-reasoner` around 2026-07-25 and its API now rejects both names; `bootstrap.py` in model-router-mcp still seeds the retired pair, so the working entry is a manual registry row.
- wildcar.org: static MkDocs (Material) site served by nginx from `/var/www/wildcar.org` on
  THIS host (vhost `web.wildcar.org`; hel-vps terminates TLS for `wildcar.org` and proxies
  here). Sources at `~/repo/wildcar-site`, venv `~/.venvs/mkdocs`, built by keeper. The
  pipeline's `wildcar_org` platform writes news into `/var/lib/posinus/wildcar-org` and
  `posinus-wildcar-org-build.service` (user keeper + group posinus) rsyncs and rebuilds; the
  news RSS at `https://wildcar.org/news/rss.xml` feeds Дзен.

## Credentials & secrets

- Router Bearer token: `AUTH_TOKEN` in `/opt/model-router-mcp/.env` (root-readable via sudo).
  The crawler reads it as `POSINUS_ROUTER_AUTH_TOKEN`, the pipeline as `ROUTER_AUTH_TOKEN`.
  `pipeline/deploy/install.sh` copies it in automatically. Never commit it.
- The `codex-oauth` provider DROPS the requested image size (learned 2026-07-30). The
  router passes `params.size` into the `image_generation` tool correctly — router
  `request_logs` 8618 asked `1024x1536` — and the picture came back 1536x1024 anyway,
  as did every other portrait request through this provider. The model driving the
  Codex `/responses` call picks the canvas itself and it reads the PROMPT: the same
  request with «Вертикальный портретный кадр, ориентация 2:3» in the prompt text
  returned a real 1024x1536. So any caller that cares about orientation must say it in
  words (`daypic.ORIENTATIONS` does); `params.size` alone is not enough. Fixing this
  inside model-router-mcp is still open.
- Codex OAuth for the router's `codex-oauth` provider (image generation): `CODEX_AUTH_PATH`
  in the router's `.env` points at `/var/lib/model-router/home/.codex/auth.json` (owner
  `modelrouter`, 0600). Since 2026-07-29 this is a SEPARATE login session, its own
  refresh-token chain distinct from keeper's `~/.codex` and hermes, so their refreshes
  cannot kill it; the owner just re-logs it in occasionally when it expires. Symptom of an
  expired credential: `HTTP 401 access_denied` on token refresh in the preparer/router
  logs — news keep publishing, only without generated pictures. Refreshing the file is the
  owner's operation. Do NOT resurrect `~/repo/openai-auth-sync` for this: the project is
  closed because syncing one token across several holders is detected by Codex and gets
  the token banned — one login per consumer is the working model.
- Platform secrets (Telegram bot token, Эгея password, VK token) live only in
  `/etc/posinus/pipeline.env`, `root:posinus-pipeline`, mode `0640`.
- Django secret key and the operator password stay out of the repository.
- Local `.env*` files are gitignored and must not be committed.

## Environments

| Env | Identifier | Account | Where used |
|-----|------------|---------|------------|
| dev | `/home/keeper/repo/posinus` | `keeper` | development, both test suites |
| prod | `/opt/posinus`, `/var/lib/posinus/posinus.sqlite3` | `posinus`, `posinus-pipeline` | live crawl, scoring, publishing |

## Commands cheat-sheet

### Dev — crawler

```bash
cd crawler
python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createoperator operator
.venv/bin/python -m waitress --listen=127.0.0.1:8000 posinus_crawler.wsgi:application
.venv/bin/python manage.py runworker
.venv/bin/python -m pytest
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
```

### Dev — pipeline

```bash
cd pipeline
python3 -m unittest discover -s tests
ROUTER_AUTH_TOKEN=... python3 evaluator.py --dry-run --limit 1
ROUTER_AUTH_TOKEN=... python3 preparer.py --dry-run --news-id N
python3 publisher.py --dry-run --news-id N
```

### Prod

```bash
# update both services: crawler venv, migrations, and pipeline code in one pull
sudo /opt/posinus/crawler/scripts/update-ubuntu.sh

# pipeline units and config only; the owner runs it, it creates the service user
sudo bash /opt/posinus/pipeline/deploy/install.sh

# status
sudo systemctl is-active posinus-web.service posinus-worker.service
systemctl list-timers 'posinus-*.timer'
sudo journalctl -u posinus-publisher.service -n 50
sudo systemctl start posinus-evaluator.service
sudo sqlite3 /var/lib/posinus/posinus.sqlite3 'PRAGMA integrity_check;'

# model and batch tuning: edit /etc/posinus/pipeline.env, applies on the next timer run
```

Manual pipeline batch as the service user:

```bash
TOKEN=$(sudo grep '^AUTH_TOKEN=' /opt/model-router-mcp/.env | cut -d= -f2-)
sudo -u posinus-pipeline env ROUTER_AUTH_TOKEN="$TOKEN" \
  bash -c 'umask 0007; python3 /opt/posinus/pipeline/evaluator.py --limit 3'
```

## Host-specific quirks

### Dev

- `127.0.0.1:8000` is the crawler web UI (waitress, redirects to https); the model router is
  on `8088`. Do not confuse the two.
- The SQLite database must stay on a local, non-synchronized disk.
- `crawler/tests/test_ui.py` reads `deploy/systemd/posinus-web.service` by relative path, so
  run pytest from `crawler/`.

### Prod

- SQLite sidecar files (`-wal`, `-shm`) must stay group-accessible: clients run with
  `umask 0007`. See `docs/contracts/database-contract.md`.
- Set `POSINUS_SECURE=1` only after HTTPS termination is configured.
- `newscrawler.wildcar.org` is the live operator UI hostname with a Let's Encrypt
  certificate. It kept its old name on purpose: renaming it means DNS plus a new certificate.
- FastMCP redirects `/mcp` to `/mcp/` with 307; urllib does not re-POST on redirects, so the
  pipeline's client follows 307/308 manually.
- The router's deepseek adapter forwards only `temperature`, `max_tokens` and `top_p`.
  `response_format` (JSON mode) never reaches the provider, so strict JSON relies on the
  prompt plus validation.
- Stop every direct SQLite client before migrations or a database restore. Their units are
  listed in `/etc/posinus/update-services`.
- The dead NodeSource apt repo (`deb.nodesource.com` answered 403 since 2026-07-27, failing
  `apt-get update` and therefore the Playwright dependency step of `update-ubuntu.sh`) was
  removed from the host on 2026-07-27: nothing was installed from it since apt nodejs was
  purged on 2026-04-23 — node exists only via keeper's nvm. `update-ubuntu.sh` was verified
  working end-to-end the same day. Pipeline-only changes can still be shipped with
  `sudo git -C /opt/posinus pull --ff-only` while the pipeline services are inactive
  (they are oneshots and run in place; no venv, no migrations).

# pipeline

Scoring, selection, preparation and publishing. Reads every news item collected by the
crawler (`../crawler/`, through its `exchange_*` SQLite contract), scores it on a fixed set
of 20 characteristics (integer 0–10 each, independent axes), and a selection profile decides
which items pass on. Selected news are turned into a markdown retelling and then posted to
the platforms.

Three stdlib-only scripts (Python 3.12):

- `evaluator.py` — scores a batch, applies the `default` selection profile, and writes
  a review event (positive/not_positive) plus 20 scores per news in one transaction.
  `--backfill` re-verdicts already-scored news from stored scores. The `default`
  profile is strict (few items pass) — see `AGENTS/SPEC.md`, section «Пороговая модель».
- `preparer.py` — for each selected news, extracts illustrations with captions from the
  article, asks the model for a fresh lively Russian retelling, and stores it as a
  markdown document in the pipeline's own SQLite plus a media dir.
- `publisher.py` — posts each prepared news to the wildcar.org news section (a static
  MkDocs site on the same host, rebuilt by a systemd unit; carries the Дзен-ready RSS
  feed), Telegram (@posinus, photo + caption capped for the Дзен autopublisher, with a
  full-text link to wildcar.org when truncated), the wildcar.ru site (Эгея), and a VK
  community wall, fully automatically by timer. Each platform turns on only when its
  secret is set in the env file; idempotent per platform, marks «Опубликовано» when all
  enabled platforms succeed.

Plus `daypic.py` — «Картина дня»: a daily generated picture drawn twice from one
prompt (vertical for telegram, horizontal for the sites and VK) in a random style
that never repeats within the month, posted with a date-and-holidays caption to
wildcar.org (its own `kartina` section), telegram, the Эгея site and VK through the
publisher's adapters. The operator edits its prompt, styles and schedule on the
crawler's «Картина дня» page; the vertical file lands in
`/var/lib/posinus/pipeline/daypic/<date>-<slot>.<ext>` for external pickup.

The model is configured in `/etc/posinus/pipeline.env` (never hard-coded;
each event records the model that actually answered).

```bash
python3 -m unittest discover -s tests        # unit tests (no network, no DB)
python3 evaluator.py --backfill --dry-run    # re-verdict old scored news, print only
python3 preparer.py --dry-run --news-id N    # prepare one selected news, print only
python3 preparer.py --ignore-image URL       # blacklist an image (e.g. a source's logo)
python3 preparer.py --review-images          # vision-check queued pictures, drop the junk
python3 publisher.py --dry-run --news-id N   # build the posts, send nothing
sudo bash deploy/install.sh                  # host install: user, config, systemd timers
```

Host run commands live in `../AGENTS/ENV.md`.

- Product spec: `AGENTS/SPEC.md` (in Russian)
- Agent workflow: `AGENTS.md`; repository map and service boundaries: `../AGENTS.md`

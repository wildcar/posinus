#!/usr/bin/env bash
# One-shot production migration from the split newscrawler / news-evaluator layout to the
# merged posinus layout. Run once, by the server owner:
#
#   sudo bash migrate-to-posinus.sh              # dry run: prints the plan, changes nothing
#   sudo bash migrate-to-posinus.sh --apply      # performs the migration
#
# Run it from a development checkout, NOT from /opt/posinus: this script is what creates
# /opt/posinus, by cloning REPO_URL there. It does not read anything from its own directory.
#
#   sudo bash ~keeper/repo/posinus/crawler/deploy/migrate-to-posinus.sh --apply
#
# It renames the service group and users (the GIDs and UIDs are kept, so file ownership
# follows automatically), moves /opt, /etc, /var/lib and /var/log into the posinus names,
# rewrites both env files, and installs the renamed systemd units.
#
# The crawler venv is rebuilt rather than moved: a venv hardcodes its own absolute path in
# the interpreter shebangs, so moving it silently breaks pip and every console script.
#
# What it deliberately does NOT change:
#   - selector_name 'news-evaluator' inside the database (renaming it would re-queue the
#     whole reviewed corpus for evaluation);
#   - the newscrawler.wildcar.org hostname, its DNS record and its TLS certificate;
#   - the Django app label 'collector'.
set -Eeuo pipefail

REPO_URL="${REPO_URL:-https://github.com/wildcar/posinus.git}"
BRANCH="${BRANCH:-main}"

OLD_APP=/opt/newscrawler
OLD_PIPE_APP=/opt/news-evaluator
NEW_ROOT=/opt/posinus

OLD_ETC=/etc/newscrawler
OLD_PIPE_ETC=/etc/news-evaluator
NEW_ETC=/etc/posinus

OLD_LIB=/var/lib/newscrawler
OLD_PIPE_LIB=/var/lib/news-evaluator
NEW_LIB=/var/lib/posinus

OLD_LOG=/var/log/newscrawler
NEW_LOG=/var/log/posinus

OLD_DB="$OLD_LIB/newscrawler.sqlite3"
NEW_DB="$NEW_LIB/posinus.sqlite3"

OLD_UNITS=(newscrawler-web.service newscrawler-worker.service
           news-evaluator.service news-evaluator.timer
           news-preparer.service news-preparer.timer
           news-publisher.service news-publisher.timer)

APPLY=0
case "${1:-}" in
    --apply) APPLY=1 ;;
    --dry-run|"") APPLY=0 ;;
    *) echo "Usage: $0 [--dry-run|--apply]" >&2; exit 2 ;;
esac

log()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
run()  {
    if (( APPLY )); then
        "$@"
    else
        printf '  would run: %s\n' "$*"
    fi
}

if [[ $EUID -ne 0 ]]; then
    echo "Run this script through sudo." >&2
    exit 1
fi
for command in git groupmod usermod install sqlite3 systemctl; do
    command -v "$command" >/dev/null || { echo "Missing command: $command" >&2; exit 1; }
done

# --------------------------------------------------------------------------------------
step "Preflight"

[[ -d $OLD_LIB ]]   || { echo "$OLD_LIB is missing: nothing to migrate." >&2; exit 1; }
[[ -f $OLD_DB ]]    || { echo "$OLD_DB is missing." >&2; exit 1; }
[[ -d $OLD_ETC ]]   || { echo "$OLD_ETC is missing." >&2; exit 1; }
getent group newscrawler >/dev/null || { echo "group newscrawler is missing." >&2; exit 1; }

for path in "$NEW_ROOT" "$NEW_ETC" "$NEW_LIB" "$NEW_LOG"; do
    if [[ -e $path ]]; then
        echo "$path already exists — migration looks done or half-done. Inspect it by hand." >&2
        exit 1
    fi
done

log "  crawler DB:      $OLD_DB"
log "  crawler group:   $(getent group newscrawler)"
if id -u newsevaluator >/dev/null 2>&1; then
    log "  pipeline user:   $(id newsevaluator)"
    HAVE_PIPELINE=1
else
    log "  pipeline user:   absent (pipeline was never installed)"
    HAVE_PIPELINE=0
fi

integrity=$(sqlite3 "$OLD_DB" 'PRAGMA integrity_check;')
[[ $integrity == ok ]] || { echo "Database integrity check failed before we start: $integrity" >&2; exit 1; }
log "  integrity:       ok"

# --------------------------------------------------------------------------------------
step "Stop and disable the old units"

active_units=()
for unit in "${OLD_UNITS[@]}"; do
    if systemctl list-unit-files "$unit" >/dev/null 2>&1 && \
       [[ -n $(systemctl list-unit-files --no-legend "$unit" 2>/dev/null) ]]; then
        active_units+=("$unit")
    fi
done
log "  present: ${active_units[*]:-none}"
if (( ${#active_units[@]} > 0 )); then
    run systemctl stop "${active_units[@]}"
    run systemctl disable "${active_units[@]}"
fi

# --------------------------------------------------------------------------------------
step "Checkpoint and back up the database"

# With every client stopped, fold the WAL into the main file so the move carries no
# dependency on the sidecars.
run sqlite3 "$OLD_DB" 'PRAGMA wal_checkpoint(TRUNCATE);'
backup="$OLD_LIB/backups/pre-posinus-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
run install -d -o newscrawler -g newscrawler -m 2770 "$OLD_LIB/backups"
run sqlite3 "$OLD_DB" ".backup '$backup'"
log "  backup: $backup"

# --------------------------------------------------------------------------------------
step "Rename the service principals (UIDs and GIDs are preserved)"

run groupmod -n posinus newscrawler
run usermod -l posinus -d "$NEW_LIB" newscrawler
if (( HAVE_PIPELINE )); then
    run groupmod -n posinus-pipeline newsevaluator
    run usermod -l posinus-pipeline newsevaluator
fi

# --------------------------------------------------------------------------------------
step "Move state, config and logs"

run mv "$OLD_LIB" "$NEW_LIB"
# The database, its WAL/SHM sidecars and the worker lock are all named after the old
# database file. Rename them inside the moved directory. In a dry run the directory has
# not moved, so probe the old location to report the same set of files.
probe_dir=$( (( APPLY )) && echo "$NEW_LIB" || echo "$OLD_LIB" )
for suffix in "" -wal -shm; do
    if [[ -e "$probe_dir/newscrawler.sqlite3$suffix" ]]; then
        run mv "$NEW_LIB/newscrawler.sqlite3$suffix" "$NEW_DB$suffix"
    fi
done
if [[ -e "$probe_dir/newscrawler.worker.lock" ]]; then
    run mv "$NEW_LIB/newscrawler.worker.lock" "$NEW_LIB/posinus.worker.lock"
fi

if [[ -d $OLD_PIPE_LIB ]]; then
    # Legacy 'pages' directory from the pre-markdown publisher rides along; nothing reads
    # it anymore, but deleting other people's data is not this script's job.
    run mv "$OLD_PIPE_LIB" "$NEW_LIB/pipeline"
fi

[[ -d $OLD_LOG ]] && run mv "$OLD_LOG" "$NEW_LOG"
run mv "$OLD_ETC" "$NEW_ETC"

# --------------------------------------------------------------------------------------
step "Rewrite the crawler env file"

old_env="$NEW_ETC/newscrawler.env"
new_env="$NEW_ETC/crawler.env"
if (( APPLY )); then
    sed -e 's/^NEWSCRAWLER_/POSINUS_/' \
        -e "s#$OLD_LIB/newscrawler\.sqlite3#$NEW_DB#g" \
        -e "s#$OLD_LIB#$NEW_LIB#g" \
        -e "s#$OLD_LOG#$NEW_LOG#g" \
        "$old_env" > "$new_env"
    chown root:posinus "$new_env"
    chmod 0640 "$new_env"
    rm -f "$old_env"

    # A missing POSINUS_DB_PATH makes Django fall back to a fresh empty dev database,
    # which would look like total data loss. Fail loudly instead.
    for key in POSINUS_DB_PATH POSINUS_BACKUP_DIR POSINUS_LOG_DIR PLAYWRIGHT_BROWSERS_PATH; do
        grep -q "^$key=" "$new_env" || { echo "$key is missing from $new_env" >&2; exit 1; }
    done
    grep -Fxq "POSINUS_DB_PATH=$NEW_DB" "$new_env" || {
        echo "POSINUS_DB_PATH in $new_env is not $NEW_DB" >&2; exit 1; }
    log "  wrote $new_env and verified the production paths"
else
    log "  would rewrite $old_env -> $new_env (NEWSCRAWLER_ -> POSINUS_, paths updated)"
    log "  would verify POSINUS_DB_PATH=$NEW_DB is present"
fi

step "Rewrite the pipeline env file"

if [[ -d $OLD_PIPE_ETC ]] || [[ -f $NEW_ETC/news-evaluator.env ]]; then
    old_pipe_env="$OLD_PIPE_ETC/news-evaluator.env"
    new_pipe_env="$NEW_ETC/pipeline.env"
    if (( APPLY )); then
        sed -e "s#$OLD_LIB/newscrawler\.sqlite3#$NEW_DB#g" \
            -e "s#$OLD_PIPE_LIB#$NEW_LIB/pipeline#g" \
            "$old_pipe_env" > "$new_pipe_env"
        chown root:posinus-pipeline "$new_pipe_env"
        chmod 0640 "$new_pipe_env"
        rm -rf "$OLD_PIPE_ETC"
        grep -Fxq "NEWS_DB_PATH=$NEW_DB" "$new_pipe_env" || {
            echo "NEWS_DB_PATH in $new_pipe_env is not $NEW_DB" >&2; exit 1; }
        # SELECTOR_NAME must survive untouched: it keys the existing review events.
        grep -Fxq 'SELECTOR_NAME=news-evaluator' "$new_pipe_env" || {
            echo "SELECTOR_NAME changed — that would re-queue the whole corpus" >&2; exit 1; }
        log "  wrote $new_pipe_env, secrets carried over, SELECTOR_NAME unchanged"
    else
        log "  would rewrite $old_pipe_env -> $new_pipe_env (paths only, secrets carried over)"
    fi
else
    log "  pipeline config absent, skipping"
fi

step "Rewrite the update-services registry"

if (( APPLY )); then
    printf '%s\n' posinus-evaluator.service posinus-preparer.service posinus-publisher.service \
        > "$NEW_ETC/update-services"
    chown root:root "$NEW_ETC/update-services"
    chmod 0644 "$NEW_ETC/update-services"
else
    log "  would write posinus-{evaluator,preparer,publisher}.service to $NEW_ETC/update-services"
fi

# --------------------------------------------------------------------------------------
step "Check out the merged repository and rebuild the crawler venv"

run mv "$OLD_APP" "$OLD_APP.pre-posinus"
[[ -d $OLD_PIPE_APP ]] && run mv "$OLD_PIPE_APP" "$OLD_PIPE_APP.pre-posinus"
run install -d -o root -g root -m 0755 "$NEW_ROOT"
run git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$NEW_ROOT"
run chown -R root:root "$NEW_ROOT"
run python3 -m venv "$NEW_ROOT/crawler/.venv"
run "$NEW_ROOT/crawler/.venv/bin/python" -m pip install --upgrade pip
run "$NEW_ROOT/crawler/.venv/bin/python" -m pip install -e "$NEW_ROOT/crawler"

# --------------------------------------------------------------------------------------
step "Install the renamed units"

if (( APPLY )); then
    install -o root -g root -m 0644 \
        "$NEW_ROOT/crawler/deploy/systemd/posinus-web.service" \
        /etc/systemd/system/posinus-web.service
    install -o root -g root -m 0644 \
        "$NEW_ROOT/crawler/deploy/systemd/posinus-worker.service" \
        /etc/systemd/system/posinus-worker.service
    for unit in "${OLD_UNITS[@]}"; do
        rm -f "/etc/systemd/system/$unit"
    done
    systemctl daemon-reload
else
    log "  would install posinus-web.service and posinus-worker.service"
    log "  would remove the old unit files: ${OLD_UNITS[*]}"
fi

# --------------------------------------------------------------------------------------
step "Fix up ownership and start the crawler"

run chown -R "root:posinus" "$NEW_LIB/playwright"
run install -d -o posinus -g posinus -m 0750 "$NEW_ROOT/crawler/staticfiles"
if (( APPLY )); then
    ( set -a; . "$new_env"; set +a
      umask 0007
      cd "$NEW_ROOT/crawler"
      runuser -u posinus -- .venv/bin/python manage.py migrate --noinput
      runuser -u posinus -- .venv/bin/python manage.py collectstatic --noinput
      runuser -u posinus -- .venv/bin/python manage.py check )
    chown -R root:root "$NEW_ROOT/crawler/staticfiles"
    find "$NEW_ROOT/crawler/staticfiles" -type d -exec chmod 0755 {} +
    find "$NEW_ROOT/crawler/staticfiles" -type f -exec chmod 0644 {} +
    chown posinus:posinus "$NEW_DB"
    chmod 0660 "$NEW_DB"
    systemctl enable --now posinus-web.service posinus-worker.service
else
    log "  would run migrate / collectstatic / check, then enable posinus-web and posinus-worker"
fi

# --------------------------------------------------------------------------------------
step "Verify"

if (( APPLY )); then
    integrity=$(sqlite3 "$NEW_DB" 'PRAGMA integrity_check;')
    [[ $integrity == ok ]] || { echo "Post-migration integrity check failed: $integrity" >&2; exit 1; }
    systemctl is-active --quiet posinus-web.service || { echo "posinus-web did not start" >&2; exit 1; }
    systemctl is-active --quiet posinus-worker.service || { echo "posinus-worker did not start" >&2; exit 1; }
    log "  database ok, web and worker active"
    log ""
    log "Now install the pipeline, which creates its units and re-registers its timers:"
    log "  sudo bash $NEW_ROOT/pipeline/deploy/install.sh"
    log ""
    log "Old trees kept for rollback: $OLD_APP.pre-posinus, $OLD_PIPE_APP.pre-posinus"
    log "Pre-migration database backup: $backup"
else
    log ""
    log "Dry run only. Nothing changed. Rerun with --apply to perform the migration."
fi

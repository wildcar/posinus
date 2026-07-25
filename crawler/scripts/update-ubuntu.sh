#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/newscrawler}"
ENV_FILE="${ENV_FILE:-/etc/newscrawler/newscrawler.env}"
EXTRA_SERVICES_FILE="${EXTRA_SERVICES_FILE:-/etc/newscrawler/update-services}"
SERVICE_USER="${SERVICE_USER:-newscrawler}"
SERVICE_GROUP="${SERVICE_GROUP:-newscrawler}"
DB_PATH="${DB_PATH:-/var/lib/newscrawler/newscrawler.sqlite3}"
BACKUP_DIR="${BACKUP_DIR:-/var/lib/newscrawler/backups}"
BROWSER_PATH="${BROWSER_PATH:-/var/lib/newscrawler/playwright}"
LOG_DIR="${LOG_DIR:-/var/log/newscrawler}"
BRANCH="${1:-main}"
PYTHON="$APP_DIR/.venv/bin/python"

if [[ $EUID -ne 0 ]]; then
    echo "Run this script through sudo." >&2
    exit 1
fi
if [[ ! $BRANCH =~ ^[A-Za-z0-9._/-]+$ ]]; then
    echo "Invalid branch name: $BRANCH" >&2
    exit 1
fi

for command in curl flock git grep lsof runuser sqlite3 systemctl; do
    command -v "$command" >/dev/null || {
        echo "Required command is missing: $command" >&2
        exit 1
    }
done

exec 9>/run/lock/newscrawler-update.lock
flock -n 9 || {
    echo "Another newscrawler update is already running." >&2
    exit 1
}

[[ -d "$APP_DIR/.git" ]] || { echo "$APP_DIR is not a Git repository." >&2; exit 1; }
[[ -x "$PYTHON" ]] || { echo "Virtual environment is missing: $PYTHON" >&2; exit 1; }
[[ -r "$ENV_FILE" ]] || { echo "Environment file is missing: $ENV_FILE" >&2; exit 1; }
[[ $(stat -c '%u' "$ENV_FILE") == 0 ]] || { echo "$ENV_FILE must be owned by root." >&2; exit 1; }
env_mode=$(stat -c '%a' "$ENV_FILE")
(( (8#$env_mode & 022) == 0 )) || { echo "$ENV_FILE must not be group- or world-writable." >&2; exit 1; }

grep -Fxq "NEWSCRAWLER_DB_PATH=$DB_PATH" "$ENV_FILE" || { echo "NEWSCRAWLER_DB_PATH must be $DB_PATH." >&2; exit 1; }
grep -Fxq "NEWSCRAWLER_BACKUP_DIR=$BACKUP_DIR" "$ENV_FILE" || { echo "NEWSCRAWLER_BACKUP_DIR must be $BACKUP_DIR." >&2; exit 1; }
grep -Fxq "NEWSCRAWLER_LOG_DIR=$LOG_DIR" "$ENV_FILE" || { echo "NEWSCRAWLER_LOG_DIR must be $LOG_DIR." >&2; exit 1; }
grep -Fxq "PLAYWRIGHT_BROWSERS_PATH=$BROWSER_PATH" "$ENV_FILE" || { echo "PLAYWRIGHT_BROWSERS_PATH must be $BROWSER_PATH." >&2; exit 1; }
[[ -f "$DB_PATH" ]] || { echo "Installed database is missing: $DB_PATH" >&2; exit 1; }
[[ -d "$LOG_DIR" ]] || { echo "Log directory is missing: $LOG_DIR" >&2; exit 1; }

cd "$APP_DIR"
if [[ -n $(git status --porcelain) ]]; then
    echo "Refusing to update a dirty application checkout." >&2
    git status --short >&2
    exit 1
fi

current_branch=$(git symbolic-ref --quiet --short HEAD) || {
    echo "The application checkout must be on a branch." >&2
    exit 1
}
if [[ $current_branch != "$BRANCH" ]]; then
    echo "Checkout is on $current_branch, requested branch is $BRANCH." >&2
    exit 1
fi

git fetch --prune origin "$BRANCH"
read -r local_only remote_only < <(git rev-list --left-right --count "HEAD...origin/$BRANCH")
if (( local_only != 0 )); then
    echo "Local branch contains commits not present in origin/$BRANCH; aborting." >&2
    exit 1
fi

services=(newscrawler-web.service newscrawler-worker.service)
if [[ -f "$EXTRA_SERVICES_FILE" ]]; then
    while IFS= read -r service || [[ -n $service ]]; do
        service=${service%%#*}
        service=${service//[[:space:]]/}
        [[ -z $service ]] && continue
        if [[ ! $service =~ ^[A-Za-z0-9_.@-]+\.service$ ]]; then
            echo "Invalid service name in $EXTRA_SERVICES_FILE: $service" >&2
            exit 1
        fi
        services+=("$service")
    done < "$EXTRA_SERVICES_FILE"
fi

for service in "${services[@]}"; do
    systemctl cat "$service" >/dev/null || {
        echo "Registered update service does not exist: $service" >&2
        exit 1
    }
done

active_services=()
for service in "${services[@]}"; do
    if systemctl is-active --quiet "$service"; then
        active_services+=("$service")
    fi
done

old_commit=$(git rev-parse HEAD)
backup_path=""
services_stopped=0
update_succeeded=0
env_loaded=0

install_units() {
    install -o root -g root -m 0644 \
        "$APP_DIR/deploy/systemd/newscrawler-web.service" \
        /etc/systemd/system/newscrawler-web.service
    install -o root -g root -m 0644 \
        "$APP_DIR/deploy/systemd/newscrawler-worker.service" \
        /etc/systemd/system/newscrawler-worker.service
    systemctl daemon-reload
}

collect_static() {
    install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0750 "$APP_DIR/staticfiles"
    chown -R "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR/staticfiles"
    runuser -u "$SERVICE_USER" --preserve-environment -- "$PYTHON" manage.py collectstatic --noinput
    chown -R root:root "$APP_DIR/staticfiles"
    find "$APP_DIR/staticfiles" -type d -exec chmod 0755 {} +
    find "$APP_DIR/staticfiles" -type f -exec chmod 0644 {} +
}

start_previous_services() {
    if (( ${#active_services[@]} > 0 )); then
        systemctl start "${active_services[@]}"
    fi
}

rollback_on_failure() {
    status=$?
    trap - EXIT
    if (( update_succeeded == 0 && services_stopped == 1 )); then
        echo "Update failed; restoring the previous application and database state." >&2
        set +e
        systemctl stop "${services[@]}"
        if [[ -n $backup_path && -f $backup_path ]]; then
            rm -f -- "$DB_PATH" "$DB_PATH-wal" "$DB_PATH-shm" \
                "${DB_PATH%.sqlite3}.worker.lock"
            install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0660 \
                "$backup_path" "$DB_PATH"
        fi
        git reset --hard "$old_commit"
        "$PYTHON" -m pip install --disable-pip-version-check -e "$APP_DIR"
        install_units
        if (( env_loaded == 1 )); then
            collect_static
        fi
        start_previous_services
        set -e
    fi
    exit "$status"
}
trap rollback_on_failure EXIT

if (( ${#active_services[@]} > 0 )); then
    systemctl stop "${active_services[@]}"
fi
services_stopped=1

open_db_files=()
for path in "$DB_PATH" "$DB_PATH-wal" "$DB_PATH-shm"; do
    [[ -e $path ]] && open_db_files+=("$path")
done
if (( ${#open_db_files[@]} > 0 )) && lsof "${open_db_files[@]}"; then
    echo "A database client is still running. Register its systemd unit in $EXTRA_SERVICES_FILE." >&2
    exit 1
fi

if [[ -f "$DB_PATH" ]]; then
    install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 2770 "$BACKUP_DIR"
    backup_path="$BACKUP_DIR/pre-update-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
    sqlite3 "$DB_PATH" ".backup '$backup_path'"
    integrity=$(sqlite3 "$backup_path" 'PRAGMA integrity_check;')
    [[ $integrity == ok ]] || { echo "Backup integrity check failed: $integrity" >&2; exit 1; }
    chown "$SERVICE_USER:$SERVICE_GROUP" "$backup_path"
    chmod 0660 "$backup_path"
fi

git merge --ff-only "origin/$BRANCH"
"$PYTHON" -m pip install --disable-pip-version-check -e "$APP_DIR"
"$PYTHON" -m playwright install-deps chromium
install -d -o root -g "$SERVICE_GROUP" -m 2750 "$BROWSER_PATH"
PLAYWRIGHT_BROWSERS_PATH="$BROWSER_PATH" "$PYTHON" -m playwright install chromium
chown -R "root:$SERVICE_GROUP" "$BROWSER_PATH"
chmod -R g+rX,o-rwx "$BROWSER_PATH"

install_units
set -a
# The production file is root-owned and intentionally uses shell-compatible KEY=value lines.
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
env_loaded=1
umask 0007

runuser -u "$SERVICE_USER" --preserve-environment -- "$PYTHON" manage.py migrate --noinput
collect_static
runuser -u "$SERVICE_USER" --preserve-environment -- "$PYTHON" manage.py check

if [[ -f "$DB_PATH" ]]; then
    integrity=$(sqlite3 "$DB_PATH" 'PRAGMA integrity_check;')
    [[ $integrity == ok ]] || { echo "Updated database integrity check failed: $integrity" >&2; exit 1; }
    chown "$SERVICE_USER:$SERVICE_GROUP" "$DB_PATH"
    chmod 0660 "$DB_PATH"
fi
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$LOG_DIR"
find "$LOG_DIR" -type d -exec chmod 2770 {} +
find "$LOG_DIR" -type f -exec chmod 0660 {} +

start_previous_services

for service in "${active_services[@]}"; do
    systemctl is-active --quiet "$service" || {
        echo "Service did not start: $service" >&2
        exit 1
    }
done
if printf '%s\n' "${active_services[@]}" | grep -qx newscrawler-web.service; then
    web_ready=0
    for _ in {1..10}; do
        if curl --fail --silent --show-error --max-time 2 http://127.0.0.1:8000/login/ >/dev/null; then
            web_ready=1
            break
        fi
        sleep 1
    done
    (( web_ready == 1 )) || { echo "Web health check failed." >&2; exit 1; }
fi

update_succeeded=1
services_stopped=0
echo "Updated newscrawler from $old_commit to $(git rev-parse HEAD)."
if [[ -n $backup_path ]]; then
    echo "Pre-update database backup: $backup_path"
fi

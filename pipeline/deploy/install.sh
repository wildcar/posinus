#!/usr/bin/env bash
# Install or update the pipeline (evaluator, preparer, publisher) on this host.
# Idempotent; run as root:
#   sudo bash deploy/install.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The crawler owns /var/lib/posinus (its DB lives there, group-shared per the
# exchange contract). Install the crawler first: creating that directory here
# would hand the shared parent to the wrong owner, since `install -d` applies
# -o/-g to every directory it creates, parents included.
if [ ! -d /var/lib/posinus ]; then
    echo "/var/lib/posinus is missing — install the crawler first (docs/deployment.md)." >&2
    exit 1
fi

# Dedicated system user in the posinus group (crawler contract for direct
# DB clients).
if ! id -u posinus-pipeline >/dev/null 2>&1; then
    useradd --system --home-dir /nonexistent --no-create-home \
        --shell /usr/sbin/nologin --groups posinus posinus-pipeline
    echo "created system user posinus-pipeline (group posinus)"
fi

# The scripts run straight out of the checkout at /opt/posinus/pipeline — no copy
# step, so `sudo /opt/posinus/crawler/scripts/update-ubuntu.sh` ships new pipeline
# code along with the crawler. Rerun this installer only for unit or config changes.
if [ "$REPO_DIR" != /opt/posinus/pipeline ]; then
    echo "NOTE: units will run the code at /opt/posinus/pipeline, not $REPO_DIR." >&2
fi
for script in evaluator.py preparer.py publisher.py; do
    if [ ! -r "/opt/posinus/pipeline/$script" ]; then
        echo "Missing /opt/posinus/pipeline/$script — clone the repo to /opt/posinus." >&2
        exit 1
    fi
done

# Pipeline-owned state: own DB and downloaded images (the retelling itself is
# markdown in the DB). Kept separate from the crawler DB by contract; owned by
# the service user.
install -d -o posinus-pipeline -g posinus-pipeline -m 0750 /var/lib/posinus/pipeline
install -d -o posinus-pipeline -g posinus-pipeline -m 0750 /var/lib/posinus/pipeline/media

install -d -m 0755 /etc/posinus
ENV_FILE=/etc/posinus/pipeline.env
if [ ! -f "$ENV_FILE" ]; then
    install -o root -g posinus-pipeline -m 0640 \
        "$REPO_DIR/deploy/pipeline.env.example" "$ENV_FILE"
fi

# Convenience: pull the router token from its own .env so the file works
# out of the box. Rewrites only the placeholder, never an already-set token.
ROUTER_ENV=/opt/model-router-mcp/.env
if grep -qx 'ROUTER_AUTH_TOKEN=fill-me' "$ENV_FILE" && [ -r "$ROUTER_ENV" ]; then
    TOKEN="$(grep '^AUTH_TOKEN=' "$ROUTER_ENV" | head -1 | cut -d= -f2-)"
    if [ -n "$TOKEN" ]; then
        { grep -v '^ROUTER_AUTH_TOKEN=' "$ENV_FILE"
          printf 'ROUTER_AUTH_TOKEN=%s\n' "$TOKEN"; } > "$ENV_FILE.tmp"
        chown root:posinus-pipeline "$ENV_FILE.tmp"
        chmod 0640 "$ENV_FILE.tmp"
        mv "$ENV_FILE.tmp" "$ENV_FILE"
        echo "copied AUTH_TOKEN from model-router-mcp into $ENV_FILE"
    fi
fi
if grep -qx 'ROUTER_AUTH_TOKEN=fill-me' "$ENV_FILE"; then
    echo "NOTE: fill ROUTER_AUTH_TOKEN in $ENV_FILE" >&2
fi

install -m 0644 "$REPO_DIR/deploy/posinus-evaluator.service" /etc/systemd/system/posinus-evaluator.service
install -m 0644 "$REPO_DIR/deploy/posinus-evaluator.timer" /etc/systemd/system/posinus-evaluator.timer
install -m 0644 "$REPO_DIR/deploy/posinus-preparer.service" /etc/systemd/system/posinus-preparer.service
install -m 0644 "$REPO_DIR/deploy/posinus-preparer.timer" /etc/systemd/system/posinus-preparer.timer
install -m 0644 "$REPO_DIR/deploy/posinus-publisher.service" /etc/systemd/system/posinus-publisher.service
install -m 0644 "$REPO_DIR/deploy/posinus-publisher.timer" /etc/systemd/system/posinus-publisher.timer
systemctl daemon-reload
systemctl enable --now posinus-evaluator.timer
systemctl enable --now posinus-preparer.timer
systemctl enable --now posinus-publisher.timer

# The crawler's update script stops every service listed here before touching
# the DB schema (both open the crawler DB).
touch /etc/posinus/update-services
for unit in posinus-evaluator.service posinus-preparer.service posinus-publisher.service; do
    if ! grep -qx "$unit" /etc/posinus/update-services; then
        echo "$unit" >> /etc/posinus/update-services
        echo "registered $unit in /etc/posinus/update-services"
    fi
done

echo "done; check: systemctl list-timers 'posinus-*.timer'"

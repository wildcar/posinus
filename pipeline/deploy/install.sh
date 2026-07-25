#!/usr/bin/env bash
# Install or update the news evaluator on this host. Idempotent; run as root:
#   sudo bash deploy/install.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Dedicated system user in the newscrawler group (crawler contract for direct
# DB clients).
if ! id -u newsevaluator >/dev/null 2>&1; then
    useradd --system --home-dir /nonexistent --no-create-home \
        --shell /usr/sbin/nologin --groups newscrawler newsevaluator
    echo "created system user newsevaluator (group newscrawler)"
fi

install -d -m 0755 /opt/news-evaluator
install -m 0644 "$REPO_DIR/evaluator.py" /opt/news-evaluator/evaluator.py
install -m 0644 "$REPO_DIR/preparer.py" /opt/news-evaluator/preparer.py
install -m 0644 "$REPO_DIR/publisher.py" /opt/news-evaluator/publisher.py

# Evaluator-owned state: own DB and downloaded images (the retelling itself is
# markdown in the DB). Kept separate from the crawler DB by contract; owned by
# the service user.
install -d -o newsevaluator -g newsevaluator -m 0750 /var/lib/news-evaluator
install -d -o newsevaluator -g newsevaluator -m 0750 /var/lib/news-evaluator/media

install -d -m 0755 /etc/news-evaluator
ENV_FILE=/etc/news-evaluator/news-evaluator.env
if [ ! -f "$ENV_FILE" ]; then
    install -o root -g newsevaluator -m 0640 \
        "$REPO_DIR/deploy/news-evaluator.env.example" "$ENV_FILE"
fi

# Convenience: pull the router token from its own .env so the file works
# out of the box. Rewrites only the placeholder, never an already-set token.
ROUTER_ENV=/opt/model-router-mcp/.env
if grep -qx 'ROUTER_AUTH_TOKEN=fill-me' "$ENV_FILE" && [ -r "$ROUTER_ENV" ]; then
    TOKEN="$(grep '^AUTH_TOKEN=' "$ROUTER_ENV" | head -1 | cut -d= -f2-)"
    if [ -n "$TOKEN" ]; then
        { grep -v '^ROUTER_AUTH_TOKEN=' "$ENV_FILE"
          printf 'ROUTER_AUTH_TOKEN=%s\n' "$TOKEN"; } > "$ENV_FILE.tmp"
        chown root:newsevaluator "$ENV_FILE.tmp"
        chmod 0640 "$ENV_FILE.tmp"
        mv "$ENV_FILE.tmp" "$ENV_FILE"
        echo "copied AUTH_TOKEN from model-router-mcp into $ENV_FILE"
    fi
fi
if grep -qx 'ROUTER_AUTH_TOKEN=fill-me' "$ENV_FILE"; then
    echo "NOTE: fill ROUTER_AUTH_TOKEN in $ENV_FILE" >&2
fi

install -m 0644 "$REPO_DIR/deploy/news-evaluator.service" /etc/systemd/system/news-evaluator.service
install -m 0644 "$REPO_DIR/deploy/news-evaluator.timer" /etc/systemd/system/news-evaluator.timer
install -m 0644 "$REPO_DIR/deploy/news-preparer.service" /etc/systemd/system/news-preparer.service
install -m 0644 "$REPO_DIR/deploy/news-preparer.timer" /etc/systemd/system/news-preparer.timer
install -m 0644 "$REPO_DIR/deploy/news-publisher.service" /etc/systemd/system/news-publisher.service
install -m 0644 "$REPO_DIR/deploy/news-publisher.timer" /etc/systemd/system/news-publisher.timer
systemctl daemon-reload
systemctl enable --now news-evaluator.timer
systemctl enable --now news-preparer.timer
systemctl enable --now news-publisher.timer

# The crawler's update script stops every service listed here before touching
# the DB schema (both open the crawler DB).
touch /etc/newscrawler/update-services
for unit in news-evaluator.service news-preparer.service news-publisher.service; do
    if ! grep -qx "$unit" /etc/newscrawler/update-services; then
        echo "$unit" >> /etc/newscrawler/update-services
        echo "registered $unit in /etc/newscrawler/update-services"
    fi
done

echo "done; check: systemctl list-timers 'news-*.timer'"

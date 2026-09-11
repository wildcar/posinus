#!/usr/bin/env bash
# Ask for a wildcar.org rebuild when the site's hand-edited sources changed.
#
# The publisher rebuilds the site after every publication, but «Интересное»
# (docs/interesting/), the static pages, the hook, the theme overrides and
# mkdocs.yml are edited by hand in the site checkout, and nothing in the
# pipeline knows when. So posinus-wildcar-org-watch.timer runs this every
# couple of minutes: when something there is newer than the last build it
# touches the same marker the publisher uses, and the .path unit starts the
# build. A change younger than QUIET seconds is left for the next run — a
# folder still being copied in is only half there.
#
# Runs as keeper (the checkout is keeper's) with SupplementaryGroups=posinus,
# which the request mailbox (group posinus, mode 2770) requires for the marker.
set -euo pipefail

SITE_REPO="${WILDCAR_SITE_REPO:-/home/keeper/repo/wildcar-site}"
SITE_OUT="${WILDCAR_SITE_OUT:-/var/www/wildcar.org}"
MARKER="${WILDCAR_ORG_REBUILD_MARKER:-/var/lib/posinus/pipeline/requests/rebuild-wildcar-org}"
QUIET="${WILDCAR_ORG_WATCH_QUIET:-90}"
# mkdocs writes the sitemap last on every build; its mtime is the build time.
STAMP="$SITE_OUT/sitemap.xml"

[ -d "$SITE_REPO/docs" ] || exit 0
# Never built: the publisher's first marker builds it, nothing to compare against.
[ -e "$STAMP" ] || exit 0

# docs/news and docs/kartina are the publisher's, rsynced in by every build;
# their timestamps are the publisher's, and their directories change with
# every rebuild, so they are pruned — otherwise the watch would chase its
# own tail. `find` here may be bfs, so the -newermt stamp is ISO 8601.
watch() {
    find "$SITE_REPO/docs" "$SITE_REPO/hooks" "$SITE_REPO/overrides" "$SITE_REPO/mkdocs.yml" \
        \( -path "$SITE_REPO/docs/news" -o -path "$SITE_REPO/docs/kartina" \) -prune -o "$@" -print -quit
}

changed=$(watch -newer "$STAMP")
[ -n "$changed" ] || exit 0
since=$(date -u -d "@$(( $(date +%s) - QUIET ))" +%Y-%m-%dT%H:%M:%SZ)
busy=$(watch -newermt "$since")
if [ -n "$busy" ]; then
    echo "site sources still changing ($busy); waiting for the next run"
    exit 0
fi
echo "site sources changed since the last build ($changed); requesting a rebuild"
touch "$MARKER"

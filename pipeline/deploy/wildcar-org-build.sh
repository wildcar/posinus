#!/usr/bin/env bash
# Rebuild the wildcar.org static site with the news section the publisher wrote.
#
# Runs as keeper from posinus-wildcar-org-build.service: the MkDocs checkout and
# venv are keeper's, and SupplementaryGroups=posinus grants read access to the
# content directory. The publisher (posinus-pipeline) cannot run MkDocs itself,
# so it writes files, touches the rebuild marker and waits for this.
set -euo pipefail

CONTENT="${WILDCAR_ORG_CONTENT_DIR:-/var/lib/posinus/wildcar-org}"
SRC="$CONTENT/news"
DAYPIC_SRC="$CONTENT/${DAYPIC_WILDCAR_SECTION:-kartina}"
SITE_REPO="${WILDCAR_SITE_REPO:-/home/keeper/repo/wildcar-site}"
SITE_OUT="${WILDCAR_SITE_OUT:-/var/www/wildcar.org}"
MKDOCS="${MKDOCS_BIN:-/home/keeper/.venvs/mkdocs/bin/mkdocs}"

# An empty or unreadable source must not wipe the published section: rsync
# --delete against it would take every news page off the site.
if [ ! -r "$SRC/index.md" ]; then
    echo "no readable $SRC/index.md; refusing to sync an empty news section" >&2
    exit 1
fi

# docs/news is generated content, gitignored in the site repository; the
# publisher's copy is the source of truth and this sync is one-way.
rsync -a --delete "$SRC/" "$SITE_REPO/docs/news/"
# The daily-picture section rides the same rebuild. It appears only after the
# first issue, so its absence is normal — and the same guard keeps a broken
# content dir from wiping the published pages.
if [ -r "$DAYPIC_SRC/index.md" ]; then
    rsync -a --delete "$DAYPIC_SRC/" "$SITE_REPO/docs/$(basename "$DAYPIC_SRC")/"
fi
"$MKDOCS" build --config-file "$SITE_REPO/mkdocs.yml" --site-dir "$SITE_OUT"

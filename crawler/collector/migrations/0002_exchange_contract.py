from django.db import migrations


FORWARD_SQL = """
CREATE VIEW exchange_news_for_selection AS
SELECT
    n.id AS news_id,
    COALESCE((
        SELECT o.canonical_url
        FROM news_occurrences o
        WHERE o.news_item_id = n.id
        ORDER BY o.fetched_at, o.id
        LIMIT 1
    ), '') AS primary_url,
    COALESCE((
        SELECT json_group_array(json_object(
            'url', o.url,
            'canonical_url', o.canonical_url,
            'source_id', s.id,
            'source_name', s.name,
            'domain', s.domain,
            'fetched_at', o.fetched_at
        ))
        FROM news_occurrences o
        JOIN sources s ON s.id = o.source_id
        WHERE o.news_item_id = n.id
    ), '[]') AS sources_json,
    n.title,
    n.body_text,
    n.language,
    n.published_at,
    n.first_seen_at
FROM news_items n
WHERE n.purged_at IS NULL;

CREATE VIEW exchange_latest_reviews AS
SELECT id, news_id, decision, score, reason, selector_name,
       selector_version, idempotency_key, created_at
FROM (
    SELECT r.*,
           row_number() OVER (
               PARTITION BY r.news_id, r.selector_name
               ORDER BY r.created_at DESC, r.id DESC
           ) AS position
    FROM exchange_review_events r
)
WHERE position = 1;

CREATE TRIGGER exchange_review_events_no_update
BEFORE UPDATE ON exchange_review_events
BEGIN
    SELECT RAISE(ABORT, 'exchange_review_events is append-only');
END;

CREATE TRIGGER exchange_review_events_no_delete
BEFORE DELETE ON exchange_review_events
BEGIN
    SELECT RAISE(ABORT, 'exchange_review_events is append-only');
END;
"""

REVERSE_SQL = """
DROP TRIGGER IF EXISTS exchange_review_events_no_delete;
DROP TRIGGER IF EXISTS exchange_review_events_no_update;
DROP VIEW IF EXISTS exchange_latest_reviews;
DROP VIEW IF EXISTS exchange_news_for_selection;
"""


class Migration(migrations.Migration):
    dependencies = [("collector", "0001_initial")]
    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]


# Written by hand on 2026-09-24.

from django.db import migrations

# News about Russia never reached the air: from 2026-08-20 to 2026-09-24 every
# selected item with pride_russia >= 5 (a snow leopard census, a rescued Ladoga
# seal, Chelyabinsk surgeons) sat in the queue at a strength of about 7.2 while
# the published ones started at 7.8, and expired after ten days. Two changes,
# both on the owner's request of 2026-09-24:
#
# - such an item gets +1.0 to its strength (capped at 10), which puts it among
#   what actually goes out;
# - the view now also returns `pride_russia` itself, so the publisher can keep
#   its daily quota of news about Russia (see pipeline/AGENTS/SPEC.md).
#
# The threshold and the bonus are repeated in collector/services/broadcast.py
# (RUSSIA_MIN, RUSSIA_BONUS): the «Эфир» screen must show the same order.
FORWARD_SQL = """
DROP VIEW IF EXISTS exchange_publication_order;
CREATE VIEW exchange_publication_order AS
SELECT
    s.news_id,
    ROUND(MIN(10.0,
        0.5 * MAX(CASE WHEN s.characteristic_key IN (
                    'pride_humanity', 'pride_russia', 'inspiration', 'beauty',
                    'interestingness', 'surprise', 'uniqueness'
                 ) THEN s.value ELSE 0 END)
      + 0.3 * MAX(CASE WHEN s.characteristic_key = 'positivity' THEN s.value ELSE 0 END)
      + 0.2 * MAX(CASE WHEN s.characteristic_key = 'interestingness' THEN s.value ELSE 0 END)
      + CASE WHEN MAX(CASE WHEN s.characteristic_key = 'pride_russia' THEN s.value ELSE 0 END) >= 5
             THEN 1.0 ELSE 0 END
    ), 2) AS strength,
    COALESCE(p.rank, 0) AS operator_rank,
    p.hold_until AS hold_until,
    p.dropped_at AS dropped_at,
    MAX(CASE WHEN s.characteristic_key = 'pride_russia' THEN s.value ELSE 0 END) AS pride_russia
FROM exchange_latest_evaluation_scores s
LEFT JOIN exchange_publication_plan p ON p.news_id = s.news_id
WHERE s.selector_name = 'news-evaluator'
GROUP BY s.news_id;
"""

REVERSE_SQL = """
DROP VIEW IF EXISTS exchange_publication_order;
CREATE VIEW exchange_publication_order AS
SELECT
    s.news_id,
    ROUND(
        0.5 * MAX(CASE WHEN s.characteristic_key IN (
                    'pride_humanity', 'pride_russia', 'inspiration', 'beauty',
                    'interestingness', 'surprise', 'uniqueness'
                 ) THEN s.value ELSE 0 END)
      + 0.3 * MAX(CASE WHEN s.characteristic_key = 'positivity' THEN s.value ELSE 0 END)
      + 0.2 * MAX(CASE WHEN s.characteristic_key = 'interestingness' THEN s.value ELSE 0 END),
    2) AS strength,
    COALESCE(p.rank, 0) AS operator_rank,
    p.hold_until AS hold_until,
    p.dropped_at AS dropped_at
FROM exchange_latest_evaluation_scores s
LEFT JOIN exchange_publication_plan p ON p.news_id = s.news_id
WHERE s.selector_name = 'news-evaluator'
GROUP BY s.news_id;
"""


class Migration(migrations.Migration):

    dependencies = [
        ('collector', '0018_daypicslot_openrouter'),
    ]

    operations = [
        migrations.RunSQL(FORWARD_SQL, REVERSE_SQL),
    ]

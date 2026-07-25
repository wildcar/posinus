"""Minimal direct-SQLite selector contract example."""
import os
import sqlite3
import uuid
from datetime import datetime, timezone

database = os.environ.get("NEWSCRAWLER_DB_PATH", "data/newscrawler.sqlite3")
connection = sqlite3.connect(database, timeout=30)
connection.execute("PRAGMA journal_mode=WAL")
connection.execute("PRAGMA foreign_keys=ON")
connection.execute("PRAGMA busy_timeout=30000")

row = connection.execute("""
    SELECT n.news_id, n.title, n.body_text, n.sources_json
    FROM exchange_news_for_selection n
    WHERE NOT EXISTS (
        SELECT 1 FROM exchange_latest_reviews r
        WHERE r.news_id = n.news_id AND r.selector_name = ?
    )
    ORDER BY n.first_seen_at
    LIMIT 1
""", ("example-selector",)).fetchone()

if row:
    news_id, title, body, sources_json = row
    # Replace this demonstration decision with the real selector.
    decision = "skipped"
    connection.execute("""
        INSERT INTO exchange_review_events
          (news_id, decision, score, reason, selector_name, selector_version,
           idempotency_key, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (news_id, decision, None, "example only", "example-selector", "1",
          str(uuid.uuid4()), datetime.now(timezone.utc).isoformat()))
    connection.commit()
connection.close()

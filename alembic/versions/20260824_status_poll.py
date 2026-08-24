"""status polling: ask the provider whether a submitted post went live

Revision ID: 20260824_status_poll
Revises: 20260823_credential_slots
Create Date: 2026-08-24

Adds a nullable ``last_polled_at`` to ``publish_attempts``.

Why
---
Until now the only completion signal was a webhook, because the only provider
had no status endpoint (``supports_status_lookup=False``). A post whose callback
was lost — a redeploy mid-flight, a webhook secret not yet stored, a signature
mismatch — had exactly one ending: the stale sweeper moved it to ``unknown``
after 30 minutes and a human had to go and look at the account.

Zernio declares ``supports_status_lookup=True``, so that post can simply be asked
about. This column is the poller's rate limit: without a persisted timestamp,
"poll submitted attempts" runs every reconciliation tick (60s by default) and a
post pending for an hour costs 60 requests instead of 12. Being a column rather
than in-memory state matters because the queue is deliberately shared by two
processes and either may restart at any time.

NULL means never polled, which is both the correct initial value for every
existing row and the permanent value for every attempt on a provider that cannot
be polled. Nothing reads it except the poller's own due-check, so this migration
cannot change the behaviour of anything already running.

Defensive in the same way as the baseline: the repo's boot path is ``create_all``,
so a live database may already have this column from a boot on newer code.
"""
import sqlalchemy as sa
from alembic import op

revision = "20260824_status_poll"
down_revision = "20260823_credential_slots"
branch_labels = None
depends_on = None

TABLE = "publish_attempts"
COLUMN = "last_polled_at"


def _table_exists(table):
    return table in set(sa.inspect(op.get_bind()).get_table_names())


def _has_column(table, column):
    insp = sa.inspect(op.get_bind())
    if table not in set(insp.get_table_names()):
        return True  # nothing to add to a table that is not there
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    if not _has_column(TABLE, COLUMN):
        op.add_column(TABLE, sa.Column(
            COLUMN, sa.DateTime(timezone=True), nullable=True))


def downgrade():
    """Drop the column.

    Lossless: it holds no publication fact, only when the poller last asked. On
    the next pass every submitted attempt simply looks never-polled and is asked
    once more.
    """
    if _table_exists(TABLE) and _has_column(TABLE, COLUMN):
        op.drop_column(TABLE, COLUMN)

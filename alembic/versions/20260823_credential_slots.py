"""credential slots: several provider accounts behind one publishing group

Revision ID: 20260823_credential_slots
Revises: 20260809_publishing
Create Date: 2026-08-23

Adds a nullable ``credential_slot`` label to ``publish_credentials`` and
``publish_destinations``.

Why
---
Until now a group held exactly ONE provider API key, which was fine while the
only provider connected every platform under a single account. Zernio does not:
its free tier connects 2 social accounts per Zernio account, and a full fan-out
needs 3 (TikTok + Instagram + YouTube). So a group now holds several keys, each
labelled with a slot, and a destination names the slot it publishes through.

The label is nullable on purpose, and NULL means "the group default". That is
what makes this change invisible to everything that already exists: every
Status 200 credential and destination stays NULL, resolution falls through to the
default exactly as before, and the encryption AAD is unchanged for a NULL slot
(see ``publishing/admin_api._credential_aad`` — appending a slot segment
unconditionally would make every stored credential undecryptable).

Defensive in the same way as the baseline: the repo's boot path is ``create_all``,
so a live database may already have these columns from a boot on newer code. Every
step checks the catalogue first.
"""
import sqlalchemy as sa
from alembic import op

revision = "20260823_credential_slots"
down_revision = "20260809_publishing"
branch_labels = None
depends_on = None


SLOTTED = ("publish_credentials", "publish_destinations")

# The lookup index gains the slot column, because resolution now filters on it.
OLD_LOOKUP = "ix_publish_credentials_lookup"
OLD_LOOKUP_COLS = ["publish_group_id", "kind", "active"]
NEW_LOOKUP_COLS = ["publish_group_id", "kind", "credential_slot", "active"]

# At most one ACTIVE credential per (group, kind, slot). This is the same
# "rotation is by-insert" invariant the revoke sweep in admin_api enforces in
# application code, pinned in the schema so a concurrent write cannot leave two
# live keys in one slot and make dispatch's choice arbitrary.
#
# Partial, and NULLS NOT DISTINCT is deliberately NOT used (it needs PG 15): a
# NULL slot is the group default, and Postgres treats NULLs as distinct in a
# unique index, so the default slot is not covered by this constraint. That is
# the pre-existing behaviour for the pre-existing shape — unchanged on purpose.
UNIQUE_ACTIVE = "uq_credential_active_per_slot"


def _has_column(table, column):
    insp = sa.inspect(op.get_bind())
    if table not in set(insp.get_table_names()):
        return True  # nothing to add to a table that is not there
    return column in {c["name"] for c in insp.get_columns(table)}


def _indexes(table):
    insp = sa.inspect(op.get_bind())
    if table not in set(insp.get_table_names()):
        return set()
    names = {ix["name"] for ix in insp.get_indexes(table)}
    names |= {uq["name"] for uq in insp.get_unique_constraints(table)}
    return names


def _table_exists(table):
    return table in set(sa.inspect(op.get_bind()).get_table_names())


def upgrade():
    for table in SLOTTED:
        if not _has_column(table, "credential_slot"):
            op.add_column(
                table, sa.Column("credential_slot", sa.String(32), nullable=True))

    if not _table_exists("publish_credentials"):
        return

    have = _indexes("publish_credentials")
    if OLD_LOOKUP in have:
        op.drop_index(OLD_LOOKUP, table_name="publish_credentials")
    op.create_index(OLD_LOOKUP, "publish_credentials", NEW_LOOKUP_COLS)

    if UNIQUE_ACTIVE not in have:
        op.create_index(
            UNIQUE_ACTIVE, "publish_credentials",
            ["publish_group_id", "kind", "credential_slot"],
            unique=True,
            postgresql_where=sa.text(
                "active AND revoked_at IS NULL AND credential_slot IS NOT NULL"),
        )


def downgrade():
    """Narrow the index back and drop the columns.

    Dropping ``credential_slot`` is lossy in the one case that matters: a group
    with two provider accounts loses the mapping that says which destination
    publishes through which key, and every destination falls back to the group
    default. On a Zernio group that means posts routed to an account that cannot
    reach that platform. Downgrade only from a state where slots are unused.
    """
    if _table_exists("publish_credentials"):
        have = _indexes("publish_credentials")
        if UNIQUE_ACTIVE in have:
            op.drop_index(UNIQUE_ACTIVE, table_name="publish_credentials")
        if OLD_LOOKUP in have:
            op.drop_index(OLD_LOOKUP, table_name="publish_credentials")
        op.create_index(OLD_LOOKUP, "publish_credentials", OLD_LOOKUP_COLS)

    for table in SLOTTED:
        if _table_exists(table) and _has_column(table, "credential_slot"):
            op.drop_column(table, "credential_slot")

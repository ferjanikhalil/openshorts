"""Database access for publishing.

Deliberately thin: it reuses ``cloud.database``'s engine, sessionmaker and
declarative Base rather than opening a second pool. Publishing works with
``BILLING_ENABLED`` off — ``cloud.database`` reads ``DATABASE_URL`` directly and
holds no billing logic — so a self-hoster who wants publishing needs Postgres but
not the cloud/billing stack.

One wrinkle worth naming: ``cloud.database.init_engine()`` returns early if the
engine already exists, and it only imports ``cloud.models`` before
``create_all``. So publishing must import its own models and re-run
``create_all`` itself; otherwise, whenever cloud initializes first, the publish
tables would silently never be created. ``create_all`` is idempotent, so doing it
twice costs one cheap round-trip at boot.
"""
from sqlalchemy import text

from cloud import database


async def init() -> None:
    """Ensure the engine exists and the publishing tables are present."""
    # Registers the 9 publish tables on the shared Base.metadata.
    from . import models  # noqa: F401

    await database.init_engine()

    engine = database._engine  # noqa: SLF001 — same package family, no public accessor
    async with engine.begin() as conn:
        await conn.run_sync(_create_publishing_tables)
        # Partial unique indexes are created by create_all, but be explicit about
        # the one that matters: without it, two concurrent dispatchers could each
        # insert a live attempt for the same (request, destination) and publish
        # the same clip twice to a real audience.
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_attempt_live_per_destination "
            "ON publish_attempts (publish_request_id, publish_destination_id) "
            "WHERE status IN ('pending','in_flight','submitted','succeeded')"
        ))


def _create_publishing_tables(sync_conn) -> None:
    from . import models
    tables = [
        models.PublishGroup.__table__,
        models.PublishCredential.__table__,
        models.PublishDestination.__table__,
        models.PublishAssignment.__table__,
        models.PublishRequest.__table__,
        models.PublishAttempt.__table__,
        models.PublishMedia.__table__,
        models.ProviderWebhookEvent.__table__,
        models.PublishEvent.__table__,
    ]
    database.Base.metadata.create_all(sync_conn, tables=tables, checkfirst=True)


def session():
    """New AsyncSession context manager, for background workers."""
    return database.session()


async def get_db():
    """FastAPI dependency yielding an AsyncSession."""
    async for s in database.get_db():
        yield s

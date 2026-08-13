import pytest
from sqlalchemy.orm import Session

from app.db import engine


@pytest.fixture
def db_session():
    """A Session bound to a connection-level transaction that's rolled back
    at teardown. Code under test (e.g. ingest_failures) can call db.commit()
    freely — join_transaction_mode="create_savepoint" makes those commits
    only release/recreate a SAVEPOINT, never the outer transaction, so
    nothing written during a test is ever actually persisted.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()

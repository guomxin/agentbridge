"""Compose local store mutations without opening a second writer transaction."""
from contextlib import contextmanager


@contextmanager
def store_transaction(store, connection=None):
    if connection is not None:
        if not connection.in_transaction:
            raise ValueError("an explicit store connection requires an active transaction")
        yield connection
        return
    with store._connect() as owned:
        owned.execute("BEGIN IMMEDIATE")
        yield owned

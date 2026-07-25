import random
import sqlite3
import time
from functools import wraps


def retry_sqlite(attempts=5):
    def decorator(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            for attempt in range(attempts):
                try:
                    return function(*args, **kwargs)
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or attempt == attempts - 1:
                        raise
                    time.sleep((0.05 * 2**attempt) + random.random() * 0.05)
        return wrapped
    return decorator


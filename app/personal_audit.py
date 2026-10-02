"""Digest personal tables read-only, so an install can prove it left personal data unchanged."""
import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import sqlite3

from user_store import PERSONAL_TABLES

# Imports and Photos sync legitimately add rows while the app runs.
GROWING_TABLES = ('photos_import_history', 'media_roots')


def audit(database):
    with contextlib.closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
        present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = {}
        for table in PERSONAL_TABLES:
            if table not in present:
                continue
            digest, count = hashlib.sha256(), 0
            for row in db.execute(f'SELECT * FROM {table} ORDER BY 1'):
                digest.update(json.dumps(row, default=str).encode() + b'\n')
                count += 1
            result[table] = dict(rows=count, sha256=digest.hexdigest())
        return result


def changed_tables(before, after):
    """Tables (other than ones that grow during normal use) whose contents changed."""
    return [table for table in before if table not in GROWING_TABLES and before[table] != after.get(table)]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    print(json.dumps(audit(parser.parse_args().database), sort_keys=True))

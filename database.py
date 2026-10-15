"""Read-only SQLite access. No hosted database or credentials required."""
from contextlib import contextmanager, closing
from datetime import datetime
from functools import lru_cache
from pathlib import Path
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time

ROOT = Path(__file__).resolve().parent
PAGE_SIZE = 100

@lru_cache(maxsize=8192)
def pcard_datetime(value):
    """Convert the source M/D/YYYY H:MM:SS strings to sortable ISO text."""
    if value is None or not str(value).strip():
        return None
    try:
        day, clock = str(value).strip().split()
        month, day, year = map(int, day.split('/'))
        hour, minute, second = map(int, clock.split(':'))
        return datetime(year, month, day, hour, minute, second).isoformat(' ')
    except (ValueError, TypeError):
        return None

@lru_cache(maxsize=1)
def database_path():
    manifest = json.loads((ROOT / 'data/manifest.json').read_text())
    target = Path(tempfile.gettempdir()) / ('pcards-' + manifest['sha256'] + '.db')
    if target.exists():
        with target.open('rb') as existing:
            if hashlib.file_digest(existing, 'sha256').hexdigest() == manifest['sha256']:
                return target
    fd, filename = tempfile.mkstemp(prefix='pcards-', suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as dest, gzip.open(ROOT / 'data/pcards.db.gz', 'rb') as source:
            digest = hashlib.sha256()
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                dest.write(chunk)
        if digest.hexdigest() != manifest['sha256']:
            raise ValueError('Bundled database checksum does not match.')
        os.replace(filename, target)
    finally:
        if os.path.exists(filename):
            os.unlink(filename)
    return target

FUNCTIONS = set('abs avg coalesce count date datetime dense_rank first_value glob ifnull instr julianday lag last_value lead length like lower ltrim max min nullif printf rank replace round row_number rtrim strftime substr substring sum time total trim typeof unicode upper pcard_datetime'.split())

@lru_cache(maxsize=1)
def indexed_database_path():
    """Build indexes in a disposable copy; keep the bundled snapshot unchanged."""
    original = database_path()
    target = original.with_name(original.stem + '-indexed-v1.db')
    if target.exists():
        return target
    fd, name = tempfile.mkstemp(prefix='pcards-index-', suffix='.db', dir=target.parent)
    os.close(fd)
    try:
        shutil.copyfile(original, name)
        with closing(sqlite3.connect(name)) as conn:
            conn.execute('CREATE INDEX audit_person_date ON pcards(Year, AgencyName, FullName, TransactionDate)')
            conn.execute('CREATE INDEX audit_person_vendor_amount ON pcards(Year, AgencyName, FullName, Vendor, Amount)')
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return target

def authorize(action, arg1, arg2, db, source):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    # SQLite reports db=None and an empty column for COUNT(*) table reads.
    if action == sqlite3.SQLITE_READ and arg1 == 'pcards' and (db == 'main' or (db is None and arg2 == '')):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or '').lower() in FUNCTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY

@contextmanager
def connection(path=None, timeout=15):
    conn = sqlite3.connect(Path(path or indexed_database_path()).resolve().as_uri() + '?mode=ro', uri=True)
    conn.create_function('pcard_datetime', 1, pcard_datetime, deterministic=True)
    conn.execute('PRAGMA query_only=ON')
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2_000_000)
    conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 30_000)
    conn.set_authorizer(authorize)
    deadline = time.monotonic() + timeout
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    try:
        yield conn
    finally:
        conn.close()

def validate_sql(sql):
    if not isinstance(sql, str) or not 1 <= len(sql) <= 20000:
        raise ValueError('Expected a SQL query of up to 20,000 characters.')
    sql = re.sub(r'\A(?:\s*--[^\n]*(?:\n|$))*', '', sql).strip().removesuffix(';').strip()
    if not re.match(r'^(SELECT|WITH)\b', sql, re.I) or not re.search(r'\bpcards\b', sql, re.I):
        raise ValueError('Use one SELECT or WITH query against pcards.')
    return sql

def query_page(sql, parameters=(), page=1, path=None):
    sql = validate_sql(sql)
    with connection(path) as conn:
        total = conn.execute(f'SELECT COUNT(*) FROM ({sql}) AS results', parameters).fetchone()[0]
        page = max(1, min(int(page), max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)))
        executed = f'SELECT * FROM ({sql}) AS results LIMIT ? OFFSET ?'
        bound = tuple(parameters) + (PAGE_SIZE, (page - 1) * PAGE_SIZE)
        cursor = conn.execute(executed, bound)
        headers = [c[0] for c in cursor.description]
        rows = cursor.fetchall()
    return dict(headers=headers, rows=rows, total=total, page=page, sql=executed, parameters=bound)

def years():
    with connection() as conn:
        return [r[0] for r in conn.execute('SELECT DISTINCT "Year" FROM pcards WHERE "Year" IS NOT NULL ORDER BY "Year" DESC')]

SORT_COLUMNS = {'Transaction date': 'pcard_datetime("TransactionDate")', 'Posted date': 'pcard_datetime("PostedDate")', 'Name': '"FullName"', 'Vendor': '"Vendor"', 'Description': '"Description"', 'Amount': '"Amount"', 'MCC': '"MCC"'}

def search_query(year, field, keyword, sort='Transaction date', direction='Descending'):
    if field not in ('Description', 'Vendor') or sort not in SORT_COLUMNS or direction not in ('Ascending', 'Descending'):
        raise ValueError('Choose valid search and sorting options.')
    keyword = keyword.strip()
    if not 1 <= len(keyword) <= 80:
        raise ValueError('Enter a keyword of 1–80 characters.')
    # Preserve original SQL LIKE behavior: % and _ remain wildcards.
    sql = f'''SELECT "TransactionDate" AS "Transaction date", "PostedDate" AS "Posted date",
       "FullName" AS "Name", "Vendor", "Description", "Amount", "MCC"
FROM pcards
WHERE "Year" = ? AND LOWER(COALESCE("{field}", '')) LIKE LOWER(?)
ORDER BY {SORT_COLUMNS[sort]} {'ASC' if direction == 'Ascending' else 'DESC'},
         pcard_datetime("TransactionDate") DESC, rowid'''
    return sql, (int(year), '%' + keyword + '%')

def split_query(kind):
    controls = {
        'Same cardholder, vendor and day': ('"FullName", "Vendor", day', 'COUNT(*) > 1'),
        'Two cardholders at one vendor': ('"Vendor", day', 'COUNT(*) = 2 AND COUNT(DISTINCT "FullName") = 2'),
        'One cardholder at two vendors': ('"FullName", day', 'COUNT(*) = 2 AND COUNT(DISTINCT "Vendor") = 2'),
    }
    group, condition = controls[kind]
    fields = group.split(', ')
    join = ' AND '.join(f'p.{field} IS g.{field}' for field in fields)
    return f'''WITH positive AS (
  SELECT rowid AS source_row, *, date(pcard_datetime("TransactionDate")) AS day
  FROM pcards
  WHERE "Year" = 2014 AND "AgencyName" = 'OKLAHOMA STATE UNIVERSITY'
    AND "Amount" > 0
), flagged AS (
  SELECT {group}, COUNT(*) AS purchase_count, SUM("Amount") AS combined_amount
  FROM positive WHERE day IS NOT NULL
  GROUP BY {group}
  HAVING {condition} AND SUM("Amount") > 5000
)
SELECT p.*, g.purchase_count, g.combined_amount
FROM positive p JOIN flagged g ON {join}
ORDER BY p.day, p."FullName", p."Vendor", p.source_row'''

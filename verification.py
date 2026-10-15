"""Compare Gemini's assignment result to the user's verified SQL, never substitute it."""
from collections import Counter
from functools import lru_cache
import json
from pathlib import Path
from database import connection, validate_sql

REFERENCES = json.loads((Path(__file__).parent / 'verified_queries.json').read_text(encoding='utf-8'))
BY_ID = {r['id']: r for r in REFERENCES}

def result_signature(sql):
    with connection() as conn:
        cursor = conn.execute(validate_sql(sql))
        columns = tuple(c[0] for c in cursor.description)
        rows = cursor.fetchmany(20001)
        if len(rows) > 20000:
            raise ValueError('Assignment verification exceeded its result limit.')
    return columns, Counter(rows)

@lru_cache(maxsize=22)
def reference_signature(reference_id):
    return result_signature(BY_ID[reference_id]['sql'])

def verify_generated(sql, reference_id):
    if reference_id not in BY_ID:
        raise ValueError('Gemini returned an unknown assignment reference. Please retry.')
    if result_signature(sql) != reference_signature(reference_id):
        raise ValueError('The generated SQL did not match the verified assignment result (columns, values or row counts). Please retry and name the Part and Control/Question number. No unverified results were displayed.')

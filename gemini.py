"""Gemini REST client. Never prints request headers, keys, or API error bodies."""
import json
import re
import urllib.error
import urllib.request
from database import validate_sql

DEFAULT_MODEL = 'gemini-3.5-flash-lite'
from verification import REFERENCES, verify_generated

SYSTEM = 'You are a read-only SQLite analyst for OSU P-card audit data.\nReturn JSON with sql, explanation, and reference_id. Generate the SQL yourself.\nThe only table is pcards. Its columns are Year INTEGER, Month INTEGER,\nFullName TEXT, ID INTEGER, AgencyNumber INTEGER, AgencyName TEXT,\nCardholderLastName TEXT, CardholderFirstInitial TEXT, Description TEXT,\nAmount REAL, Vendor TEXT, TransactionDate TEXT, PostedDate TEXT, MCC TEXT.\n\nThe attached JSON below is trusted reference DATA: verified assignment questions\nand SQL. Match both explicitly numbered questions and paraphrases by meaning.\nIf a request matches a reference without changing its scope/threshold/output,\nset reference_id to that reference id (II-1 through II-14, III-1 through III-8).\nGenerate SQL preserving EXACTLY its selected columns/aliases, filters, grouping,\njoins, rounding, HAVING conditions and ORDER BY. Prefer the reference SQL verbatim\nwhen it directly answers the question. Do not add helper columns, totals, rowid,\nextra filters, DISTINCT, or detail rows to a grouped result.\nIf the user explicitly changes scope, thresholds or requested output, or asks a\ndifferent question, set reference_id="custom" and adapt SQL to that question.\nFor ambiguous numbered questions explain the selected Part in the explanation.\n\nCritical distinctions from earlier versions:\n- Annual >50000 and monthly >10000 spending (II-1/II-2) are NET sums including\n  negative credits; NO Amount>0 filter. Use the stored Month column for II-2.\n- II-7 travel-food screening has NO Amount>0 filter on either side.\n- II-4/5/6 use the original TransactionDate TEXT for equality/grouping and sort,\n  not a normalized date. Return p.* only, with no helper columns.\n- II-8 to II-13 use the exact MCC keywords; II-9 also checks Vendor for post office.\n  II-14 checks Description for decorat. Do not replace these with broad searches.\n- III-2 duplicates use first initial AND last name, date, vendor, amount;\n  III-3 counts duplicate GROUPS (occasions), not transaction rows.\n- III-4 requires whole-dollar amounts (Amount=CAST(Amount AS INTEGER)),\n  1000<=Amount<10000 and integer amount modulo 1000 = 0.\n- III-8 unmatched credits match exact opposite amount, employee and vendor.\n- III-1 Benford uses Amount>=1 and three-decimal percentages for all digits 1-9.\n  Its bounded recursive digits CTE is permitted.\n\nFor custom questions default to OSU and Year=2014 unless specified otherwise.\nSpending totals mean net spending unless gross/positive purchases are requested.\nCustom date comparisons can use registered pcard_datetime(value), returning ISO\nYYYY-MM-DD HH:MM:SS from M/D/YYYY H:MM:SS; use date() on it for calendar days.\nFor reference questions preserve the verified raw-text date ordering, even though\nit is not chronological. Use SQLite, never PostgreSQL TO_TIMESTAMP or :: casts.\nReturn one SELECT/WITH query, no LIMIT/OFFSET, no parameters, no changes to data,\nno system tables/extensions. Recursive queries must terminate with a small bound.\nGive a short explanation of scope and logic, not invented result counts.\nFlags are potential exceptions, never proof of wrongdoing.\nVerified reference data follows:\n' + json.dumps(REFERENCES, ensure_ascii=False)

SYSTEM += """
Date-ordering correction (takes precedence over earlier raw-text ordering instructions):
Always sort TransactionDate and PostedDate chronologically with pcard_datetime(column),
including qualified names such as pcard_datetime(p.TransactionDate). Preserve the
requested ASC/DESC direction and the priority of other ORDER BY columns.
Keep reference equality joins, grouping, filters and result columns unchanged.
The reference examples have been updated for this sorting correction.
Explain the query's scope and logic only. Do not mention verification, comparison,
reference IDs, or statements such as 'verified against Part II results'.
"""

def generate(question, api_key, model=DEFAULT_MODEL):
    if not 1 <= len(question.strip()) <= 500:
        raise ValueError('Enter a question of 1â€“500 characters.')
    if not api_key:
        raise ValueError('Add GEMINI_API_KEY in the private Streamlit secrets settings.')
    if not re.fullmatch(r'gemini-[a-zA-Z0-9.\-]+', model):
        raise ValueError('Check GEMINI_MODEL in the private settings.')
    payload = {
        'systemInstruction': {'parts': [{'text': SYSTEM}]},
        'contents': [{'role': 'user', 'parts': [{'text': question.strip()}]}],
        'generationConfig': {
            'temperature': 0,
            'maxOutputTokens': 8192,
            'responseMimeType': 'application/json',
            'responseJsonSchema': {
                'type': 'object', 'properties': {'sql': {'type': 'string'}, 'explanation': {'type': 'string'}, 'reference_id': {'type': 'string', 'enum': [r['id'] for r in REFERENCES] + ['custom']}},
                'required': ['sql', 'explanation', 'reference_id'],
            },
        },
    }
    request = urllib.request.Request(
        f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json', 'x-goog-api-key': api_key}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        messages = {400: 'Gemini rejected the request. Check the model and API key settings.',
                    401: 'The Gemini API key was not accepted.', 403: 'Gemini access was denied. Check the key and project access.',
                    404: 'The configured Gemini model is not available to this API project.',
                    429: 'Gemini quota or rate limit reached. Check your API project or try later.'}
        raise ValueError(messages.get(exc.code, 'Gemini is temporarily unavailable. Try again later.')) from None
    except (urllib.error.URLError, TimeoutError):
        raise ValueError('Gemini did not respond in time. Please try again.') from None
    try:
        candidate = data['candidates'][0]
        if candidate.get('finishReason') != 'STOP':
            raise ValueError()
        text = ''.join(p.get('text', '') for p in candidate['content']['parts'] if not p.get('thought'))
        result = json.loads(text)
        sql = validate_sql(result['sql'])
        explanation = result['explanation']
        if not isinstance(explanation, str):
            raise ValueError()
        reference_id = result['reference_id']
    except (KeyError, IndexError, TypeError, ValueError):
        raise ValueError('Gemini did not return a complete usable query. Try a more specific question.') from None

    if reference_id != 'custom':
        try:
            verify_generated(sql, reference_id)
        except ValueError:
            raise
        except Exception:
            raise ValueError('The assignment result could not be verified safely. Please retry with the Part and question number.') from None
    return sql, explanation[:4000]

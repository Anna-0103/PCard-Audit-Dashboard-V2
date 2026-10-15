import json
import os
from pathlib import Path
import sqlite3
import time
import pandas as pd
import streamlit as st
from database import PAGE_SIZE, SORT_COLUMNS, query_page, search_query, split_query, years
from gemini import DEFAULT_MODEL, generate

st.set_page_config(page_title='OSU P-card Audit', page_icon='📊', layout='wide')
st.markdown('''<style>
.stApp {background:#f4f7fb;color:#19283a}
.hero {background:#102a43;color:white;padding:2rem;border-radius:14px;margin-bottom:1.5rem}
.hero p {color:#c8e5fb}.hero h1 {color:white;margin:0}
</style><div class="hero"><p>INTERNAL AUDIT WORKSPACE</p>
<h1>OSU P-card Audit</h1><p>Explore potential exceptions in purchasing-card transactions.
Results are indicators for follow-up, not findings of misconduct.</p></div>''', unsafe_allow_html=True)

def setting(name, default=''):
    value = os.environ.get(name)
    if value:
        return value
    try:
        return str(st.secrets.get(name, default))
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return default

@st.cache_data(show_spinner=False)
def available_years():
    return years()

def results(key):
    state = st.session_state.get(key)
    if not state:
        return
    st.caption(state.get('explanation', ''))
    st.markdown('**SQLite query used by the website**')
    st.code(state['sql'], language='sql')
    try:
        with st.spinner('Searching the bundled database…'):
            result = query_page(state['sql'], state.get('parameters', ()), state.get('page', 1))
    except (sqlite3.Error, ValueError):
        st.error('This query could not run safely or exceeded the 15-second limit. Try a simpler question or one of the assignment checks.')
        return
    state['page'] = result['page']
    total, page = result['total'], result['page']
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    first = (page - 1) * PAGE_SIZE + 1 if total else 0
    st.write(f"Showing rows {first:,}–{min(page * PAGE_SIZE, total):,} of {total:,}")
    prev, label, nxt = st.columns([1, 3, 1])
    if prev.button('Previous', disabled=page <= 1, key=key + '_prev'):
        state['page'] -= 1
        st.rerun()
    label.markdown(f'Page **{page:,}** of **{pages:,}**')
    if nxt.button('Next', disabled=page >= pages, key=key + '_next'):
        state['page'] += 1
        st.rerun()
    st.dataframe(pd.DataFrame(result['rows'], columns=result['headers']), hide_index=True, width='stretch')
    with st.expander('Executed pagination SQL and bound values'):
        st.code(result['sql'], language='sql')
        st.code(json.dumps(result['parameters'], ensure_ascii=False), language='json')
        st.caption('Question marks are safely bound values, in order. Counts use the same query without pagination.')

try:
    year_options = available_years()
except Exception:
    st.error('The bundled database could not be opened. Check that data/pcards.db.gz and data/manifest.json were deployed together.')
    st.stop()

ask, dashboard = st.tabs(['Ask the database', 'Prohibited purchases'])
with ask:
    st.subheader('Ask a question in plain language')
    st.write('For example: “Which employees spent more than $10,000 in March 2014?”')
    st.caption('The AI creates a read-only query; review the SQL before relying on the results. Your question and the table structure are sent to Google Gemini. Transaction results are not sent.')
    key = setting('GEMINI_API_KEY')
    model = setting('GEMINI_MODEL', DEFAULT_MODEL)
    if not key:
        st.info('Natural-language questions need GEMINI_API_KEY in the private Streamlit secrets settings. Search and assignment checks below are ready to use.')
    with st.form('question_form'):
        question = st.text_area('Your audit question', max_chars=500)
        submitted = st.form_submit_button('Ask question', disabled=not bool(key))
    if submitted:
        st.session_state.pop('ask_result', None)
        if time.monotonic() - st.session_state.get('last_ask', -100) < 5:
            st.warning('Please wait a few seconds between questions.')
        else:
            st.session_state['last_ask'] = time.monotonic()
            try:
                with st.spinner('Creating a SQLite query…'):
                    sql, explanation = generate(question, key, model)
                st.session_state['ask_result'] = dict(sql=sql, explanation=explanation, page=1)
            except ValueError as exc:
                st.error(str(exc))
            except Exception:
                st.error('The question could not be processed. Check your private API settings and try again.')
    with st.expander('Run an assignment split-purchase check'):
        kind = st.selectbox('Audit control', ['Same cardholder, vendor and day', 'Two cardholders at one vendor', 'One cardholder at two vendors'])
        st.caption('2014 · Oklahoma State University · positive purchases · combined amount over $5,000. These checks do not call Gemini.')
        if st.button('Run check'):
            st.session_state['ask_result'] = dict(sql=split_query(kind), explanation=kind + ': potential exceptions only.', page=1)
    results('ask_result')
with dashboard:
    st.subheader('Search possible prohibited purchases')
    st.write('Choose a year and search descriptions or vendors. Try alcohol, gift, gas, post office, or membership.')
    st.caption('As in the original dashboard, keyword searches include all agencies and positive and negative amounts for the selected year. % and _ are SQL wildcard characters.')
    with st.form('search_form'):
        a, b = st.columns(2)
        year = a.selectbox('Year', year_options)
        field = b.selectbox('Search type', ['Description', 'Vendor'])
        keyword = st.text_input('Keyword', max_chars=80)
        search = st.form_submit_button('Search transactions')
    a, b = st.columns(2)
    sort = a.selectbox('Sort all matching transactions by', list(SORT_COLUMNS))
    direction = b.selectbox('Sort direction', ['Descending', 'Ascending'])
    if search:
        st.session_state.pop('search_result', None)
        st.session_state.pop('search_filters', None)
        try:
            search_query(year, field, keyword, sort, direction)
            st.session_state['search_filters'] = (year, field, keyword)
        except ValueError as exc:
            st.error(str(exc))
    if 'search_filters' in st.session_state:
        sql, parameters = search_query(*st.session_state['search_filters'], sort, direction)
        old = st.session_state.get('search_result', {})
        if old.get('sql') != sql or old.get('parameters') != parameters:
            st.session_state['search_result'] = dict(sql=sql, parameters=parameters, page=1)
    results('search_result')

with st.expander('About the data and SQL'):
    manifest = json.loads((Path(__file__).parent / 'data/manifest.json').read_text())
    st.write(f"{manifest['rows']:,} original transactions · 2010–2014 · bundled SQLite snapshot.")
    st.write('Dates are stored in the original M/D/YYYY format. The app registers pcard_datetime(value), which converts them to sortable ISO dates. Use date(pcard_datetime("TransactionDate")) for a calendar day. Invalid dates become NULL.')
    st.caption('Read-only database connections, a SQL operation/function allowlist, and a 15-second query deadline protect the data. Gemini output can still contain analytical mistakes. The data are a snapshot and do not sync with Supabase.')

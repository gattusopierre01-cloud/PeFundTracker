"""Google Sheets persistence for the MosaiQ Fund Tracker.
Stores the whole data blob (funds/triggers/statuses/dismissed) as JSON text in a
single worksheet, chunked across rows so it never hits the per-cell size limit.
Credentials + sheet id come from Streamlit secrets — never from the repo."""
import json
import streamlit as st

SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]
DATA_WS = "data"
CHUNK = 45000


def available():
    """True only if the secrets needed to connect are present."""
    try:
        return ("gcp" in st.secrets) and ("sheet_id" in st.secrets)
    except Exception:
        return False


@st.cache_resource(show_spinner=False)
def _client():
    import gspread
    from google.oauth2.service_account import Credentials
    info = json.loads(st.secrets["gcp"]["service_account_json"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _sheet():
    return _client().open_by_key(st.secrets["sheet_id"])


def load_blob():
    """Return the stored JSON string, or None if nothing saved yet."""
    sh = _sheet()
    try:
        ws = sh.worksheet(DATA_WS)
    except Exception:
        return None
    text = "".join(ws.col_values(1)).strip()
    return text or None


def save_blob(text):
    """Overwrite the stored blob, chunked across column A."""
    sh = _sheet()
    try:
        ws = sh.worksheet(DATA_WS)
    except Exception:
        ws = sh.add_worksheet(title=DATA_WS, rows=200, cols=1)
    ws.clear()
    chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or [""]
    ws.append_rows([[ch] for ch in chunks], value_input_option="RAW")

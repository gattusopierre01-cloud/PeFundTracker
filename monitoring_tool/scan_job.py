"""Headless daily scan for the MosaiQ Fund Tracker.
Runs on a schedule via GitHub Actions: reads the Google Sheet, scans every fund,
auto-logs new fresh STRONG signals back to the Sheet (capped), and emails a digest.
Credentials come from environment variables (GitHub Actions secrets), never the repo.
"""
import os
import json
import smtplib
import datetime
from email.mime.text import MIMEText

import gspread
from google.oauth2.service_account import Credentials
import tracker_core as c

SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]
DATA_WS = "data"
CHUNK = 45000
MAX_AUTOLOG = 25          # never auto-log more than this per run
APP_URL = "https://pefundtracker-uy3sarfbvtgudjq7dhxbyy.streamlit.app"


def _sheet():
    info = json.loads(os.environ["GCP_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds).open_by_key(os.environ["SHEET_ID"])


def load_data(sh):
    try:
        ws = sh.worksheet(DATA_WS)
    except Exception:
        return {}
    text = "".join(ws.col_values(1)).strip()
    return json.loads(text) if text else {}


def save_data(sh, data):
    try:
        ws = sh.worksheet(DATA_WS)
    except Exception:
        ws = sh.add_worksheet(title=DATA_WS, rows=200, cols=1)
    text = json.dumps(data, indent=2, ensure_ascii=False)
    ws.clear()
    chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or [""]
    ws.append_rows([[ch] for ch in chunks], value_input_option="RAW")


def send_email(subject, body):
    user = os.environ["SMTP_USER"]
    pw = os.environ["SMTP_PASS"]
    to = os.environ.get("EMAIL_TO", user)
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    with smtplib.SMTP_SSL(host, port) as srv:
        srv.login(user, pw)
        srv.send_message(msg)


def build_digest(rows, n_new):
    today = datetime.date.today().isoformat()
    if not rows:
        return f"MosaiQ Fund Tracker — {today}\n\nNo active signals in the queue today."
    out = [f"MosaiQ Fund Tracker — {today}",
           f"{n_new} new signal(s) logged this morning. Your queue:\n"]
    for tier in ("T1", "T2", "T3"):
        tr = [r for r in rows if r["tier"] == tier]
        if not tr:
            continue
        out.append(f"=== {tier} — {c.TIER_ACTION[tier]} ===")
        for r in tr[:15]:
            out.append(f"• {r['firm']} ({c.region_for(r['firm'])}) — "
                       f"{r['lead']}, {r['age_days']}d old  ->  {r['receiver']}")
        out.append("")
    out.append(f"Open the tracker to draft outreach / confirm / dismiss:\n{APP_URL}")
    return "\n".join(out)


def _age(d):
    try:
        return (datetime.date.today() - datetime.date.fromisoformat(d)).days
    except Exception:
        return 999


def main():
    sh = _sheet()
    data = load_data(sh)
    funds = data.get("funds", [])
    triggers = data.get("triggers", [])
    dismissed = set(data.get("dismissed", []))
    if not funds:
        print("No funds in the sheet; nothing to scan.")
        return

    found = []
    for f in funds:
        try:
            found.extend(c.scan_fund(f))
        except Exception as e:
            print(f"scan failed for {f}: {e}")
    try:
        found.extend(c.scan_trade_press(funds))
    except Exception as e:
        print(f"trade-press scan failed: {e}")

    logged = {(t["firm"], t["date"], t["type"]) for t in triggers}
    raw = [x for x in found
           if (x["firm"], x["date"], x["suggested_type"]) not in logged
           and c.dismiss_key(x["firm"], x["title"]) not in dismissed]
    strong = [x for x in c.dedupe_candidates(raw) if x["is_strong"]]

    # only auto-log genuinely fresh signals, and cap the count
    fresh = [x for x in strong if _age(x["date"]) <= c.FRESH_DAYS]
    to_log = fresh[:MAX_AUTOLOG]
    overflow = len(strong) - len(to_log)

    for x in to_log:
        triggers.append({"firm": x["firm"], "type": x["suggested_type"],
                         "date": x["date"], "source": x["source"], "note": x["title"]})
    data["triggers"] = triggers
    save_data(sh, data)
    print(f"{len(to_log)} new signal(s) logged (of {len(strong)} strong candidates).")

    rows = c.build_queue(funds, triggers)
    body = build_digest(rows, len(to_log))
    if overflow > 0:
        body += (f"\n\n({overflow} more candidate signal(s) found but not auto-logged — "
                 f"open the app's Scan tab to review and add them.)")
    try:
        send_email(f"MosaiQ Fund Tracker — {len(to_log)} new signal(s)", body)
        print("digest email sent.")
    except Exception as e:
        print(f"email failed: {e}")


if __name__ == "__main__":
    main()

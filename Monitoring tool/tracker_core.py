"""Core logic for the MosaiQ Fund Tracker — no Streamlit here so it's testable on its own."""
from datetime import datetime, timezone, date
from urllib.parse import quote_plus
import feedparser

# --- Trigger taxonomy (mirror of trigger_monitor.py) ---
TRIGGER_TYPES = {
    "new_platform":      {"label": "New platform acquisition",            "base": 10, "receiver": "Operating Partner / Head of Value Creation"},
    "new_op_hire":       {"label": "New Operating / Tech Operating Partner","base": 10, "receiver": "the new hire, directly"},
    "add_on":            {"label": "Add-on / bolt-on acquisition",         "base": 9,  "receiver": "Operating Partner / portco CFO"},
    "fund_close":        {"label": "New fund close",                       "base": 7,  "receiver": "Operating Partner / IR"},
    "ai_value_language": {"label": "Public AI / value-creation statement", "base": 7,  "receiver": "Operating Partner / Managing Partner"},
    "portco_ops_hiring": {"label": "Portco posting ops/transformation roles","base": 5, "receiver": "portco COO/CFO (cc fund OP)"},
    "portco_leadership": {"label": "Portco leadership change",             "base": 5,  "receiver": "the incoming executive"},
    "exit_prep":         {"label": "Exit / sale-prep signal",              "base": 5,  "receiver": "Operating Partner / Managing Partner"},
}

# Keyword cues used to guess a trigger type from a news headline during a scan.
SCAN_KEYWORDS = {
    "new_platform":      ["acquires", "acquisition", "invests in", "investment in", "majority stake", "backs", "buyout", "takes stake"],
    "add_on":            ["add-on", "bolt-on", "buy-and-build", "acquires", "add on"],
    "new_op_hire":       ["operating partner", "head of value creation", "appoints", "hires", "joins as", "new partner", "names"],
    "fund_close":        ["closes fund", "raises", "final close", "fund close", "oversubscribed", "hard cap"],
    "ai_value_language": ["artificial intelligence", " ai ", "digital transformation", "value creation"],
    "exit_prep":         ["explores sale", "considers sale", "to sell", "exit", "ipo", "divests"],
}

DECAY_WINDOW_DAYS = 60
FRESH_DAYS = 14
T1_THRESHOLD = 12
T2_THRESHOLD = 6
TIER_ACTION = {
    "T1": "Act today — build the full gameplan for this firm.",
    "T2": "Within 48h — send the matched trigger sequence, log in CRM.",
    "T3": "Nurture — watch; light-touch content / warm-connection path.",
}


def today():
    return datetime.now(timezone.utc).date()


def _parse(d):
    return datetime.strptime(d, "%Y-%m-%d").date()


def decayed_points(t, ref):
    meta = TRIGGER_TYPES.get(t["type"])
    if not meta:
        return 0.0
    age = (ref - _parse(t["date"])).days
    age = max(age, 0)
    if age >= DECAY_WINDOW_DAYS:
        return 0.0
    return meta["base"] * (1 - age / DECAY_WINDOW_DAYS)


def score_firm(triggers, ref):
    active, score, fresh_strong = [], 0.0, False
    for t in triggers:
        pts = decayed_points(t, ref)
        if pts <= 0:
            continue
        age = (ref - _parse(t["date"])).days
        meta = TRIGGER_TYPES[t["type"]]
        active.append({**t, "points": round(pts, 1), "age_days": age, "label": meta["label"]})
        score += pts
        if age <= FRESH_DAYS and meta["base"] >= 9:
            fresh_strong = True
    active.sort(key=lambda x: x["points"], reverse=True)
    return round(score, 1), active, fresh_strong


def tier_for(score, active, fresh_strong):
    if not active:
        return None
    if score >= T1_THRESHOLD or (fresh_strong and len(active) >= 2):
        return "T1"
    if score >= T2_THRESHOLD or fresh_strong:
        return "T2"
    return "T3"


def build_queue(funds, triggers):
    """funds: list of firm names. triggers: list of {firm,type,date,...}. Returns ranked rows."""
    ref = today()
    by_firm = {}
    for t in triggers:
        by_firm.setdefault(t["firm"], []).append(t)
    rows = []
    for firm in {*funds, *by_firm}:
        score, active, fresh = score_firm(by_firm.get(firm, []), ref)
        tier = tier_for(score, active, fresh)
        if tier is None:
            continue
        top = active[0]
        rows.append({
            "firm": firm, "score": score, "tier": tier,
            "lead": top["label"], "age_days": top["age_days"],
            "receiver": TRIGGER_TYPES[top["type"]]["receiver"],
            "action": TIER_ACTION[tier], "n_active": len(active),
        })
    order = {"T1": 0, "T2": 1, "T3": 2}
    rows.sort(key=lambda r: (order[r["tier"]], -r["score"]))
    return rows


def guess_type(title):
    """Guess a trigger type from a headline; return (type, label) or (None, None)."""
    low = f" {title.lower()} "
    for ttype, kws in SCAN_KEYWORDS.items():
        if any(kw in low for kw in kws):
            return ttype, TRIGGER_TYPES[ttype]["label"]
    return None, None


def scan_fund(firm, max_items=8):
    """On-demand: query Google News RSS for a firm, return candidate signals.
    Network call lives here. Returns list of dicts (not yet logged)."""
    q = quote_plus(f'"{firm}" (acquires OR acquisition OR invests OR appoints OR raises OR fund OR partner)')
    url = f"https://news.google.com/rss/search?q={q}&hl=en-GB&gl=GB&ceid=GB:en"
    feed = feedparser.parse(url)
    out = []
    for e in feed.entries[:max_items]:
        title = getattr(e, "title", "")
        ttype, label = guess_type(title)
        if not ttype:
            continue
        try:
            pub = datetime(*e.published_parsed[:6]).date().isoformat()
        except Exception:
            pub = today().isoformat()
        out.append({
            "firm": firm,
            "title": title,
            "suggested_type": ttype,
            "suggested_label": label,
            "date": pub,
            "source": getattr(e, "link", ""),
        })
    return out

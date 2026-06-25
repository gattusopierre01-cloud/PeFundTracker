"""Core logic for the MosaiQ Fund Tracker — no Streamlit here so it's testable on its own."""
from datetime import datetime, timezone, date
from urllib.parse import quote_plus
import re
import feedparser

# === 1. POINT WEIGHTS (re-prioritised around the Operating-Partner ICP) =====
# A new value-creation owner you can call by name is the best "act now"; a fresh
# portco (to run ProcessX on) and a bolt-on (duplicated workflows) are next.
TRIGGER_TYPES = {
    "new_op_hire":       {"label": "New Operating / Value-Creation Partner", "base": 10, "receiver": "the new hire, directly"},
    "new_platform":      {"label": "New platform acquisition",              "base": 9,  "receiver": "Operating Partner / Head of Value Creation"},
    "add_on":            {"label": "Add-on / bolt-on acquisition",          "base": 8,  "receiver": "Operating Partner / portco CFO"},
    "fund_close":        {"label": "New fund close",                        "base": 6,  "receiver": "Operating Partner / IR"},
    "exit_prep":         {"label": "Exit / sale-prep signal",               "base": 5,  "receiver": "Operating Partner / Managing Partner"},
    "portco_leadership": {"label": "Portco leadership change (CEO/CFO)",     "base": 5,  "receiver": "the incoming executive"},
    "portco_ops_hiring": {"label": "Portco ops/transformation hiring",      "base": 4,  "receiver": "portco COO/CFO (cc fund OP)"},
    "ai_value_language": {"label": "Public AI / value-creation statement",  "base": 4,  "receiver": "Operating Partner / Managing Partner"},
}
# "Strong" = worth surfacing automatically and acting on alone when fresh.
STRONG_TYPES = {"new_op_hire", "new_platform", "add_on"}

# === 2. KEYWORD CUES (tightened: an action word is required, generic =========
# mentions are dropped). Checked in this order so the most specific wins.
# ai/value-creation bare words removed — too noisy; portco_ops_hiring isn't a
# news signal (it's a job-board one) so it's manual-only, not auto-detected.
SCAN_KEYWORDS = [
    ("add_on",            ["add-on", "bolt-on", "add on", "bolt on", "buy-and-build", "buy and build"]),
    ("new_op_hire",       ["operating partner", "head of value creation", "value creation partner", "chief operating partner"]),
    ("new_platform",      ["acquires", "acquisition of", "majority stake", "majority investment", "takes majority",
                           "invests in", "investment in", "backs", "take-private", "to take private", "take private",
                           "recapitalis", "recapitaliz", "carve-out", "carve out"]),
    ("fund_close",        ["closes fund", "final close", "holds final close", "raises new fund", "new fund",
                           "oversubscribed", "hard cap", "fund close", "closes its fund"]),
    ("exit_prep",         ["explores sale", "considers sale", "weighs sale", "mulls sale", "puts up for sale",
                           "to sell", "exits", "divests", "sells stake", "completes sale"]),
    ("portco_leadership", ["new ceo", "new cfo", "appoints ceo", "appoints cfo", "names ceo", "names cfo"]),
    ("ai_value_language", ["ai value creation", "ai-driven value", "digital value creation"]),
]

# === 3. TIERS + DECAY (tightened to the outreach window) ====================
DECAY_WINDOW_DAYS = 45   # a signal contributes 0 once it's this old (was 60)
FRESH_DAYS = 21          # "fresh" = act-on-alone window (was 14)
T1_THRESHOLD = 14
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
    age = max((ref - _parse(t["date"])).days, 0)
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
        if age <= FRESH_DAYS and t["type"] in STRONG_TYPES:
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
        rows.append({"firm": firm, "score": score, "tier": tier, "lead": top["label"],
                     "age_days": top["age_days"], "receiver": TRIGGER_TYPES[top["type"]]["receiver"],
                     "action": TIER_ACTION[tier], "n_active": len(active)})
    order = {"T1": 0, "T2": 1, "T3": 2}
    rows.sort(key=lambda r: (order[r["tier"]], -r["score"]))
    return rows


def guess_type(title):
    """Classify a headline. Requires a real action phrase; returns (type,label) or (None,None)."""
    low = f" {title.lower()} "
    for ttype, kws in SCAN_KEYWORDS:
        if any(kw in low for kw in kws):
            return ttype, TRIGGER_TYPES[ttype]["label"]
    return None, None


def _title_key(title):
    # strip the " - Outlet" suffix Google News appends, lowercase, keep words
    core = re.split(r"\s+-\s+[^-]+$", title)[0]
    return re.sub(r"[^a-z0-9 ]", "", core.lower()).strip()


def dedupe_candidates(cands):
    """Collapse the same event reported by multiple outlets.
    One entry per (firm, type), keeping the most recent; records how many headlines merged."""
    groups = {}
    for c in cands:
        key = (c["firm"], c["suggested_type"])
        groups.setdefault(key, []).append(c)
    out = []
    for (firm, ttype), items in groups.items():
        items.sort(key=lambda x: x["date"], reverse=True)
        best = dict(items[0])
        best["n_headlines"] = len(items)
        best["is_strong"] = ttype in STRONG_TYPES
        out.append(best)
    # strong first, then by recency
    out.sort(key=lambda x: (not x["is_strong"], x["date"]), reverse=False)
    out.sort(key=lambda x: x["is_strong"], reverse=True)
    return out


def scan_fund(firm, max_items=10):
    q = quote_plus(f'"{firm}" (acquires OR invests OR appoints OR "operating partner" OR raises OR "bolt-on" OR sells)')
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
        out.append({"firm": firm, "title": title, "suggested_type": ttype,
                    "suggested_label": label, "date": pub, "source": getattr(e, "link", "")})
    return out

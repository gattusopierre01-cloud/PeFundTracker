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
                     "lead_type": top["type"], "headline": top.get("note") or top.get("source") or "",
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


# ============================================================================
#  v3 ADDITIONS — wider sources, outreach drafts, region map, dismiss keys
# ============================================================================

# --- Wider sources: PE trade press (catches OP-hire & people moves Google ---
#     News usually misses). Site-wide feeds; we match watched fund names in
#     titles. NOTE: feed URLs can change — each is tried independently and any
#     that fails to load is skipped, so the scan never breaks on a dead feed.
TRADE_FEEDS = [
    ("Real Deals",          "https://realdeals.eu.com/feed"),
    ("PE News",             "https://www.penews.com/rss"),
    ("Private Equity Wire", "https://www.privateequitywire.co.uk/feed"),
    ("Private Equity Intl", "https://www.privateequityinternational.com/feed/"),
]


def scan_trade_press(funds, max_per_feed=60):
    """Pull trade-press feeds once each and raise a candidate when a watched
    fund name appears in a headline. Complements the per-fund Google News scan."""
    out = []
    names = [(f, f.lower()) for f in funds if len(f) > 3]
    for source_name, feed_url in TRADE_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
        except Exception:
            continue
        for e in feed.entries[:max_per_feed]:
            title = getattr(e, "title", "")
            low = title.lower()
            hit = next((f for f, fl in names if fl in low), None)
            if not hit:
                continue
            ttype, label = guess_type(title)
            if not ttype:
                continue
            try:
                pub = datetime(*e.published_parsed[:6]).date().isoformat()
            except Exception:
                pub = today().isoformat()
            out.append({"firm": hit, "title": f"{title} — {source_name}",
                        "suggested_type": ttype, "suggested_label": label,
                        "date": pub, "source": getattr(e, "link", "")})
    return out


# --- Signal -> ready draft. Short (<200w), honest-broker, no invented metrics,
#     references the trigger, drives to the 15-min call. Swap [Loom] for yours.
CALENDLY = "https://calendly.com/d/d3sx-q4t-jcg"

OUTREACH = {
    "new_op_hire":
        "Hi {first} — congratulations on the move to {firm}.\n\n"
        "When a value-creation mandate is fresh, the first question is usually *where* the "
        "operational upside actually sits across the portfolio. That's what we do at MosaiQ: "
        "ProcessX gives a fast, tool-agnostic read on the process-level value in a business "
        "from public information — an honest-broker map, not a platform pitch.\n\n"
        "Happy to run one free on a portfolio company of your choosing so you can see the "
        "output, no strings. Worth 15 minutes? {calendly}\n\n[Loom: 90-sec demo]",
    "new_platform":
        "Hi {first} — saw {firm}'s new investment ({headline}).\n\n"
        "The first 100 days are when a process baseline is most useful and least disruptive. "
        "MosaiQ's ProcessX produces a tool-agnostic read of where operational/process value "
        "sits in a business, from public info — useful as an independent second view alongside "
        "your own value-creation plan.\n\n"
        "I'd be glad to run one free on the new portfolio company so you can judge it on output. "
        "15 minutes to walk through it? {calendly}\n\n[Loom: 90-sec demo]",
    "add_on":
        "Hi {first} — noted the recent bolt-on ({headline}).\n\n"
        "Integrations are where duplicated processes quietly accumulate. ProcessX gives a fast, "
        "tool-agnostic read on process-level value across the combined business from public "
        "information — an independent map to sit beside your integration plan.\n\n"
        "I can run one free on the combined entity so you can see the output. Worth 15 minutes? "
        "{calendly}\n\n[Loom: 90-sec demo]",
    "fund_close":
        "Hi {first} — congratulations on the close ({headline}).\n\n"
        "As capital goes to work, a quick read on where operational value sits helps prioritise "
        "the value-creation agenda early. ProcessX does exactly that — tool-agnostic, from public "
        "information, as an honest-broker view.\n\n"
        "Happy to run one free on a current portfolio company. 15 minutes? {calendly}\n\n[Loom: 90-sec demo]",
    "exit_prep":
        "Hi {first} — {headline}.\n\n"
        "Ahead of a process, a clean, independent process map can de-risk diligence and support "
        "the operational story. ProcessX produces that from public information — tool-agnostic, "
        "no platform commitment.\n\n"
        "I'd be glad to run one free so you can see the output. 15 minutes? {calendly}\n\n[Loom: 90-sec demo]",
    "portco_leadership":
        "Hi {first} — congratulations on the new role.\n\n"
        "New leaders often want a fast, independent baseline of where operational/process value "
        "sits. ProcessX gives exactly that from public information — tool-agnostic, an honest read "
        "rather than a sales pitch.\n\n"
        "Happy to run one free on the business so you can judge it on output. Worth 15 minutes? "
        "{calendly}\n\n[Loom: 90-sec demo]",
    "_default":
        "Hi {first} — saw the recent news at {firm} ({headline}).\n\n"
        "MosaiQ's ProcessX gives a fast, tool-agnostic read on where operational/process value "
        "sits in a business, from public information — an honest-broker map.\n\n"
        "I'd be glad to run one free so you can see the output. 15 minutes? {calendly}\n\n[Loom: 90-sec demo]",
}


def draft_outreach(firm, ttype, headline=""):
    tmpl = OUTREACH.get(ttype, OUTREACH["_default"])
    return tmpl.format(first="[first name]", firm=firm,
                       headline=headline or "recent news", calendly=CALENDLY)


def linkedin_people_url(firm):
    from urllib.parse import quote_plus as _q
    return ("https://www.linkedin.com/search/results/people/?keywords="
            + _q(f'{firm} operating partner'))


# --- Region map (reuses the 240-fund master list as the single source) ------
_REGION_GROUPS = {
"UK & Ireland": ["Key Capital Partners","Limerston Capital","WestBridge Capital","YFM Equity Partners","Penta Capital","Perwyn","Vespa Capital","Volpi Capital","Baird Capital","EMK Capital","Silverfleet Capital","Equistone Partners Europe","Harwood Private Equity","3i Group","LDC","Livingbridge","Bowmark Capital","Graphite Capital","Phoenix Equity Partners","Bridgepoint","TowerBrook Capital Partners","Searchlight Capital Partners","Permira","Inflexion","ECI Partners","NorthEdge Capital","BGF"],
"France": ["Naxicap Partners","Activa Capital","Azulis Capital","Parquest Capital","Qualium Investissement","Siparex","Sparring Capital","TCR Capital","Turenne Capital","UI Investissement","LBO France","Cerea Partners","Argos Wityu","21 Invest","Meanings Capital Partners","Eurazeo","Eurazeo PME","Five Arrows","Ardian","Astorg","Apax Partners France","Flexstone Partners","Access Capital Partners","SWEN Capital Partners","NextStage AM","Wendel","Sodero Gestion","LT Capital","Arkea Capital","Ouest Croissance","Amethis","Montefiore Investment","Metric Capital Partners","PAI Partners","Merieux Equity Partners","Tikehau Capital","Truffle Capital","Unexo","RAISE","Pergam","Amundi Private Equity Funds","Seven2"],
"DACH": ["Capvis","AUCTUS Capital Partners","DPE Deutsche Private Equity","EMERAM Capital Partners","Maxburg Capital Partners","capiton","Hannover Finanz","VR Equitypartner","Finatem","Paragon Partners","Nord Holding","ODEWALD","AFINUM","ECM Equity Capital Management","Lafayette Mittelstand Capital","NEXX Capital","Beyond Capital Partners","Accursia Capital","Ufenau Capital Partners","Deutsche Beteiligungs AG","Bregal Unternehmerkapital","Adiuva Capital","Nordwind Capital","BWK","CornerstoneCapital","Astorius","Elvaston Capital Management","COI Partners","DEDIQ","AdAstra"],
"US": ["Altamont Capital Partners","NewSpring Capital","Prospect Capital Management","Transom Capital Group","VSS Capital Partners","Align Capital Partners","Source Capital","ShoreView Industries","Baymark Partners","Heritage Holding","Hidden Harbor Capital Partners","Dauntless Capital Partners","Pfingsten Partners","Rockwood Equity Partners","Gauge Capital","Portrait Capital","Sleeping Giant Capital","JLL Partners","H.I.G. Capital","Littlejohn & Co.","Angelo Gordon","Bruckmann Rosser Sherrill & Co.","Freeman Spogli & Co.","J.H. Whitney & Company","J.W. Childs Associates","Thomas H. Lee Partners","Yucaipa Companies","L Catterton","Insight Partners","TA Associates","The Riverside Company"],
"Benelux": ["Bencis Capital Partners","Egeria","Avedon Capital Partners","Waterland Private Equity","Gilde Equity Management","Gilde Healthcare","Rivean Capital","Main Capital Partners","Mentha Capital","Sofindev","Plain Vanilla Investments","Bolster Investment Partners","Holland Capital","Standard Investment","IceLake Capital","PMH Investments","Pride Capital Partners","Airbridge Equity Partners"],
"Nordics": ["Accent Equity Partners","Amplio Private Equity","Segulah","MVI Advisors","Verdane","Nordic Capital","Altor Equity Partners","EQT","Triton Partners","FSN Capital","Polaris Private Equity","Axcel","GRO Capital","Summa Equity","Cubera","IK Partners"],
"Iberia": ["Magnum Industrial Partners","Portobello Capital","Miura Partners","Asterion Industrial Partners","Alantra Private Equity","ProA Capital","Buenavista Equity Partners (GED Capital)","Nazca Capital","Realza Capital","Espiga Capital","Talde","Aurica Capital","Black Toro Capital","Suma Capital","Sherpa Capital","Diana Capital","MCH Private Equity","Arta Capital","Alter Capital","Explorer Investments","Crescent","ECS Capital","Inter-Risco","Vallis Capital Partners","Iberis Capital","Oxy Capital","Atena Equity Partners","HCapital"],
"Italy": ["Investindustrial","FSI SGR","Clessidra Private Equity","Xenon Private Equity","Quadrivio Group","Alpha Private Equity","Alto Partners","Synergo Capital","Assietta Private Equity","Wise Equity","NB Renaissance","Charme Capital Partners","Progressio SGR","Ambienta SGR","Nextalia SGR","Vertis SGR","Aksia Group","ItalGlobal Partners","Equita Private Debt"],
"Canada": ["Northleaf Capital Partners","Birch Hill Equity Partners","Clairvest Group","TorQuest Partners","Altas Partners","Novacap","ONCAP","Onex","Fulcrum Capital Partners","Ironbridge Equity Partners","Argyle Capital","Parity Capital","CAI Capital Partners","Peloton Capital Management","Sagard Private Equity Canada","Cordiant Capital","Fengate Asset Management","Canadian Business Growth Fund","BMO Capital Partners","OMERS Private Equity","Dawson Partners","PRIVEQ Capital","Tandem Expansion","Catalyst Capital Group","Whitehorse Liquidity Partners","ARC Financial","Azimuth Capital Management","EdgeStone Capital Partners","Fulmer Capital Partners"],
}
REGIONS = {n: r for r, names in _REGION_GROUPS.items() for n in names}
REGION_LIST = list(_REGION_GROUPS.keys()) + ["Other"]


def region_for(firm):
    return REGIONS.get(firm, "Other")


PIPELINE_STAGES = ["Not contacted", "Contacted", "Replied", "Meeting", "Won", "Dead"]


def dismiss_key(firm, title):
    return f"{firm}||{_title_key(title)}"

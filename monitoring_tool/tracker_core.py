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
                           "buyout of", "take-private", "to take private", "take private",
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


def _is_actor(title, firm):
    """True only if the headline STARTS with the fund name (fund is the doer),
    not merely mentions it. Big precision lever against false positives."""
    core = re.split(r"\s+-\s+[^-]+$", title)[0]
    core = re.sub(r"^(exclusive|breaking|update|just in)\s*[:\-]\s*", "", core, flags=re.I)
    core = core.strip(" \"'\u2018\u2019\u201c\u201d")
    return core.lower().startswith(firm.lower())


def scan_fund(firm, max_items=10):
    q = quote_plus(f'"{firm}" (acquires OR invests OR appoints OR "operating partner" OR raises OR "bolt-on" OR sells)')
    url = f"https://news.google.com/rss/search?q={q}&hl=en-GB&gl=GB&ceid=GB:en"
    feed = feedparser.parse(url)
    out = []
    for e in feed.entries[:max_items]:
        title = getattr(e, "title", "")
        ttype, label = guess_type(title)
        if not ttype or not _is_actor(title, firm):
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
            if not hit or not _is_actor(title, hit):
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


def draft_outreach(firm, ttype, headline="", first="[first name]"):
    tmpl = OUTREACH.get(ttype, OUTREACH["_default"])
    return tmpl.format(first=first or "[first name]", firm=firm,
                       headline=headline or "recent news", calendly=CALENDLY)


# The people MosaiQ should reach at a fund: value-creation / operations decision-makers.
CONTACT_ROLES = "value creation operating partner portfolio operations COO"


def linkedin_people_url(firm, roles=CONTACT_ROLES):
    """Public LinkedIn people search, pre-filtered to the fund + the relevant roles."""
    from urllib.parse import quote_plus as _q
    return ("https://www.linkedin.com/search/results/people/?keywords="
            + _q(f'"{firm}" {roles}'))


def sales_nav_people_url(firm, roles=CONTACT_ROLES):
    """Sales Navigator people search (opens in the user's Sales Nav seat)."""
    from urllib.parse import quote_plus as _q
    return ("https://www.linkedin.com/sales/search/people?keywords="
            + _q(f'"{firm}" {roles}'))


def google_contact_url(firm):
    """Google fallback — often surfaces the exact profile as the top hit."""
    from urllib.parse import quote_plus as _q
    return ("https://www.google.com/search?q="
            + _q(f'{firm} (head of value creation OR operating partner OR '
                 f'portfolio director OR head of operations) LinkedIn'))


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


# ============================================================================
#  v4 — best-effort fund discovery from PE deal headlines
# ============================================================================
PE_SUFFIXES = ("capital", "partners", "equity", "ventures", "investments",
               "management", "advisors", "holdings", "group")
DISCOVER_QUERIES = [
    "private equity acquires", "private equity invests in", "private equity backs",
    "buyout firm acquires", "private equity bolt-on", "growth equity invests in",
]
_ACTION = re.compile(
    r"\b(acquires|acquire|invests in|invest in|backs|buys|to acquire|"
    r"completes acquisition of|takes majority|takes a stake|recapitalises|recapitalizes)\b",
    re.I)


def _extract_candidate(title):
    """Pull a likely fund name from the start of a deal headline, or None."""
    core = re.split(r"\s+-\s+[^-]+$", title)[0]          # drop " - Outlet"
    m = _ACTION.search(core)
    if not m:
        return None
    cand = core[:m.start()].strip(" .,–—-:'\"")
    words = cand.split()
    if not (2 <= len(words) <= 6):
        return None
    cl = cand.lower()
    if not any(cl.endswith(s) or (" " + s) in (" " + cl) for s in PE_SUFFIXES):
        return None
    return cand


# Unambiguous global mega-funds (AUM far above 15bn) — never suggest these in
# discovery. Conservative: clear giants only, no borderline European mid-caps.
MEGA_EXCLUDE = {
    "kkr", "blackstone", "carlyle", "the carlyle group", "apollo", "apollo global",
    "eqt", "cvc", "cvc capital partners", "advent international", "advent",
    "bain capital", "tpg", "tpg capital", "warburg pincus", "permira", "ardian",
    "cinven", "hellman & friedman", "hellman and friedman", "vista equity partners",
    "thoma bravo", "brookfield", "general atlantic", "silver lake", "leonard green",
    "clayton dubilier & rice", "cd&r", "bc partners", "hg", "hg capital",
    "francisco partners", "providence equity", "insight partners", "l catterton",
}


def _is_mega(name):
    n = name.lower()
    return any(n == m or n.startswith(m + " ") for m in MEGA_EXCLUDE)


def discover_funds(known, aum=None, max_aum=15.0, max_per_query=40):
    """Scan PE deal headlines for fund names not already on the watchlist.
    Excludes known mega-funds and any candidate recorded as larger than max_aum (bn).
    Heuristic — returns review candidates, not verified funds."""
    aum = aum or {}
    known_l = {k.lower() for k in known}
    found = {}
    for q in DISCOVER_QUERIES:
        url = f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-GB&gl=GB&ceid=GB:en"
        try:
            feed = feedparser.parse(url)
        except Exception:
            continue
        for e in feed.entries[:max_per_query]:
            title = getattr(e, "title", "")
            cand = _extract_candidate(title)
            if not cand or cand.lower() in known_l or _is_mega(cand):
                continue
            rec = aum.get(cand)
            if isinstance(rec, (int, float)) and rec > max_aum:
                continue
            key = cand.lower()
            if key not in found:
                found[key] = {"name": cand, "n": 0, "sample": title, "source": getattr(e, "link", "")}
            found[key]["n"] += 1
    return sorted(found.values(), key=lambda x: (-x["n"], x["name"]))


# ============================================================================
#  v5 — best-effort automated AUM lookup via Wikidata (free, partial coverage)
# ============================================================================
import urllib.request as _urlreq
import urllib.parse as _urlparse

_WD_API = "https://www.wikidata.org/w/api.php"
_WD_UA = {"User-Agent": "MosaiQ-Fund-Tracker/1.0"}
_AUM_PROP = "P2403"                       # Wikidata "assets under management"
_WD_CUR = {"Q4917": "USD", "Q4916": "EUR", "Q25224": "GBP", "Q4726": "JPY"}
_WD_DESC_HINTS = ("private equity", "investment", "asset management", "venture",
                  "buyout", "equity firm", "capital", "investment firm", "private-equity")


def _wd_get(params):
    url = _WD_API + "?" + _urlparse.urlencode({**params, "format": "json"})
    req = _urlreq.Request(url, headers=_WD_UA)
    with _urlreq.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _wd_entity_for(name):
    """Find a Wikidata entity id for a fund name; only accept finance-looking ones."""
    data = _wd_get({"action": "wbsearchentities", "search": name,
                    "language": "en", "type": "item", "limit": 5})
    for hit in data.get("search", []):
        desc = (hit.get("description") or "").lower()
        if any(h in desc for h in _WD_DESC_HINTS):
            return hit["id"]
    return None


def _parse_aum_claims(claims):
    """Return (aum_in_billions, currency) from P2403 claims, or None."""
    for cl in claims:
        try:
            val = cl["mainsnak"]["datavalue"]["value"]
            amt = float(val["amount"])
            unit_q = val.get("unit", "").rsplit("/", 1)[-1]
            return round(amt / 1e9, 2), _WD_CUR.get(unit_q, "")
        except Exception:
            continue
    return None


def fetch_aum_wikidata(funds, skip=None, cap=300):
    """Best-effort AUM (billions) from Wikidata. Partial coverage (mostly larger
    funds); figures can be dated; confirm values before trusting them.
    Returns {fund: {"aum_bn": float, "currency": str, "entity": qid}}."""
    import time
    skip = skip or set()
    out, n = {}, 0
    for f in funds:
        if f in skip or n >= cap:
            continue
        n += 1
        try:
            qid = _wd_entity_for(f)
            if not qid:
                continue
            data = _wd_get({"action": "wbgetclaims", "entity": qid, "property": _AUM_PROP})
            parsed = _parse_aum_claims(data.get("claims", {}).get(_AUM_PROP, []))
            if parsed:
                out[f] = {"aum_bn": parsed[0], "currency": parsed[1], "entity": qid}
        except Exception:
            continue
        time.sleep(0.1)
    return out

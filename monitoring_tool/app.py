import json
from datetime import date
import streamlit as st
import tracker_core as c

st.set_page_config(page_title="MosaiQ Fund Tracker", page_icon="📈", layout="wide")

# ---- load seed data once ----
def _load_seed():
    try:
        with open("seed_data.json", encoding="utf-8") as f:
            d = json.load(f)
        return d.get("funds", []), d.get("triggers", [])
    except Exception:
        return [], []

if "funds" not in st.session_state:
    f, t = _load_seed()
    st.session_state.funds = f
    st.session_state.triggers = t
    st.session_state.candidates = []

# ---- sidebar: save / load (free hosting has no persistent disk) ----
with st.sidebar:
    st.header("Your data")
    st.caption(
        "Free hosting doesn't keep data between restarts. **Download** to save your "
        "log, and **upload** it next time to pick up where you left off."
    )
    blob = json.dumps({"funds": st.session_state.funds, "triggers": st.session_state.triggers}, indent=2)
    st.download_button("⬇️ Download my data", blob, file_name="fund_tracker_data.json", mime="application/json")
    up = st.file_uploader("⬆️ Load saved data", type="json")
    if up and st.button("Load this file"):
        d = json.load(up)
        st.session_state.funds = d.get("funds", [])
        st.session_state.triggers = d.get("triggers", [])
        st.success("Loaded.")
        st.rerun()
    st.divider()
    st.metric("Funds tracked", len(st.session_state.funds))
    st.metric("Signals logged", len(st.session_state.triggers))

st.title("📈 MosaiQ Fund Tracker")

tab_queue, tab_scan, tab_watch, tab_log = st.tabs(
    ["🎯 Queue", "🔍 Run a scan", "📋 Watchlist", "✍️ Log a signal"]
)

# ---------------- QUEUE ----------------
with tab_queue:
    st.subheader("Who to act on")
    rows = c.build_queue(st.session_state.funds, st.session_state.triggers)
    if not rows:
        st.info("No active signals yet. Run a scan or log a signal to populate this.")
    else:
        colour = {"T1": "🔴", "T2": "🟠", "T3": "🟢"}
        for tier in ["T1", "T2", "T3"]:
            tier_rows = [r for r in rows if r["tier"] == tier]
            if not tier_rows:
                continue
            st.markdown(f"### {colour[tier]} {tier} — {c.TIER_ACTION[tier]}")
            for r in tier_rows:
                extra = f"  ·  +{r['n_active']-1} more signal(s)" if r["n_active"] > 1 else ""
                st.markdown(
                    f"**{r['firm']}**  ·  score {r['score']}  ·  _{r['lead']}_, {r['age_days']}d old{extra}  \n"
                    f"→ contact: {r['receiver']}"
                )
            st.divider()

# ---------------- SCAN ----------------
with tab_scan:
    st.subheader("On-demand scan")
    st.caption(
        "Checks each fund on your watchlist against free Google News results and surfaces "
        "candidate signals. Nothing is logged until you confirm it. "
        "(The live check only works on the deployed app, not in a preview.)"
    )
    if st.button("🔍 Run scan on my watchlist", type="primary"):
        if not st.session_state.funds:
            st.warning("Add some funds on the Watchlist tab first.")
        else:
            found = []
            prog = st.progress(0.0, "Scanning…")
            for i, firm in enumerate(st.session_state.funds, 1):
                try:
                    found.extend(c.scan_fund(firm))
                except Exception as e:
                    st.write(f"⚠️ couldn't scan {firm}: {e}")
                prog.progress(i / len(st.session_state.funds), f"Scanning… {firm}")
            prog.empty()
            # drop candidates already logged (same firm+date+type)
            seen = {(t["firm"], t["date"], t["type"]) for t in st.session_state.triggers}
            st.session_state.candidates = [
                x for x in found if (x["firm"], x["date"], x["suggested_type"]) not in seen
            ]
            st.success(f"Found {len(st.session_state.candidates)} candidate signal(s) to review.")

    if st.session_state.candidates:
        st.markdown("#### Review candidates — tick the real ones, then add")
        type_labels = {k: v["label"] for k, v in c.TRIGGER_TYPES.items()}
        keep = []
        for i, cand in enumerate(st.session_state.candidates):
            with st.container(border=True):
                st.markdown(f"**{cand['firm']}** — {cand['title']}")
                cc = st.columns([1, 2, 2, 3])
                take = cc[0].checkbox("Log it", key=f"k{i}")
                ttype = cc[1].selectbox(
                    "Type", list(type_labels), index=list(type_labels).index(cand["suggested_type"]),
                    format_func=lambda k: type_labels[k], key=f"t{i}",
                )
                dt = cc[2].text_input("Date", cand["date"], key=f"d{i}")
                cc[3].caption(cand["source"])
                if take:
                    keep.append({"firm": cand["firm"], "type": ttype, "date": dt,
                                 "source": cand["source"], "note": cand["title"]})
        if st.button("➕ Add selected to my log"):
            st.session_state.triggers.extend(keep)
            st.session_state.candidates = []
            st.success(f"Added {len(keep)} signal(s). Check the Queue tab.")
            st.rerun()

# ---------------- WATCHLIST ----------------
with tab_watch:
    st.subheader("Funds you track")
    new = st.text_input("Add a fund")
    if st.button("Add fund") and new.strip():
        if new.strip() not in st.session_state.funds:
            st.session_state.funds.append(new.strip())
            st.rerun()
    for i, firm in enumerate(sorted(st.session_state.funds)):
        col = st.columns([6, 1])
        col[0].write(firm)
        if col[1].button("remove", key=f"rm{i}"):
            st.session_state.funds.remove(firm)
            st.rerun()

# ---------------- MANUAL LOG ----------------
with tab_log:
    st.subheader("Log a signal you spotted yourself")
    type_labels = {k: v["label"] for k, v in c.TRIGGER_TYPES.items()}
    with st.form("manual"):
        firm = st.text_input("Fund")
        ttype = st.selectbox("Trigger type", list(type_labels), format_func=lambda k: type_labels[k])
        dt = st.text_input("Date it happened (YYYY-MM-DD)", date.today().isoformat())
        src = st.text_input("Source (where you saw it)")
        note = st.text_input("Note (optional)")
        if st.form_submit_button("Add to log") and firm.strip():
            st.session_state.triggers.append(
                {"firm": firm.strip(), "type": ttype, "date": dt, "source": src, "note": note}
            )
            if firm.strip() not in st.session_state.funds:
                st.session_state.funds.append(firm.strip())
            st.success(f"Logged: {firm} — {type_labels[ttype]}")

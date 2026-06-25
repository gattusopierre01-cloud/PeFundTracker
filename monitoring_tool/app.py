import json
from datetime import date
import streamlit as st
import tracker_core as c
import sheets

st.set_page_config(page_title="MosaiQ Fund Tracker", page_icon="📈", layout="wide")
USE_SHEETS = sheets.available()


def _load_seed():
    try:
        with open("seed_data.json", encoding="utf-8") as f:
            d = json.load(f)
        return d.get("funds", []), d.get("triggers", [])
    except Exception:
        return [], []


def _apply(d):
    st.session_state.funds = d.get("funds", [])
    st.session_state.triggers = d.get("triggers", [])
    st.session_state.statuses = d.get("statuses", {})
    st.session_state.dismissed = d.get("dismissed", [])


def _blob():
    return json.dumps({
        "funds": st.session_state.funds,
        "triggers": st.session_state.triggers,
        "statuses": st.session_state.statuses,
        "dismissed": st.session_state.dismissed,
    }, indent=2, ensure_ascii=False)


def persist():
    """Save to the Google Sheet (no-op if not connected)."""
    if not (USE_SHEETS and st.session_state.get("sheet_ok")):
        return
    try:
        sheets.save_blob(_blob())
    except Exception as e:
        st.warning(f"Couldn't save to Google Sheet: {e}")


# ---- one-time load ----
if "loaded" not in st.session_state:
    st.session_state.candidates = []
    st.session_state.sheet_ok = False
    st.session_state.sheet_err = None
    if USE_SHEETS:
        try:
            raw = sheets.load_blob()
            if raw:
                _apply(json.loads(raw))
            else:
                f, t = _load_seed()
                _apply({"funds": f, "triggers": t})
            st.session_state.sheet_ok = True
        except Exception as e:
            st.session_state.sheet_err = str(e)
            f, t = _load_seed()
            _apply({"funds": f, "triggers": t})
    else:
        f, t = _load_seed()
        _apply({"funds": f, "triggers": t})
    st.session_state.loaded = True

# ---- sidebar ----
with st.sidebar:
    st.header("Your data")
    if USE_SHEETS and st.session_state.sheet_ok:
        st.success("✅ Saving to your Google Sheet automatically.")
        if st.button("🔄 Reload from Sheet"):
            try:
                raw = sheets.load_blob()
                if raw:
                    _apply(json.loads(raw))
                st.success("Reloaded.")
                st.rerun()
            except Exception as e:
                st.error(f"Reload failed: {e}")
        if st.button("💾 Save now"):
            persist()
            st.success("Saved.")
    elif USE_SHEETS and st.session_state.sheet_err:
        st.error(
            "⚠️ Google Sheet not connected — using temporary memory (changes won't "
            f"survive a restart). Reason: {st.session_state.sheet_err}"
        )
        st.caption("Usual causes: the Sheets/Drive API isn't enabled, the sheet isn't "
                   "shared with the robot email as Editor, or the secret is mis-pasted.")
    else:
        st.caption("Google Sheet not set up yet — using temporary memory. Add the secret "
                   "to switch on auto-save.")

    st.divider()
    st.caption("One-time import / restore from a file:")
    up = st.file_uploader("Import a JSON data file", type="json")
    if up and st.button("Load file & save to Sheet"):
        _apply(json.load(up))
        persist()
        st.success("Imported.")
        st.rerun()
    st.download_button("⬇️ Download a backup copy", _blob(),
                       file_name="fund_tracker_backup.json", mime="application/json")
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
        regions_present = sorted({c.region_for(r["firm"]) for r in rows})
        fc = st.columns([2, 3, 3])
        tiers_sel = fc[0].multiselect("Tier", ["T1", "T2", "T3"], default=["T1", "T2", "T3"])
        regions_sel = fc[1].multiselect("Region", regions_present, default=regions_present)
        query = fc[2].text_input("Search fund", "").strip().lower()
        rows = [r for r in rows
                if r["tier"] in tiers_sel
                and c.region_for(r["firm"]) in regions_sel
                and (query in r["firm"].lower() if query else True)]
        if not rows:
            st.warning("No funds match these filters.")

        colour = {"T1": "🔴", "T2": "🟠", "T3": "🟢"}
        for tier in ["T1", "T2", "T3"]:
            tier_rows = [r for r in rows if r["tier"] == tier]
            if not tier_rows:
                continue
            st.markdown(f"### {colour[tier]} {tier} — {c.TIER_ACTION[tier]}")
            for r in tier_rows:
                firm = r["firm"]
                with st.container(border=True):
                    head = st.columns([5, 2])
                    extra = f"  ·  +{r['n_active']-1} more" if r["n_active"] > 1 else ""
                    head[0].markdown(
                        f"**{firm}**  ·  _{c.region_for(firm)}_  ·  score {r['score']}  \n"
                        f"_{r['lead']}_, {r['age_days']}d old{extra}  ·  → {r['receiver']}"
                    )
                    prev = st.session_state.statuses.get(firm, "Not contacted")
                    idx = c.PIPELINE_STAGES.index(prev) if prev in c.PIPELINE_STAGES else 0
                    new_status = head[1].selectbox("Status", c.PIPELINE_STAGES, index=idx, key=f"st_{firm}")
                    if new_status != prev:
                        st.session_state.statuses[firm] = new_status
                        persist()

                    with st.expander("✉️ Draft outreach + find the contact"):
                        st.text_area(
                            "Email draft (edit, then copy)",
                            c.draft_outreach(firm, r["lead_type"], r["headline"]),
                            height=280, key=f"draft_{firm}",
                        )
                        st.markdown(
                            f"[🔗 Find {firm} people on LinkedIn]({c.linkedin_people_url(firm)})  "
                            f"&nbsp;·&nbsp;  [📅 Your Calendly]({c.CALENDLY})"
                        )
                        st.caption("Swap “[first name]” for the contact and “[Loom: 90-sec demo]” for your Loom link.")

# ---------------- SCAN ----------------
with tab_scan:
    st.subheader("On-demand scan")
    st.caption(
        "Scans each fund against free Google News **and PE trade-press feeds** (which catch "
        "operating-partner hires Google News usually misses), then auto-screens: collapses the "
        "same deal from several outlets into one, drops mere mentions, hides anything you've "
        "dismissed, and surfaces the strong signals first. (Live check only works on the deployed app.)"
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
            prog.progress(1.0, "Checking trade press…")
            try:
                found.extend(c.scan_trade_press(st.session_state.funds))
            except Exception:
                pass
            prog.empty()
            logged = {(t["firm"], t["date"], t["type"]) for t in st.session_state.triggers}
            dismissed = set(st.session_state.dismissed)
            raw = [x for x in found
                   if (x["firm"], x["date"], x["suggested_type"]) not in logged
                   and c.dismiss_key(x["firm"], x["title"]) not in dismissed]
            st.session_state.candidates = c.dedupe_candidates(raw)
            n_strong = sum(1 for x in st.session_state.candidates if x["is_strong"])
            st.success(
                f"Screened {len(found)} raw hits → {len(st.session_state.candidates)} distinct signals "
                f"({n_strong} strong)."
            )

    if st.session_state.candidates:
        cands = st.session_state.candidates
        n_strong = sum(1 for x in cands if x["is_strong"])
        n_weak = len(cands) - n_strong
        st.markdown("#### Review — strong signals are pre-ticked")
        show_weak = st.checkbox(f"Also show {n_weak} weaker signal(s)", value=False)
        shown = [x for x in cands if x["is_strong"] or show_weak]

        if st.button(f"✅ Accept all {len(shown)} shown", type="primary"):
            st.session_state.triggers.extend({
                "firm": x["firm"], "type": x["suggested_type"], "date": x["date"],
                "source": x["source"], "note": x["title"],
            } for x in shown)
            st.session_state.candidates = [x for x in cands if x not in shown]
            persist()
            st.success(f"Logged {len(shown)} signal(s). Check the Queue tab.")
            st.rerun()

        st.caption("…or fine-tune below: tick to log, ✕ to dismiss (won't resurface on future scans).")
        type_labels = {k: v["label"] for k, v in c.TRIGGER_TYPES.items()}
        keep = []
        for i, cand in enumerate(shown):
            with st.container(border=True):
                badge = "🟢 strong" if cand["is_strong"] else "⚪️ weak"
                extra = f" · {cand['n_headlines']} headlines merged" if cand["n_headlines"] > 1 else ""
                top = st.columns([6, 1])
                top[0].markdown(f"**{cand['firm']}** &nbsp;·&nbsp; {badge}{extra}")
                if top[1].button("✕ dismiss", key=f"dis{i}"):
                    st.session_state.dismissed.append(c.dismiss_key(cand["firm"], cand["title"]))
                    st.session_state.candidates = [x for x in cands if x is not cand]
                    persist()
                    st.rerun()
                st.markdown(cand["title"])
                cc = st.columns([1, 2, 2, 3])
                take = cc[0].checkbox("Log it", value=cand["is_strong"], key=f"k{i}")
                ttype = cc[1].selectbox(
                    "Type", list(type_labels), index=list(type_labels).index(cand["suggested_type"]),
                    format_func=lambda k: type_labels[k], key=f"t{i}",
                )
                dt = cc[2].text_input("Date", cand["date"], key=f"d{i}")
                cc[3].caption(cand["source"])
                if take:
                    keep.append({"firm": cand["firm"], "type": ttype, "date": dt,
                                 "source": cand["source"], "note": cand["title"]})
        if st.button("➕ Add ticked to my log"):
            st.session_state.triggers.extend(keep)
            st.session_state.candidates = [x for x in cands if x not in shown]
            persist()
            st.success(f"Added {len(keep)} signal(s). Check the Queue tab.")
            st.rerun()

# ---------------- WATCHLIST ----------------
with tab_watch:
    st.subheader("Funds you track")
    new = st.text_input("Add a fund")
    if st.button("Add fund") and new.strip():
        if new.strip() not in st.session_state.funds:
            st.session_state.funds.append(new.strip())
            persist()
            st.rerun()
    st.caption(f"{len(st.session_state.funds)} funds tracked.")
    for i, firm in enumerate(sorted(st.session_state.funds)):
        col = st.columns([5, 2, 1])
        col[0].write(firm)
        col[1].caption(c.region_for(firm))
        if col[2].button("remove", key=f"rm{i}"):
            st.session_state.funds.remove(firm)
            persist()
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
            persist()
            st.success(f"Logged: {firm} — {type_labels[ttype]}")

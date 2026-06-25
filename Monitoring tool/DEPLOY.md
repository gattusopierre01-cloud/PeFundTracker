# Deploy your Fund Tracker — click by click (no Terminal)

You have four files: `app.py`, `tracker_core.py`, `requirements.txt`, `seed_data.json`.
Goal: get them onto a free GitHub account, then point free Streamlit at them, so your tracker lives at a URL like `https://your-name-fund-tracker.streamlit.app`.

Total time: ~15 minutes, one time. Nothing installs on your Mac.

---

## Part 1 — Put the files on GitHub (the storage)

1. Go to **github.com** and create a free account (skip if you have one).
2. Click the **"+"** top-right → **New repository**.
3. Name it `fund-tracker`. Set it to **Public** (required for free Streamlit). Tick **"Add a README file"**. Click **Create repository**.
4. On the repo page, click **Add file → Upload files**.
5. Drag in all four files (`app.py`, `tracker_core.py`, `requirements.txt`, `seed_data.json`). Wait for them to finish, then click **Commit changes**.

That's your code stored. You'll come back here only when you want to change something.

---

## Part 2 — Deploy on Streamlit (the live page)

6. Go to **share.streamlit.io**.
7. Click **Continue with GitHub** and approve the connection (this is the GitHub link-up — a few clicks, no coding).
8. Click **Create app** (top-right) → choose **"Deploy a public app from GitHub"**.
9. Fill in:
   - **Repository:** `your-username/fund-tracker`
   - **Branch:** `main`
   - **Main file path:** `app.py`
   - (Optional) **App URL:** pick a name like `mosaiq-fund-tracker`.
10. Click **Deploy**. Wait a few minutes while it installs and launches.

When it finishes, you have a live page at your URL. **Bookmark it.** That's your tracker.

---

## Using it

- **Queue tab** — your ranked "who to act on" list (T1 act today / T2 send a sequence / T3 watch).
- **Run a scan tab** — click the button; it checks your watchlist against free news and shows candidate signals. Tick the real ones, set the type/date, **Add selected**. They flow into the Queue.
- **Watchlist tab** — add or remove the funds you track.
- **Log a signal tab** — type in something you spotted yourself.

## Two things to know
- **Saving your work:** free hosting wipes data when the app restarts. Use **⬇️ Download my data** in the sidebar to save a file, and **⬆️ Load saved data** next time. (Seed data reloads on a fresh start regardless.)
- **The scan:** it pulls free Google News results — good for catching acquisitions, hires, and fund closes at named funds, but it's a *candidate finder you confirm*, not a perfect or exhaustive feed. It won't surface funds you don't already track.

## Changing the app later
Edit a file in the GitHub repo (pencil icon → edit → Commit), and Streamlit updates the live page automatically within a minute. Ask me and I'll give you the exact change to paste.

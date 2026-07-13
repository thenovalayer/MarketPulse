# Market Pulse — GitHub Pages + Actions

Publishes two pages, auto-rebuilt daily by GitHub Actions (not by Claude/Cowork):

- `index.html` — Daily Market Pulse (India + USA, 10 news-driven picks each)
- `tracker/index.html` — 14-day forward tracker for the fixed 20-stock cohort from 1 Jul 2026

Once GitHub Pages is on, your permanent URLs are:

- `https://<your-username>.github.io/<repo-name>/`
- `https://<your-username>.github.io/<repo-name>/tracker/`

## What actually updates it

A GitHub Actions workflow (`.github/workflows/daily-pulse.yml`) runs on a cron schedule, on GitHub's own servers — independent of this chat and independent of Cowork's scheduler (which we saw miss runs). It calls `scripts/generate_pulse.py`, which:

1. Calls the **Gemini API** (`gemini-2.5-flash`, with built-in Google Search grounding) to research today's India + US market news and return the same style of picks you've seen in chat.
2. Re-prices the fixed 20-stock cohort in `data/cohort.json` (never changes the list, just updates current price/day-count) for the tracker.
3. Renders both HTML files from the templates in `templates/`.
4. The workflow commits and pushes the changed files. GitHub Pages picks up the new commit automatically.

## One-time setup (you do this in GitHub's UI — not something I can do for you)

1. Create a new **public** GitHub repo and push everything in this folder to it.
2. Get a free Gemini API key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey) — no credit card required. Note: this is separate from a Google AI Pro subscription; the free API tier works with or without one, and easily covers this script's ~2 calls/day.
3. **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `GEMINI_API_KEY`
   - Value: the key from step 2.
4. **Settings → Pages** → Source: "Deploy from a branch" → Branch: `main`, folder: `/ (root)`. Save.
5. **Actions** tab → find "Daily Market Pulse" → click **Run workflow** once manually to confirm it works before trusting the schedule.

## Before you trust it

Run it locally first:

```
pip install -r requirements.txt
export GEMINI_API_KEY=AIza...
python scripts/generate_pulse.py
```

Open the generated `index.html` and `tracker/index.html` in a browser and sanity-check them — the Google Search grounding call pattern in `generate_pulse.py` is the part most likely to need a small adjustment if the `google-genai` SDK has changed since this was written. I can't run this end-to-end myself in this chat (no live Gemini API key here), so treat the first few runs as "verify, don't just trust."

## Schedule

Default cron in the workflow is `32 2 * * 1-5` (UTC) = 8:02 AM IST, weekdays — matching the original Cowork schedule. Edit the `cron:` line in `daily-pulse.yml` to change it (GitHub Actions cron is always UTC, unlike Cowork's scheduler which uses local time).

## Cost/ops notes

- This runs entirely on Gemini's free tier (no billing account needed) at this volume — roughly 2 calls/day, well under the free-tier daily request cap. If you ever scale this up a lot, check current Gemini API rate limits before relying on it long-term.
- If a run fails (bad API response, malformed JSON, etc.), the workflow should fail loudly rather than publish garbage — check the Actions tab's run log if the page looks stale.
- The 14-day tracker experiment auto-marks itself "complete" once `windowDays` have elapsed since `entryDate` in `data/cohort.json` — it keeps rendering (harmlessly) after that, it just stops being a live experiment.

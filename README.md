# 🏀 March Madness Bracket Tracker

A self-contained system that reads your Google Forms bracket entries and generates a beautiful, shareable live leaderboard website — automatically updated from ESPN every 2 hours during the tournament.

---

## How it works

1. Your **Google Form** collects picks → saved to a **Google Sheet**
2. You download the sheet as `.xlsx` and commit it to this repo **once** (or whenever entries close)
3. **GitHub Actions** runs every 2 hours, fetches live scores from ESPN, regenerates the leaderboard, and deploys it automatically
4. Participants visit your **GitHub Pages URL** to see live standings

---

## One-time setup (~10 minutes)

### Step 1 — Create a GitHub repository

1. Go to [github.com](https://github.com) and sign in (or create a free account)
2. Click **New repository** → name it something like `march-madness-2026`
3. Set it to **Public** (required for free GitHub Pages)
4. Click **Create repository**

### Step 2 — Upload these files

Upload the contents of this folder to your new repository. The easiest way:
- On the repo page, click **Add file → Upload files**
- Drag in all the files (including the `.github/workflows/` folder)

> **Important:** Make sure the `.github/workflows/update-standings.yml` file is included — this is what powers the auto-updates.

### Step 3 — Add your entries file

1. Download your Google Sheet as Excel:
   - Open your Google Sheet → **File → Download → Microsoft Excel (.xlsx)**
2. Rename the file to exactly `bracket_entries.xlsx`
3. Upload it to your GitHub repository (same as Step 2)

> Your Google Sheet must have a sheet tab named **"Raw Data"** with the Google Forms output.
> The column headers must include `Email Address` and columns like `#1 Seed (100 pts per win)` through `#16 Seed (250 pts per win)`.
> This matches the default Google Forms → Google Sheets export format exactly.

### Step 4 — Enable GitHub Pages

1. In your repo, go to **Settings → Pages** (left sidebar)
2. Under **Source**, select **Deploy from a branch**
3. Choose branch: `main`, folder: `/ (root)` → **Save**
4. After ~1 minute, your site will be live at:
   ```
   https://YOUR-USERNAME.github.io/march-madness-2026/
   ```
   Share this URL with participants!

### Step 5 — Enable Actions permissions

1. Go to **Settings → Actions → General**
2. Under **Workflow permissions**, select **Read and write permissions**
3. Click **Save**

This allows the GitHub Action to commit the updated `index.html` back to the repo.

---

## Updating entries mid-tournament

If participants submit late, just re-download your Google Sheet and re-upload `bracket_entries.xlsx` to the repo. The next automated run will pick up the changes.

---

## Scoring

| Seed | Points per win |
|------|---------------|
| #1   | 100           |
| #2   | 110           |
| #3   | 120           |
| #4   | 130           |
| #5   | 140           |
| #6   | 150           |
| #7   | 160           |
| #8   | 170           |
| #9   | 180           |
| #10  | 190           |
| #11  | 200           |
| #12  | 210           |
| #13  | 220           |
| #14  | 230           |
| #15  | 240           |
| #16  | 250           |

Participants pick **one team per seed slot**. Points = wins × points-per-win.
Play-in game slots (e.g. `Team A / Team B`) are handled automatically.

---

## Customization

Open `process_bracket.py` and edit the **CONFIGURATION** block at the top:

```python
CHALLENGE_NAME   = "March Madness 2026 Bracket Challenge"   # Page title
EXCEL_FILE       = "bracket_entries.xlsx"                   # Your entries file
TOURNAMENT_START = date(2026, 3, 19)                        # First Four date
TOURNAMENT_END   = date(2026, 4, 7)                         # Championship date
```

### If the ESPN API doesn't recognize a team name

Add it to the `TEAM_ALIASES` dict:
```python
TEAM_ALIASES = {
    "uconn": "connecticut",
    "ole miss": "mississippi",
    # Add more as needed
}
```

### Manual wins override (if ESPN is down)

Fill in `MANUAL_WINS_OVERRIDE` and run with `--no-fetch`:
```python
MANUAL_WINS_OVERRIDE = {
    "Florida": 6,
    "Houston": 5,
    "Duke": 4,
    ...
}
```
```bash
python process_bracket.py bracket_entries.xlsx --no-fetch
```

---

## Running locally

```bash
# Install dependencies
pip install -r requirements.txt

# Generate the site
python process_bracket.py bracket_entries.xlsx

# Open in browser
open index.html        # macOS
start index.html       # Windows
xdg-open index.html    # Linux
```

---

## Website features

- **Live leaderboard** — all participants ranked by score, searchable
- **Expandable rows** — click any participant to see their picks color-coded (green = alive, red = eliminated)
- **Points distribution chart** — top 20 participants visualized
- **Pick popularity charts** — for every seed, see how picks were distributed
- **Summary stats** — games played, leader, teams eliminated, total entries
- **Auto-refresh** — GitHub Actions re-runs every 2 hours and pushes updated standings

---

## Frequently asked questions

**Q: When does the site update?**
A: Every 2 hours automatically via GitHub Actions. You can also trigger a manual update from the **Actions** tab in your repo.

**Q: What if a participant submits multiple times?**
A: The script keeps only the most recent submission per email address.

**Q: What about play-in games (First Four)?**
A: First Four (play-in) wins do **not** count toward scores — only wins from the Round of 64 onward count. If a participant's pick was a play-in slot like `"Team A / Team B"`, they earn points for whichever team advanced and won games in the main draw. The First Four win itself is automatically excluded from the ESPN data.

**Q: What if the ESPN API returns 0 wins before the tournament starts?**
A: That's expected — everyone will show 0 pts until games are played. The leaderboard will be alphabetical until scores differentiate.

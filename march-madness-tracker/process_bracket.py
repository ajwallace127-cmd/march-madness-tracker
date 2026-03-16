#!/usr/bin/env python3
"""
March Madness Bracket Tracker
==============================
Reads Google Forms bracket entries (exported as .xlsx) and generates
a self-contained, shareable HTML leaderboard website.

Usage:
    python process_bracket.py                          # uses bracket_entries.xlsx
    python process_bracket.py my_entries.xlsx          # custom filename
    python process_bracket.py entries.xlsx --no-fetch  # skip ESPN, use manual wins
"""

import json
import sys
import os
import re
import requests
import pandas as pd
from datetime import date, timedelta, datetime
from collections import defaultdict

# ============================================================
# CONFIGURATION — Edit these each year
# ============================================================

CHALLENGE_NAME   = "March Madness 2026 Bracket Challenge"
EXCEL_FILE       = "bracket_entries.xlsx"
OUTPUT_FILE      = "index.html"
TOURNAMENT_START = date(2026, 3, 19)   # First Four start date
TOURNAMENT_END   = date(2026, 4, 7)    # Championship game

# Scoring formula: seed * 10 + 90  (seed1=100, seed2=110, ..., seed16=250)
def pts_per_win(seed: int) -> int:
    return seed * 10 + 90

# Manual wins override — fill this in if ESPN fetch fails or for testing.
# Format: {"Exact Team Name": number_of_wins}
# Example: {"Florida": 6, "Auburn": 4, "Tennessee": 3}
MANUAL_WINS_OVERRIDE: dict = {}

# Known team name aliases (pick name → ESPN display name fragment)
TEAM_ALIASES = {
    "uconn":        "connecticut",
    "ucf":          "central florida",
    "ucsd":         "uc san diego",
    "smu":          "southern methodist",
    "vcu":          "virginia commonwealth",
    "lsu":          "louisiana state",
    "ole miss":     "mississippi",
    "byu":          "brigham young",
    "unc":          "north carolina",
    "pitt":         "pittsburgh",
    "unlv":         "nevada las vegas",
    "saint mary's": "saint mary's",
    "st john's":    "st. john's",
}

# ============================================================
# ESPN API — FETCH TOURNAMENT RESULTS
# ============================================================

def _is_first_four_event(event: dict) -> bool:
    """
    Return True if this ESPN event is a First Four (play-in) game.
    First Four wins do NOT count toward participants' scores.
    Checks the competition headline notes and falls back to date range
    (First Four is always the first 2 days of TOURNAMENT_START).
    """
    comps = event.get("competitions", [])
    if comps:
        for note in comps[0].get("notes", []):
            headline = note.get("headline", "").lower()
            if "first four" in headline or "first 4" in headline:
                return True
    # Date-range fallback: First Four is day 1–2 of the tournament
    game_date_str = event.get("date", "")[:10]  # "YYYY-MM-DD"
    try:
        game_date = date.fromisoformat(game_date_str)
        if game_date <= TOURNAMENT_START + timedelta(days=1):
            return True
    except ValueError:
        pass
    return False


def fetch_tournament_results() -> tuple[dict, set]:
    """
    Fetch completed tournament game results from the ESPN API.
    Returns (wins_dict, eliminated_set) where:
      wins_dict      = {team_display_name: win_count}
      eliminated_set = set of team names that have lost

    First Four (play-in) games are intentionally excluded — those wins
    do not count in this scoring system.
    """
    wins        = defaultdict(int)
    losses      = defaultdict(int)
    today       = date.today()
    current     = TOURNAMENT_START
    total_games = 0
    skipped_ff  = 0

    while current <= min(today, TOURNAMENT_END):
        url = ("https://site.api.espn.com/apis/site/v2/sports/"
               "basketball/mens-college-basketball/scoreboard")
        params = {
            "dates":  current.strftime("%Y%m%d"),
            "groups": "100",   # 100 = NCAA Tournament group
            "limit":  "30",
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            for event in data.get("events", []):
                completed = (event.get("status", {})
                                  .get("type", {})
                                  .get("completed", False))
                if not completed:
                    continue
                # Skip First Four / play-in games
                if _is_first_four_event(event):
                    skipped_ff += 1
                    continue
                comps = event.get("competitions", [{}])
                for comp in comps[0].get("competitors", []):
                    name = comp.get("team", {}).get("displayName", "")
                    if not name:
                        continue
                    if comp.get("winner", False):
                        wins[name] += 1
                    else:
                        losses[name] += 1
                    total_games += 1
        except requests.exceptions.ConnectionError:
            print(f"  ⚠  Network error fetching {current} — skipping")
        except requests.exceptions.HTTPError as e:
            print(f"  ⚠  HTTP {e.response.status_code} for {current} — skipping")
        except Exception as e:
            print(f"  ⚠  Unexpected error for {current}: {e}")

        current += timedelta(days=1)

    # Apply manual overrides
    for team, w in MANUAL_WINS_OVERRIDE.items():
        wins[team] = w

    eliminated = {t for t, l in losses.items() if l > 0}
    print(f"  ESPN: {total_games // 2} games processed | "
          f"{len(wins)} teams with wins | {len(eliminated)} eliminated"
          + (f" | {skipped_ff} First Four games skipped" if skipped_ff else ""))
    return dict(wins), eliminated


# ============================================================
# TEAM NAME MATCHING
# ============================================================

def _normalize(name: str) -> str:
    """Lowercase, strip, remove common mascot suffixes."""
    name = name.lower().strip()
    for alias, canonical in TEAM_ALIASES.items():
        if name == alias:
            name = canonical
            break
    name = re.sub(r"\(\d+\)", "", name).strip()
    return name


def _word_subset_match(pick_norm: str, team_norm: str) -> bool:
    """
    True only if every word in the pick appears as a complete word in the
    team name.  Prevents 'kansas' matching 'arkansas', 'texas' matching
    'texas tech', and 'mississippi state' matching 'ole miss' (alias →
    'mississippi').
    """
    pick_words = set(pick_norm.split())
    team_words = set(team_norm.split())
    return bool(pick_words) and pick_words.issubset(team_words)


def match_team_wins(pick: str, wins: dict, eliminated: set) -> tuple[int, bool, bool]:
    """
    Match a pick string to an ESPN team name.
    Returns (wins, is_eliminated, has_played).
    Handles play-in entries like "Team A / Team B".

    Matching order:
      1. Exact normalized match (fastest, most precise)
      2. Word-subset fallback: all pick words must appear as whole words in
         the team name; when multiple teams qualify, pick the one with the
         highest Jaccard word-overlap (most specific match).
      3. Same word-subset check against eliminated teams (no wins yet).
    """
    if not pick or (isinstance(pick, float) and pd.isna(pick)):
        return 0, False, False

    pick_str = str(pick).strip()

    if "/" in pick_str:
        # Play-in (First Four) pick: "Team A / Team B"
        # Only award wins if one of the two named teams appears *exactly*
        # (or via alias) in the results. Do NOT fall back to loose word-subset
        # matching, which would incorrectly match e.g. "Texas" → "Texas Tech".
        parts = [p.strip() for p in pick_str.split("/")]
        for part in parts:
            part_norm = _normalize(part)
            for team, w in wins.items():
                if _normalize(team) == part_norm:
                    return w, team in eliminated, True
            for team in eliminated:
                if _normalize(team) == part_norm:
                    return 0, True, True
        return 0, False, False

    pick_norm = _normalize(pick_str)

    # 1 — Exact normalized match
    for team, w in wins.items():
        if _normalize(team) == pick_norm:
            return w, team in eliminated, True

    # 2 — Word-subset fallback with best-match selection
    #     Every word in the pick must appear as a whole word in the team name.
    #     When multiple teams qualify, choose the highest Jaccard overlap
    #     (most specific match), e.g. "Texas" prefers "Texas Longhorns" (0.50)
    #     over "Texas Tech Red Raiders" (0.25).
    pw = set(pick_norm.split())
    candidates = []
    for team, w in wins.items():
        tw = set(_normalize(team).split())
        if pw and pw.issubset(tw):
            jaccard = len(pw & tw) / len(pw | tw)
            candidates.append((jaccard, team, w))
    if candidates:
        candidates.sort(key=lambda x: -x[0])
        _, best_team, best_w = candidates[0]
        return best_w, best_team in eliminated, True

    # 3 — Check eliminated set (team played but has no more wins)
    for team in eliminated:
        tw = set(_normalize(team).split())
        if pw and pw.issubset(tw):
            return 0, True, True

    return 0, False, False


# ============================================================
# READ ENTRIES FROM EXCEL (Google Forms export)
# ============================================================

def read_entries(excel_file: str) -> list[dict]:
    """
    Read participant bracket entries from the Google Forms Excel export.
    Expects a 'Raw Data' sheet (or falls back to first sheet).
    Keeps only the most recent submission per email address.
    """
    try:
        df = pd.read_excel(excel_file, sheet_name="Raw Data", header=0)
    except Exception:
        df = pd.read_excel(excel_file, header=0)

    if "Timestamp" in df.columns:
        df = df.sort_values("Timestamp")
    df = df.drop_duplicates(subset="Email Address", keep="last")
    df = df[df["Email Address"].notna()]

    seed_cols = {}
    for col in df.columns:
        m = re.match(r"#(\d+)\s+Seed", str(col))
        if m:
            seed_cols[int(m.group(1))] = col

    entries = []
    for _, row in df.iterrows():
        email = str(row["Email Address"]).strip()
        if not email or email.lower() == "nan":
            continue
        picks = {}
        for seed, col in seed_cols.items():
            val = row.get(col, "")
            if pd.notna(val) and str(val).strip():
                picks[seed] = str(val).strip()
        entries.append({"email": email, "picks": picks})

    return entries


# ============================================================
# CALCULATE SCORES
# ============================================================

def calculate_scores(entries: list[dict], wins: dict, eliminated: set) -> list[dict]:
    """Score each participant based on their picks and live results."""
    standings = []

    for entry in entries:
        total_pts    = 0
        teams_alive  = 0
        picks_detail = []

        for seed in range(1, 17):
            team = entry["picks"].get(seed, "")
            w, is_elim, has_played = match_team_wins(team, wins, eliminated)
            pts = w * pts_per_win(seed)
            total_pts += pts

            alive = has_played and not is_elim
            if alive:
                teams_alive += 1

            picks_detail.append({
                "seed":        seed,
                "team":        team,
                "wins":        w,
                "points":      pts,
                "pts_per_win": pts_per_win(seed),
                "eliminated":  is_elim,
                "alive":       alive,
                "has_played":  has_played,
            })

        username = entry["email"].split("@")[0]
        words    = re.split(r"[._\-]+", username)
        words    = [re.sub(r"\d+$", "", w).capitalize() for w in words if re.sub(r"\d+$", "", w)]
        display  = " ".join(words) if words else username.title()

        standings.append({
            "email":        entry["email"],
            "name":         display,
            "total_points": total_pts,
            "teams_alive":  teams_alive,
            "picks":        picks_detail,
        })

    standings.sort(key=lambda x: (-x["total_points"], x["name"]))
    for i, s in enumerate(standings):
        s["rank"] = i + 1

    return standings


# ============================================================
# PICK POPULARITY
# ============================================================

def get_pick_popularity(entries: list[dict]) -> dict:
    """Return {seed: {team: count}} for all picks."""
    popularity = {}
    for seed in range(1, 17):
        counts = defaultdict(int)
        for entry in entries:
            team = entry["picks"].get(seed, "")
            if team:
                counts[team] += 1
        popularity[seed] = dict(sorted(counts.items(), key=lambda x: -x[1]))
    return popularity


# ============================================================
# HTML GENERATION
# ============================================================

def _pick_chips_html(picks: list[dict]) -> str:
    """Render pick chips for the expanded leaderboard row."""
    html = ""
    for p in picks:
        if not p["team"]:
            continue
        if p["eliminated"] or (p["has_played"] and not p["alive"]):
            css, icon = "pick-eliminated", "✗"
        elif p["alive"]:
            css, icon = "pick-alive", "✓"
        else:
            css, icon = "pick-notplayed", "·"
        pts = f"+{p['points']}" if p["points"] else "0"
        html += (f'<span class="pick-chip {css}">'
                 f'{icon} #{p["seed"]} {p["team"]} <em>({pts})</em></span>\n')
    return html


def build_teams_data(popularity: dict, wins: dict, eliminated: set,
                     entries: list[dict]) -> list[dict]:
    """Build a flat list of team records for the Still Ballin' tab."""
    total = len(entries)
    teams = []
    seen  = set()
    for seed in range(1, 17):
        for team, count in popularity.get(seed, {}).items():
            if team in seen:
                continue
            seen.add(team)
            w, is_elim, has_played = match_team_wins(team, wins, eliminated)
            if is_elim:
                status = "out"
            elif has_played and w > 0:
                status = "alive"
            else:
                status = "notplayed"
            teams.append({
                "seed":   seed,
                "team":   team,
                "wins":   w,
                "ppts":   pts_per_win(seed),
                "earned": w * pts_per_win(seed),
                "status": status,
                "picks":  count,
                "pct":    round(count / total * 100, 1) if total else 0.0,
            })
    order = {"alive": 0, "notplayed": 1, "out": 2}
    teams.sort(key=lambda t: (order[t["status"]], t["seed"], -t["picks"]))
    return teams


def generate_html(standings: list[dict], wins: dict, eliminated: set,
                  entries: list[dict], popularity: dict) -> str:

    last_updated = datetime.now().strftime("%B %d, %Y at %I:%M %p")
    total_p      = len(standings)
    games_played = sum(wins.values()) // 2
    leader       = standings[0] if standings else {"name": "TBD", "total_points": 0}

    teams_data  = build_teams_data(popularity, wins, eliminated, entries)
    alive_count = sum(1 for t in teams_data if t["status"] == "alive")
    elim_count  = len(eliminated)

    js_popularity = json.dumps({str(k): v for k, v in popularity.items()},
                                ensure_ascii=False)

    # ── Leaderboard rows ──────────────────────────────────────────────────
    rows_html = ""
    for s in standings:
        rank = s["rank"]
        if   rank == 1: badge_cls, row_cls = "rank-1", "row-gold"
        elif rank == 2: badge_cls, row_cls = "rank-2", "row-silver"
        elif rank == 3: badge_cls, row_cls = "rank-3", "row-bronze"
        else:           badge_cls, row_cls = "rank-n", ""

        fire_tag = ' <span class="fire-tag">🔥</span>' if rank == 1 else ""

        alive_picks = [p for p in s["picks"] if p["alive"]]
        alive_str   = ", ".join(f"#{p['seed']} {p['team']}" for p in alive_picks[:3])
        if len(alive_picks) > 3:
            alive_str += f" +{len(alive_picks) - 3} more"

        chips  = _pick_chips_html(s["picks"])
        row_id = f"pr-{rank}"

        if s["teams_alive"] > 0:
            alive_html = f'<span class="badge-alive">{s["teams_alive"]}</span>'
        else:
            alive_html = '<span class="badge-dead">0</span>'

        rows_html += f"""
        <tr class="lb-row {row_cls}" onclick="toggleRow('{row_id}')">
          <td><span class="rank-badge {badge_cls}">{rank}</span></td>
          <td>
            <span class="p-name">{s['name']}{fire_tag}</span>
            <span class="p-email">{s['email']}</span>
          </td>
          <td class="text-end"><span class="pts-val">{s['total_points']:,}</span></td>
          <td class="text-center">{alive_html}</td>
          <td class="d-none d-lg-table-cell still-in-col">{alive_str or "—"}</td>
        </tr>
        <tr class="picks-row" id="{row_id}">
          <td colspan="5"><div class="picks-inner">{chips}</div></td>
        </tr>"""

    # ── Pick Popularity chart canvases ────────────────────────────────────
    pop_charts = ""
    for seed in range(1, 17):
        pop_charts += f"""
        <div class="col-12 col-sm-6 col-xl-4">
          <div class="chart-card">
            <div class="chart-label">
              <span class="seed-tag">#{seed} Seed</span>
              <span class="ppw-tag">{pts_per_win(seed)} pts/win</span>
            </div>
            <canvas id="pc{seed}" height="130"></canvas>
          </div>
        </div>"""

    # ── Top Teams rows (sorted by earned pts, any seed) ───────────────────
    top_teams     = sorted([t for t in teams_data if t["earned"] > 0],
                            key=lambda t: -t["earned"])
    top_teams_rows = ""
    for i, t in enumerate(top_teams, 1):
        if   t["status"] == "alive":
            status_html = '<span class="ts-alive">● Alive</span>'
        elif t["status"] == "notplayed":
            status_html = '<span class="ts-wait">◌ Waiting</span>'
        else:
            status_html = '<span class="ts-out">✗ Out</span>'
        top_teams_rows += f"""
        <tr>
          <td class="text-center text-muted fw-semibold">{i}</td>
          <td class="fw-semibold">{t['team']}</td>
          <td class="text-center"><span class="seed-pill">#{t['seed']}</span></td>
          <td class="text-center">{status_html}</td>
          <td class="text-center">{t['wins']}</td>
          <td class="text-center text-muted">{t['ppts']}</td>
          <td class="text-end fw-bold" style="color:var(--blue)">{t['earned']:,}</td>
          <td class="text-center text-muted">{t['picks']}</td>
        </tr>"""

    # ── All Teams table rows ──────────────────────────────────────────────
    teams_rows  = ""
    prev_status = None
    for t in teams_data:
        if t["status"] == "out" and prev_status != "out":
            teams_rows += (
                '<tr class="elim-divider">'
                '<td colspan="8">— Eliminated —</td>'
                '</tr>')
        prev_status = t["status"]

        if   t["status"] == "alive":
            row_cls, status_html = "tr-alive", '<span class="ts-alive">● Alive</span>'
        elif t["status"] == "notplayed":
            row_cls, status_html = "tr-wait",  '<span class="ts-wait">◌ Waiting</span>'
        else:
            row_cls, status_html = "tr-out",   '<span class="ts-out">✗ Out</span>'

        earned_str = f"{t['earned']:,}" if t["earned"] else "—"

        teams_rows += f"""
        <tr class="{row_cls}">
          <td class="text-center"><span class="seed-pill">#{t['seed']}</span></td>
          <td class="team-name-cell">{t['team']}</td>
          <td class="text-center">{status_html}</td>
          <td class="text-center fw-semibold">{t['wins']}</td>
          <td class="text-center text-muted">{t['ppts']}</td>
          <td class="text-end fw-semibold">{earned_str}</td>
          <td class="text-center fw-semibold">{t['picks']}</td>
          <td class="text-end text-muted">{t['pct']}%</td>
        </tr>"""

    # ── Full HTML ─────────────────────────────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{CHALLENGE_NAME}</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://fonts.googleapis.com/css2?family=Bangers&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0/dist/chartjs-plugin-datalabels.min.js"></script>
  <style>
    :root {{
      --blue:    #0B3D91;
      --lblue:   #1A56DB;
      --orange:  #E8520A;
      --green:   #16A34A;
      --red:     #DC2626;
      --bg:      #F1F5F9;
      --card:    #FFFFFF;
      --border:  #E2E8F0;
      --text:    #1E293B;
      --muted:   #64748B;
    }}

    *, body {{ font-family: 'Inter', sans-serif; color: var(--text); }}
    body {{ background: var(--bg); }}

    /* ── HERO ─────────────────────────────────────────── */
    .hero {{
      background: linear-gradient(135deg, #0B3D91 0%, #1A56DB 55%, #C44200 100%);
      padding: 44px 0 34px; color: white; text-align: center;
    }}
    .hero-ball  {{ font-size: 2.4rem; line-height: 1; margin-bottom: 8px; }}
    .hero-title {{
      font-family: 'Bangers', cursive;
      font-size: clamp(1.8rem, 5vw, 3.4rem);
      color: white; letter-spacing: 3px;
      text-shadow: 2px 3px 0 rgba(0,0,0,0.25);
      line-height: 1.1; margin-bottom: 4px;
    }}
    .hero-sub   {{ font-size: 0.88rem; opacity: 0.75; margin-top: 6px; letter-spacing: 0.3px; }}
    .hero-updated {{ font-size: 0.72rem; opacity: 0.55; margin-top: 4px; }}

    /* Stat cards */
    .stat-card {{
      background: rgba(255,255,255,0.12);
      border: 1px solid rgba(255,255,255,0.2);
      border-radius: 10px; padding: 16px 12px; text-align: center; color: white;
    }}
    .sc-label {{ font-size: 0.68rem; font-weight: 700; text-transform: uppercase;
                 letter-spacing: 0.8px; opacity: 0.7; margin-bottom: 6px; }}
    .sc-value {{ font-family: 'Bangers', cursive; font-size: 2.4rem; line-height: 1; }}
    .sc-sub   {{ font-size: 0.72rem; opacity: 0.6; margin-top: 3px; }}

    /* ── TABS ─────────────────────────────────────────── */
    .nav-tabs {{
      border-bottom: 2px solid var(--border);
      margin-top: 28px; gap: 4px;
    }}
    .nav-tabs .nav-link {{
      font-weight: 600; font-size: 0.88rem;
      color: var(--muted); border: none; border-bottom: 3px solid transparent;
      padding: 10px 18px; border-radius: 0; background: none;
      transition: all 0.15s;
    }}
    .nav-tabs .nav-link:hover {{ color: var(--orange); }}
    .nav-tabs .nav-link.active {{
      color: var(--orange); border-bottom-color: var(--orange);
      background: none;
    }}
    .tab-content {{ padding-top: 24px; }}

    /* ── SECTION HEADER ──────────────────────────────── */
    .sec-head {{
      font-family: 'Bangers', cursive;
      font-size: 1.5rem; letter-spacing: 2px;
      color: var(--orange);
      border-left: 4px solid var(--blue);
      padding-left: 12px; margin: 0 0 16px;
    }}

    /* ── LEADERBOARD ─────────────────────────────────── */
    .lb-wrap {{
      border-radius: 12px; overflow: hidden;
      box-shadow: 0 4px 16px rgba(0,0,0,0.07); background: var(--card);
    }}
    .lb-wrap thead {{ background: var(--blue); }}
    .lb-wrap thead th {{
      font-size: 0.7rem; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.5px; color: white; padding: 13px 14px;
    }}
    .lb-row {{ cursor: pointer; transition: background 0.1s; }}
    .lb-row:hover {{ background: #EFF6FF !important; }}
    .lb-row td {{ padding: 11px 14px; vertical-align: middle; border-bottom: 1px solid var(--border); }}

    .row-gold   {{ box-shadow: inset 4px 0 0 #F59E0B; background: #FFFBEB; }}
    .row-silver {{ box-shadow: inset 4px 0 0 #94A3B8; }}
    .row-bronze {{ box-shadow: inset 4px 0 0 #B45309; }}

    .rank-badge {{
      display: inline-flex; align-items: center; justify-content: center;
      width: 30px; height: 30px; border-radius: 6px;
      font-weight: 800; font-size: 0.85rem;
    }}
    .rank-1 {{ background: #FEF3C7; color: #92400E; border: 1px solid #F59E0B; }}
    .rank-2 {{ background: #F1F5F9; color: #475569; border: 1px solid #CBD5E1; }}
    .rank-3 {{ background: #FEF9F0; color: #92400E; border: 1px solid #D97706; }}
    .rank-n {{ background: #EFF6FF; color: var(--blue); border: 1px solid #BFDBFE; font-size: 0.78rem; }}

    .p-name  {{ display: block; font-weight: 700; font-size: 0.92rem; color: var(--text); }}
    .p-email {{ display: block; font-size: 0.68rem; color: var(--muted); }}
    .fire-tag {{ font-size: 0.8rem; }}

    .pts-val {{ font-weight: 800; font-size: 1.05rem; color: var(--blue); }}

    .badge-alive {{
      display: inline-block; padding: 3px 9px; border-radius: 20px; font-size: 0.72rem;
      font-weight: 700; background: #DCFCE7; color: #15803D; border: 1px solid #BBF7D0;
    }}
    .badge-dead {{
      display: inline-block; padding: 3px 9px; border-radius: 20px; font-size: 0.72rem;
      font-weight: 600; background: #F8FAFC; color: var(--muted); border: 1px solid var(--border);
    }}
    .still-in-col {{ font-size: 0.78rem; color: var(--muted); }}

    /* Pick chips */
    .picks-row {{ display: none; }}
    .picks-row.open {{ display: table-row; }}
    .picks-row td {{ padding: 10px 14px 14px; background: #F8FAFC !important; border-bottom: 1px solid var(--border); }}
    .picks-inner {{ display: flex; flex-wrap: wrap; gap: 5px; }}
    .pick-chip {{
      display: inline-flex; align-items: center; gap: 4px;
      padding: 4px 10px; border-radius: 20px; font-size: 0.73rem; font-weight: 600;
    }}
    .pick-chip em {{ font-style: normal; opacity: 0.65; font-size: 0.67rem; font-weight: 500; }}
    .pick-alive      {{ background: #DCFCE7; color: #15803D; }}
    .pick-eliminated {{ background: #FEE2E2; color: #B91C1C; }}
    .pick-notplayed  {{ background: #F1F5F9; color: var(--muted); }}

    /* Search */
    #lb-search {{
      border: 1px solid var(--border); border-radius: 8px;
      padding: 8px 14px; font-size: 0.85rem; color: var(--text);
      width: 100%; max-width: 280px; outline: none; background: var(--card);
    }}
    #lb-search:focus {{ border-color: var(--lblue); box-shadow: 0 0 0 3px rgba(26,86,219,0.12); }}

    /* ── CHART CARDS ─────────────────────────────────── */
    .chart-card {{
      background: var(--card); border-radius: 10px; padding: 14px 16px; height: 100%;
      border: 1px solid var(--border); box-shadow: 0 1px 4px rgba(0,0,0,0.05);
    }}
    .chart-label {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 10px; }}
    .seed-tag {{ font-weight: 800; font-size: 0.88rem; color: var(--blue); }}
    .ppw-tag  {{ font-size: 0.72rem; color: var(--muted); font-weight: 600; }}

    /* ── TEAM TRACKER ────────────────────────────────── */
    .tracker-card {{
      background: var(--card); border-radius: 12px; overflow: hidden;
      box-shadow: 0 2px 10px rgba(0,0,0,0.06); border: 1px solid var(--border);
    }}
    .tracker-card thead {{ background: var(--blue); }}
    .tracker-card thead th {{
      font-size: 0.7rem; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.4px; color: white; padding: 12px 10px;
      white-space: nowrap; cursor: pointer; user-select: none;
    }}
    .tracker-card thead th:hover {{ background: var(--lblue); }}
    .tracker-card tbody td {{ padding: 9px 10px; border-bottom: 1px solid var(--border); font-size: 0.85rem; }}
    .tracker-card tbody tr:last-child td {{ border-bottom: none; }}

    .tr-alive td {{ color: var(--text); }}
    .tr-wait  td {{ color: var(--muted); }}
    .tr-out   td {{ color: #CBD5E1; }}
    .tr-out .team-name-cell {{ text-decoration: line-through; text-decoration-color: #CBD5E1; }}

    .ts-alive {{ color: var(--green); font-weight: 700; font-size: 0.8rem; }}
    .ts-wait  {{ color: var(--muted); font-weight: 600; font-size: 0.8rem; }}
    .ts-out   {{ color: #CBD5E1;      font-weight: 600; font-size: 0.8rem; }}

    .team-name-cell {{ font-weight: 600; }}
    .seed-pill {{
      display: inline-block; padding: 2px 7px; border-radius: 10px; font-size: 0.72rem;
      font-weight: 700; background: #EFF6FF; color: var(--blue); border: 1px solid #BFDBFE;
    }}
    .elim-divider td {{
      text-align: center; font-size: 0.7rem; font-weight: 600; text-transform: uppercase;
      letter-spacing: 2px; color: var(--muted); padding: 7px;
      background: #F8FAFC !important; border-bottom: 1px solid var(--border);
    }}

    /* ── FOOTER ──────────────────────────────────────── */
    footer {{
      text-align: center; padding: 24px; font-size: 0.75rem; color: var(--muted);
      border-top: 1px solid var(--border); margin-top: 40px;
    }}
    footer a {{ color: var(--lblue); }}
    code {{ font-size: 0.85em; }}
  </style>
</head>
<body>

<!-- ── HERO ─────────────────────────────────────────────── -->
<div class="hero">
  <div class="container">
    <div class="hero-ball">🏀</div>
    <div class="hero-title">{CHALLENGE_NAME}</div>
    <div class="hero-sub">Live Standings &amp; Stats</div>
    <div class="hero-updated">Last updated: {last_updated}</div>

    <div class="row g-3 mt-3 justify-content-center">
      <div class="col-6 col-sm-3">
        <div class="stat-card">
          <div class="sc-label">Entries</div>
          <div class="sc-value">{total_p}</div>
          <div class="sc-sub">participants</div>
        </div>
      </div>
      <div class="col-6 col-sm-3">
        <div class="stat-card">
          <div class="sc-label">Leader</div>
          <div class="sc-value" style="font-size:1.8rem;line-height:1.2">{leader['name']}</div>
          <div class="sc-sub">{leader['total_points']:,} pts</div>
        </div>
      </div>
      <div class="col-6 col-sm-3">
        <div class="stat-card">
          <div class="sc-label">Games Played</div>
          <div class="sc-value">{games_played}</div>
          <div class="sc-sub">of 63</div>
        </div>
      </div>
      <div class="col-6 col-sm-3">
        <div class="stat-card">
          <div class="sc-label">Teams Alive</div>
          <div class="sc-value">{alive_count}</div>
          <div class="sc-sub">{elim_count} eliminated</div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ── MAIN CONTENT ──────────────────────────────────────── -->
<div class="container pb-5">

  <!-- Tabs -->
  <ul class="nav nav-tabs" id="mainTabs" role="tablist">
    <li class="nav-item" role="presentation">
      <button class="nav-link active" id="tab-standings-btn"
              data-bs-toggle="tab" data-bs-target="#tab-standings"
              type="button" role="tab">🏆 Standings</button>
    </li>
    <li class="nav-item" role="presentation">
      <button class="nav-link" id="tab-picks-btn"
              data-bs-toggle="tab" data-bs-target="#tab-picks"
              type="button" role="tab">📊 Pick Popularity</button>
    </li>
    <li class="nav-item" role="presentation">
      <button class="nav-link" id="tab-teams-btn"
              data-bs-toggle="tab" data-bs-target="#tab-teams"
              type="button" role="tab">🏀 Team Tracker</button>
    </li>
  </ul>

  <div class="tab-content" id="mainTabContent">

    <!-- ═══ TAB 1 — STANDINGS ══════════════════════════════ -->
    <div class="tab-pane fade show active" id="tab-standings" role="tabpanel">
      <div class="sec-head">Standings</div>
      <div class="d-flex justify-content-between align-items-center mb-3 flex-wrap gap-2">
        <p class="text-muted small mb-0">
          Click any row to expand picks &nbsp;·&nbsp;
          <span class="pick-chip pick-alive" style="font-size:.68rem">✓ alive</span>
          <span class="pick-chip pick-eliminated" style="font-size:.68rem">✗ out</span>
          <span class="pick-chip pick-notplayed" style="font-size:.68rem">· hasn't played</span>
        </p>
        <input id="lb-search" type="text" placeholder="🔍  Search participant…" oninput="filterTable()">
      </div>

      <div class="lb-wrap table-responsive">
        <table class="table mb-0" id="lb-table">
          <thead>
            <tr>
              <th>#</th>
              <th>Participant</th>
              <th class="text-end">Points</th>
              <th class="text-center">Alive</th>
              <th class="d-none d-lg-table-cell">Teams Still In</th>
            </tr>
          </thead>
          <tbody>
{rows_html}
          </tbody>
        </table>
      </div>
    </div>

    <!-- ═══ TAB 2 — PICK POPULARITY ════════════════════════ -->
    <div class="tab-pane fade" id="tab-picks" role="tabpanel">
      <div class="sec-head">Pick Popularity by Seed</div>
      <p class="text-muted small mb-4">
        How participants split their picks for each seed slot.
        Bar labels show the % of entries that picked each team.
      </p>
      <div class="row g-3">
{pop_charts}
      </div>
    </div>

    <!-- ═══ TAB 3 — TEAM TRACKER ════════════════════════════ -->
    <div class="tab-pane fade" id="tab-teams" role="tabpanel">

      <!-- Top Teams -->
      <div class="sec-head">Top Teams by Points</div>
      <p class="text-muted small mb-3">
        Teams ranked by total points earned — regardless of seed.
      </p>
      <div class="tracker-card mb-5 table-responsive">
        <table class="table mb-0">
          <thead>
            <tr>
              <th class="text-center">#</th>
              <th>Team</th>
              <th class="text-center">Seed</th>
              <th class="text-center">Status</th>
              <th class="text-center">Wins</th>
              <th class="text-center">Pts/Win</th>
              <th class="text-end">Points Earned</th>
              <th class="text-center"># Picked</th>
            </tr>
          </thead>
          <tbody>
{top_teams_rows}
          </tbody>
        </table>
      </div>

      <!-- All Teams -->
      <div class="sec-head">All Teams</div>
      <p class="text-muted small mb-3">
        Full tournament status for every picked team. Click column headers to sort.
      </p>
      <div class="tracker-card table-responsive">
        <table class="table mb-0" id="teams-table">
          <thead>
            <tr>
              <th class="text-center" onclick="sortTeams(0)">Seed ↕</th>
              <th onclick="sortTeams(1)">Team ↕</th>
              <th class="text-center" onclick="sortTeams(2)">Status ↕</th>
              <th class="text-center" onclick="sortTeams(3)">Wins ↕</th>
              <th class="text-center">Pts/Win</th>
              <th class="text-end" onclick="sortTeams(5)">Earned ↕</th>
              <th class="text-center" onclick="sortTeams(6)">Picked ↕</th>
              <th class="text-end" onclick="sortTeams(7)">% ↕</th>
            </tr>
          </thead>
          <tbody id="teams-body">
{teams_rows}
          </tbody>
        </table>
      </div>
    </div>

  </div><!-- /tab-content -->
</div><!-- /container -->

<footer>
  Data sourced from ESPN &bull; Re-run <code>process_bracket.py</code> to refresh &bull;
  <a href="https://docs.github.com/en/pages">Hosted on GitHub Pages</a>
</footer>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
<script>
const POPULARITY = {js_popularity};

// ── LAZY-INIT charts when Pick Popularity tab is first shown ──
document.getElementById('tab-picks-btn').addEventListener('shown.bs.tab', function () {{
  if (!window._chartsInit) {{ initCharts(); window._chartsInit = true; }}
}});

// ── TOGGLE PICKS ROW ──────────────────────────────────────
function toggleRow(id) {{
  const r = document.getElementById(id);
  if (r) r.classList.toggle('open');
}}

// ── SEARCH ────────────────────────────────────────────────
function filterTable() {{
  const q = document.getElementById('lb-search').value.toLowerCase();
  document.querySelectorAll('#lb-table tbody .lb-row').forEach(tr => {{
    const show = tr.innerText.toLowerCase().includes(q);
    tr.style.display = show ? '' : 'none';
    const next = tr.nextElementSibling;
    if (next?.classList.contains('picks-row')) {{
      if (!show) next.classList.remove('open');
      next.style.display = show ? '' : 'none';
    }}
  }});
}}

// ── TEAM TABLE SORT ───────────────────────────────────────
const _sd = {{}};
function sortTeams(col) {{
  const tbody = document.getElementById('teams-body');
  const rows  = Array.from(tbody.querySelectorAll('tr:not(.elim-divider)'));
  _sd[col] = !_sd[col];
  const dir = _sd[col] ? 1 : -1;
  rows.sort((a, b) => {{
    const av = (a.cells[col]?.innerText||'').replace(/[^0-9.%-]/g,'');
    const bv = (b.cells[col]?.innerText||'').replace(/[^0-9.%-]/g,'');
    const an = parseFloat(av), bn = parseFloat(bv);
    return isNaN(an)||isNaN(bn)
      ? dir*(a.cells[col]?.innerText||'').localeCompare(b.cells[col]?.innerText||'')
      : dir*(an-bn);
  }});
  const div = tbody.querySelector('.elim-divider');
  rows.forEach(r => r.remove());
  if (div) div.remove();
  const alive = rows.filter(r => !r.classList.contains('tr-out'));
  const out   = rows.filter(r =>  r.classList.contains('tr-out'));
  alive.forEach(r => tbody.appendChild(r));
  if (div && out.length) tbody.appendChild(div);
  out.forEach(r => tbody.appendChild(r));
}}

// ── PICK POPULARITY CHARTS (with % labels) ────────────────
function initCharts() {{
  Chart.register(ChartDataLabels);
  const PALETTE = ['#1A56DB','#E8520A','#16A34A','#7C3AED',
                   '#DB2777','#D97706','#0891B2','#64748B'];
  for (let seed = 1; seed <= 16; seed++) {{
    const raw    = POPULARITY[String(seed)] || {{}};
    const teams  = Object.keys(raw).slice(0, 8);
    const counts = teams.map(t => raw[t]);
    const total  = counts.reduce((a,b) => a+b, 0) || 1;
    const maxVal = Math.max(...counts, 1);
    const el     = document.getElementById('pc' + seed);
    if (!el) continue;
    new Chart(el.getContext('2d'), {{
      type: 'bar',
      data: {{
        labels: teams,
        datasets: [{{
          data: counts,
          backgroundColor: teams.map((_,i) => PALETTE[i%PALETTE.length]+'22'),
          borderColor:     teams.map((_,i) => PALETTE[i%PALETTE.length]),
          borderWidth: 1.5, borderRadius: 4,
        }}]
      }},
      options: {{
        indexAxis: 'y', responsive: true,
        layout: {{ padding: {{ right: 48 }} }},
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{
            callbacks: {{ label: c => ` ${{c.parsed.x}} picks (${{Math.round(c.parsed.x/total*100)}}%)` }}
          }},
          datalabels: {{
            anchor: 'end', align: 'right',
            color: '#64748B', font: {{ size: 11, weight: '600' }},
            formatter: v => Math.round(v/total*100) + '%',
            padding: {{ left: 4 }}
          }}
        }},
        scales: {{
          x: {{ beginAtZero: true, max: maxVal * 1.35,
                ticks: {{ stepSize:1, font:{{size:10}}, color:'#94A3B8' }},
                grid:  {{ color:'#F1F5F9' }} }},
          y: {{ ticks: {{ font:{{size:11, weight:'600'}}, color:'#1E293B' }},
                grid: {{ display:false }} }}
        }}
      }}
    }});
  }}
}}
</script>
</body>
</html>"""


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print("🏀  March Madness Bracket Tracker")
    print("=" * 46)

    excel_path = sys.argv[1] if len(sys.argv) > 1 else EXCEL_FILE
    no_fetch   = "--no-fetch" in sys.argv

    print(f"\n📥  Reading entries from: {excel_path}")
    if not os.path.exists(excel_path):
        print(f"  ❌  File not found: {excel_path}")
        print("  Tip: Download your Google Sheet as .xlsx and rename it.")
        sys.exit(1)
    entries = read_entries(excel_path)
    print(f"  ✓  {len(entries)} unique participants loaded")

    if no_fetch or MANUAL_WINS_OVERRIDE:
        print("\n📋  Using manual wins data (--no-fetch or MANUAL_WINS_OVERRIDE set)")
        wins, eliminated = MANUAL_WINS_OVERRIDE, set()
    else:
        print("\n🌐  Fetching live results from ESPN…")
        wins, eliminated = fetch_tournament_results()
        if not wins:
            print("  ℹ  No completed games found yet — scores will all be 0.")
            print("     (This is expected before the tournament starts.)")

    popularity = get_pick_popularity(entries)

    print("\n📊  Calculating standings…")
    standings = calculate_scores(entries, wins, eliminated)
    if standings:
        print(f"  Leader: {standings[0]['name']}  —  {standings[0]['total_points']:,} pts")

    print(f"\n✍️   Generating website → {OUTPUT_FILE}")
    html = generate_html(standings, wins, eliminated, entries, popularity)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n✅  Done!  Open {OUTPUT_FILE} in your browser.")
    print("     Push to GitHub Pages to share with participants.")
    print("     Re-run this script after each game day to update scores.\n")

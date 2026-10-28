#!/usr/bin/env python3
"""
Brawl Stars tracker: logs your battles, win rates, matchups and map results,
then recommends the best brawler for a given map.

Setup
-----
1. Create an API key at https://developer.brawlstars.com (the key is locked to
   the public IP address you register, so use your current one).
2. Put your key and tag in a file named .env next to this script (add .env to .gitignore):
       BRAWL_API_KEY=your key
       BRAWL_TAG=#YOURTAG
   Or set them as environment variables:
       export BRAWL_API_KEY="your key"
       export BRAWL_TAG="#YOURTAG"
   (or pass --tag). Optionally set BRAWL_API_BASE to use a proxy if your IP changes.
3. Run:
       py brawl_gui.py                                # desktop window (no commands needed)
       python brawl_tracker.py play                  # sync, then asks for the map and recommends
       python brawl_tracker.py play "Hard Rock Mine" # same, map given up front
       python brawl_tracker.py sync                  # fetch last 25 battles once
       python brawl_tracker.py watch                 # keep fetching every 10 min
       python brawl_tracker.py bans "Hard Rock Mine" # who to ban on a map (omit map = overall)
       python brawl_tracker.py history               # see your stored battles, newest first
       python brawl_tracker.py brawlers              # win rate per brawler
       python brawl_tracker.py matchups              # win rate vs each enemy brawler
       python brawl_tracker.py modes                 # win rate per game mode
       python brawl_tracker.py maps                  # win rate per map
       python brawl_tracker.py recommend "Hard Rock Mine"
       python brawl_tracker.py recommend "Hard Rock Mine" --enemy Shelly Colt

The API only returns your 25 most recent battles, so leave `watch` running
while you play (or run `sync` often) to build up history.

Only team modes with a win/loss result are tracked (3v3, ranked, etc.).
Showdown, friendly battles and draws are skipped.
Standard library only, no pip installs needed.
"""
import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

# ---- YOUR SETTINGS: paste them between the quotes --------------------------
API_KEY = ""          # e.g. "eyJ0eXAiOiJKV1Qi..."  (from developer.brawlstars.com)
PLAYER_TAG = ""       # e.g. "#2PP0LCY"
# ------------------------------------------------------------------------------
# (Environment variables BRAWL_API_KEY / BRAWL_TAG override these if set.)

def load_dotenv():
    """Load NAME=value lines from a .env file next to this script (or the current
    folder) into the environment. Real environment variables take priority."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, ".env"), ".env"):
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()

API_BASE = os.environ.get("BRAWL_API_BASE", "https://api.brawlstars.com/v1")
DB_PATH = os.environ.get("BRAWL_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "brawl_tracker.db")

# Shrinkage strengths for the recommender (higher = trust prior more, needs more games).
K_BRAWLER = 10   # brawler's overall win rate  -> pulled toward your global win rate
K_MODE = 6       # brawler in this mode        -> pulled toward its overall win rate
K_MAP = 4        # brawler on this map         -> pulled toward its mode win rate
K_BAN_ALL = 8    # enemy's overall loss rate vs you -> pulled toward your average loss rate
K_BAN_MAP = 4    # enemy's loss rate on one map      -> pulled toward its overall loss rate
K_MATCHUP = 6    # brawler vs a given enemy    -> pulled toward its map estimate

SCHEMA = """
CREATE TABLE IF NOT EXISTS battles (
    player_tag    TEXT NOT NULL,
    battle_time   TEXT NOT NULL,
    mode          TEXT,
    map           TEXT,
    type          TEXT,
    my_brawler    TEXT,
    result        TEXT,
    trophy_change INTEGER,
    PRIMARY KEY (player_tag, battle_time)
);
CREATE TABLE IF NOT EXISTS opponents (
    player_tag  TEXT NOT NULL,
    battle_time TEXT NOT NULL,
    brawler     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opp ON opponents (player_tag, battle_time);
"""


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------
def norm_tag(tag):
    tag = tag.strip().upper().replace("O", "0")
    return tag if tag.startswith("#") else "#" + tag


def fetch_battlelog(tag, key):
    url = f"{API_BASE}/players/{urllib.parse.quote(tag, safe='')}/battlelog"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        hints = {
            403: "API key invalid, or your current IP isn't whitelisted for this key.",
            404: "Player tag not found.",
            429: "Rate limited. Try a longer interval.",
            503: "API under maintenance.",
        }
        raise RuntimeError(f"HTTP {e.code}: {hints.get(e.code, e.reason)}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from None


def parse_battle(item, my_tag):
    """Turn one battlelog item into a dict, or None if it should be skipped."""
    b = item.get("battle", {})
    ev = item.get("event", {})
    if b.get("result") not in ("victory", "defeat"):
        return None  # showdown, draws, etc.
    if b.get("type") == "friendly":
        return None
    teams = b.get("teams")
    if not teams:
        return None

    my_idx, my_brawler = None, None
    for i, team in enumerate(teams):
        for p in team:
            if norm_tag(p.get("tag", "")) == my_tag:
                my_idx, my_brawler = i, p["brawler"]["name"].upper()
    if my_idx is None:
        return None

    opponents = [
        p["brawler"]["name"].upper()
        for i, team in enumerate(teams) if i != my_idx
        for p in team
    ]
    return {
        "battle_time": item["battleTime"],
        "mode": ev.get("mode") or b.get("mode") or "unknown",
        "map": ev.get("map") or "Unknown",
        "type": b.get("type"),
        "my_brawler": my_brawler,
        "result": b["result"],
        "trophy_change": b.get("trophyChange"),
        "opponents": opponents,
    }


def sync(conn, tag, key, verbose=True):
    data = fetch_battlelog(tag, key)
    new = skipped = 0
    for item in data.get("items", []):
        r = parse_battle(item, tag)
        if not r:
            skipped += 1
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO battles (player_tag, battle_time, mode, map, type,"
            " my_brawler, result, trophy_change) VALUES (?,?,?,?,?,?,?,?)",
            (tag, r["battle_time"], r["mode"], r["map"], r["type"],
             r["my_brawler"], r["result"], r["trophy_change"]),
        )
        if cur.rowcount:
            new += 1
            conn.executemany(
                "INSERT INTO opponents (player_tag, battle_time, brawler) VALUES (?,?,?)",
                [(tag, r["battle_time"], o) for o in r["opponents"]],
            )
    conn.commit()
    total = conn.execute(
        "SELECT COUNT(*) FROM battles WHERE player_tag=?", (tag,)
    ).fetchone()[0]
    if verbose:
        print(f"[{time.strftime('%H:%M:%S')}] {new} new, {skipped} skipped. {total} battles stored.")
    return new, skipped, total


# ----------------------------------------------------------------------------
# Stats helpers
# ----------------------------------------------------------------------------
def load_battles(conn, tag):
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM battles WHERE player_tag=?", (tag,))]
    conn.row_factory = None
    return rows


def load_matchups(conn, tag):
    """{(my_brawler, enemy_brawler): [wins, games]}"""
    d = defaultdict(lambda: [0, 0])
    q = ("SELECT b.my_brawler, o.brawler, b.result FROM opponents o "
         "JOIN battles b ON b.player_tag=o.player_tag AND b.battle_time=o.battle_time "
         "WHERE b.player_tag=?")
    for mine, enemy, result in conn.execute(q, (tag,)):
        d[(mine, enemy)][1] += 1
        d[(mine, enemy)][0] += result == "victory"
    return d


def load_enemy_rows(conn, tag):
    """One row per (battle, enemy brawler): map, mode, enemy, result."""
    q = ("SELECT DISTINCT b.battle_time, b.map, b.mode, o.brawler, b.result FROM opponents o "
         "JOIN battles b ON b.player_tag=o.player_tag AND b.battle_time=o.battle_time "
         "WHERE b.player_tag=?")
    return [dict(time=t, map=m, mode=mo, enemy=e, result=r)
            for t, m, mo, e, r in conn.execute(q, (tag,))]


def tally(rows, keyfn):
    d = defaultdict(lambda: [0, 0])
    for r in rows:
        k = keyfn(r)
        d[k][1] += 1
        d[k][0] += r["result"] == "victory"
    return d


def pct(w, n):
    return f"{100 * w / n:5.1f}%" if n else "   -  "


def est(w, n, prior, k):
    """Posterior mean win rate with a Beta prior worth k games."""
    return (w + k * prior) / (n + k)


def logit(p):
    p = min(max(p, 0.01), 0.99)
    return math.log(p / (1 - p))


def sigmoid(x):
    return 1 / (1 + math.exp(-x))


TABLE_SINK = None  # the GUI sets this to a list to collect tables instead of printing them


def table(headers, rows):
    if TABLE_SINK is not None:
        TABLE_SINK.append((list(headers), [[str(c) for c in r] for r in rows]))
        return
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]
    line = lambda r: "  ".join(str(c).ljust(w) for c, w in zip(r, widths))
    print(line(headers))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(line(r))


def nice(name):
    return name.title()


# ----------------------------------------------------------------------------
# Reports
# ----------------------------------------------------------------------------
def report_brawlers(rows, min_games):
    t = tally(rows, lambda r: r["my_brawler"])
    out = sorted(((b, w, n) for b, (w, n) in t.items() if n >= min_games),
                 key=lambda x: -x[1] / x[2])
    table(["Brawler", "Games", "Wins", "Win rate"],
          [(nice(b), n, w, pct(w, n)) for b, w, n in out])


def report_bans(rows, erows, map_query, min_games, top):
    """Enemy brawlers that beat you most, on one map (or overall if no map given)."""
    the_map = None
    if map_query:
        q = map_query.lower()
        maps = {r["map"] for r in rows}
        found = [m for m in maps if m.lower() == q] or [m for m in maps if q in m.lower()]
        if len(found) > 1:
            print("Ambiguous map name, did you mean one of:", ", ".join(sorted(found)))
            return 1
        if found:
            the_map = found[0]
        else:
            print(f"No history on '{map_query}' yet, so these are your worst matchups overall.\n")

    base_all = sum(r["result"] == "defeat" for r in rows) / len(rows)
    all_t, map_t = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    for r in erows:
        lost = r["result"] == "defeat"
        all_t[r["enemy"]][0] += lost
        all_t[r["enemy"]][1] += 1
        if the_map and r["map"] == the_map:
            map_t[r["enemy"]][0] += lost
            map_t[r["enemy"]][1] += 1

    out = []
    for e, (la, na) in all_t.items():
        p_all = est(la, na, base_all, K_BAN_ALL)
        if the_map:
            lm, nm = map_t.get(e, (0, 0))
            if nm >= min_games:
                out.append((est(lm, nm, p_all, K_BAN_MAP), e, nm, lm, la, na))
        elif na >= min_games:
            out.append((p_all, e, na, la, la, na))
    out.sort(reverse=True)

    if the_map:
        mrows = [r for r in rows if r["map"] == the_map]
        base_map = sum(r["result"] == "defeat" for r in mrows) / len(mrows)
        print(f"Ban suggestions for {the_map} (your average loss rate here: {100 * base_map:.0f}%)\n")
    else:
        print(f"Ban suggestions overall (your average loss rate: {100 * base_all:.0f}%)\n")
    if not out:
        print(f"No enemy brawler has been faced {min_games}+ times here yet. Play more games.")
        return 0
    table(["#", "Ban", "Seen", "You lost", "Loss rate", "All maps (lost/seen)"],
          [(i, nice(e), n, l, f"{100 * p:.0f}%", f"{la}/{na}")
           for i, (p, e, n, l, la, na) in enumerate(out[:top], 1)])
    print("\nLoss rate = how often you lose when they're on the enemy team, adjusted so small "
          "samples\nstay near your average. It counts all your brawlers, not just one.")
    return 0


def report_history(conn, tag, limit):
    q = ("SELECT b.battle_time, b.map, b.mode, b.my_brawler, b.result, b.trophy_change,"
         " (SELECT GROUP_CONCAT(o.brawler, ', ') FROM opponents o"
         "  WHERE o.player_tag=b.player_tag AND o.battle_time=b.battle_time)"
         " FROM battles b WHERE b.player_tag=? ORDER BY b.battle_time DESC LIMIT ?")
    out = []
    for t, mp, mode, mine, res, tc, opp in conn.execute(q, (tag, limit)):
        when = f"{t[0:4]}-{t[4:6]}-{t[6:8]} {t[9:11]}:{t[11:13]}"  # UTC
        out.append((when, mp, mode, nice(mine), "Win" if res == "victory" else "Loss",
                    "" if tc is None else f"{tc:+d}",
                    ", ".join(nice(x) for x in (opp or "").split(", ") if x)))
    table(["Time (UTC)", "Map", "Mode", "Brawler", "Result", "Trophies", "Enemies"], out)


def pretty_mode(m):
    """'gemGrab' -> 'Gem Grab'"""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", m).title()


def report_modes(rows, min_games):
    by_mode = defaultdict(list)
    for r in rows:
        by_mode[r["mode"]].append(r)
    out = []
    for mode, mrows in by_mode.items():
        n = len(mrows)
        if n < min_games:
            continue
        w = sum(r["result"] == "victory" for r in mrows)
        cands = [(bw / bn, bn, b, bw) for b, (bw, bn) in
                 tally(mrows, lambda r: r["my_brawler"]).items() if bn >= 3]
        best = "-"
        if cands:
            rate, bn, b, bw = max(cands)
            best = f"{nice(b)} ({100 * rate:.0f}%, {bn} games)"
        out.append((w / n, n, mode, w, best))
    out.sort(reverse=True)
    table(["Mode", "Games", "Wins", "Losses", "Win rate", "Best brawler (3+ games)"],
          [(pretty_mode(m), n, w, n - w, pct(w, n), best) for _, n, m, w, best in out])


def report_maps(rows, min_games):
    t = tally(rows, lambda r: (r["map"], r["mode"]))
    out = sorted(((k, w, n) for k, (w, n) in t.items() if n >= min_games),
                 key=lambda x: -x[2])
    table(["Map", "Mode", "Games", "Win rate"],
          [(m, mode, n, pct(w, n)) for (m, mode), w, n in out])


def report_matchups(matchups, min_games, my_brawler):
    agg = defaultdict(lambda: [0, 0])
    for (mine, enemy), (w, n) in matchups.items():
        if my_brawler and mine != my_brawler.upper():
            continue
        agg[enemy][0] += w
        agg[enemy][1] += n
    out = sorted(((e, w, n) for e, (w, n) in agg.items() if n >= min_games),
                 key=lambda x: x[1] / x[2])
    title = f"Your {nice(my_brawler)}" if my_brawler else "You"
    print(f"{title} vs each enemy brawler (worst matchups first)\n")
    table(["Enemy", "Games", "Win rate"], [(nice(e), n, pct(w, n)) for e, w, n in out])


def recommend(rows, matchups, map_query, enemies, min_games, top, mode_hint=None):
    if not rows:
        print("No battles stored yet. Run `sync` first.")
        return 1
    maps = {r["map"] for r in rows}
    q = map_query.lower()
    found = [m for m in maps if m.lower() == q] or [m for m in maps if q in m.lower()]
    if len(found) > 1:
        print("Ambiguous map name, did you mean one of:", ", ".join(sorted(found)))
        return 1
    if found:
        the_map = found[0]
        mode = Counter(r["mode"] for r in rows if r["map"] == the_map).most_common(1)[0][0]
    else:
        # Never played this map: fall back to mode (if given) or overall results.
        the_map = map_query
        modes = {r["mode"] for r in rows}
        hint = (mode_hint or "").lower()
        mode = next((m for m in modes if m.lower() == hint), None)
        print(f"No history on '{map_query}' yet.", end=" ")
        if mode:
            print(f"Ranking by your {mode} results.\n")
        else:
            print("Ranking by overall results (pass --mode, e.g. gemGrab, for better picks).\n")

    g = sum(r["result"] == "victory" for r in rows) / len(rows)
    overall = tally(rows, lambda r: r["my_brawler"])
    in_mode = tally([r for r in rows if r["mode"] == mode], lambda r: r["my_brawler"])
    on_map = tally([r for r in rows if r["map"] == the_map], lambda r: r["my_brawler"])
    enemies = [e.upper() for e in (enemies or [])]

    results = []
    for b, (w, n) in overall.items():
        if n < min_games:
            continue
        p_all = est(w, n, g, K_BRAWLER)
        mw, mn = in_mode.get(b, (0, 0))
        p_mode = est(mw, mn, p_all, K_MODE)
        xw, xn = on_map.get(b, (0, 0))
        p_map = est(xw, xn, p_mode, K_MAP)

        shift, vs_notes = 0.0, []
        for e in enemies:
            ew, en = matchups.get((b, e), (0, 0))
            shift += logit(est(ew, en, p_map, K_MATCHUP)) - logit(p_map)
            if en:
                vs_notes.append(f"{ew}/{en} vs {nice(e)}")
        score = sigmoid(logit(p_map) + shift)
        results.append((score, b, xw, xn, vs_notes))

    if not results:
        print(f"No brawler has {min_games}+ games yet. Lower --min-games or play more.")
        return 1
    results.sort(reverse=True)

    games_here = sum(xn for _, _, _, xn, _ in results)
    print(f"Map: {the_map}  ({mode or 'mode unknown'}, {games_here} games here)")
    if enemies:
        print("Enemy picks:", ", ".join(nice(e) for e in enemies))
    print()
    table(["#", "Brawler", "Predicted", "On this map", "Notes"], [
        (i, nice(b), f"{100 * s:.1f}%",
         f"{xw}W-{xn - xw}L" if xn else "no data",
         "; ".join(notes))
        for i, (s, b, xw, xn, notes) in enumerate(results[:top], 1)
    ])
    print("\nPredicted = estimated win probability, blending your map results with your "
          "mode and overall\nresults for that brawler. Brawlers with few games on the map "
          "stay close to their mode average.")
    return 0


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Brawl Stars win-rate tracker & map recommender")
    ap.add_argument("--tag", default=(os.environ.get("BRAWL_TAG") or PLAYER_TAG), help="player tag, e.g. #ABC123")
    ap.add_argument("--db", default=DB_PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("play", help="sync battle log, then recommend a brawler for the map you enter")
    pl.add_argument("map", nargs="?", help="current map (prompted if omitted)")
    pl.add_argument("--enemy", nargs="*", help="known enemy brawlers")
    pl.add_argument("--mode", help="mode, only needed for a map you've never played (e.g. gemGrab)")
    pl.add_argument("--min-games", type=int, default=3)
    pl.add_argument("--top", type=int, default=8)
    pl.add_argument("--bans", type=int, default=3, help="how many ban suggestions to show (0 = none)")
    pl.add_argument("--no-sync", action="store_true", help="skip fetching the battle log")

    bn = sub.add_parser("bans", help="enemy brawlers you lose to most, on a map (or overall)")
    bn.add_argument("map", nargs="?", help="map name; omit for all maps combined")
    bn.add_argument("--min-games", type=int, default=2)
    bn.add_argument("--top", type=int, default=5)

    h = sub.add_parser("history", help="show stored battles, newest first")
    h.add_argument("--limit", type=int, default=30, help="how many rows (default 30)")

    sub.add_parser("sync", help="fetch recent battles once")
    w = sub.add_parser("watch", help="fetch repeatedly")
    w.add_argument("--interval", type=int, default=600, help="seconds between fetches")

    b = sub.add_parser("brawlers", help="win rate per brawler")
    b.add_argument("--min-games", type=int, default=1)
    md = sub.add_parser("modes", help="win rate per game mode")
    md.add_argument("--min-games", type=int, default=1)
    m = sub.add_parser("maps", help="win rate per map")
    m.add_argument("--min-games", type=int, default=1)
    mu = sub.add_parser("matchups", help="win rate vs each enemy brawler")
    mu.add_argument("--min-games", type=int, default=3)
    mu.add_argument("--brawler", help="only your games on this brawler")

    r = sub.add_parser("recommend", help="best brawler for a map")
    r.add_argument("map")
    r.add_argument("--enemy", nargs="*", help="known enemy brawlers")
    r.add_argument("--min-games", type=int, default=3, help="min total games to consider a brawler")
    r.add_argument("--top", type=int, default=8)

    args = ap.parse_args()
    if not args.tag:
        sys.exit("Set PLAYER_TAG in the script, or BRAWL_TAG, or pass --tag #YOURTAG")
    tag = norm_tag(args.tag)
    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)

    if args.cmd == "play":
        key = (os.environ.get("BRAWL_API_KEY") or API_KEY)
        if args.no_sync:
            pass
        elif not key:
            print("[warn] BRAWL_API_KEY not set, using stored battles only.")
        else:
            try:
                sync(conn, tag, key)
            except RuntimeError as e:
                print(f"[warn] Sync failed ({e}). Using stored battles only.")
        map_name, enemies = args.map, args.enemy
        if not map_name:
            map_name = input("Current map: ").strip()
            if not map_name:
                sys.exit("No map entered.")
            if enemies is None:
                raw = input("Enemy brawlers if known (comma-separated, Enter to skip): ").strip()
                enemies = [e.strip() for e in raw.split(",") if e.strip()]
        print()
        rows = load_battles(conn, tag)
        rc = recommend(rows, load_matchups(conn, tag), map_name, enemies,
                       args.min_games, args.top, args.mode)
        if rc == 0 and args.bans > 0:
            print("\n" + "=" * 60 + "\n")
            report_bans(rows, load_enemy_rows(conn, tag), map_name, 2, args.bans)
        return rc

    if args.cmd in ("sync", "watch"):
        key = (os.environ.get("BRAWL_API_KEY") or API_KEY)
        if not key:
            sys.exit("Set API_KEY in the script or BRAWL_API_KEY")
        try:
            sync(conn, tag, key)
        except RuntimeError as e:
            if args.cmd == "sync":
                sys.exit(str(e))
            print(f"[warn] {e}")
        while args.cmd == "watch":
            time.sleep(args.interval)
            try:
                sync(conn, tag, key)
            except RuntimeError as e:
                print(f"[warn] {e}")
        return 0

    rows = load_battles(conn, tag)
    if not rows:
        sys.exit("No battles stored yet. Run `sync` first.")
    if args.cmd == "bans":
        return report_bans(rows, load_enemy_rows(conn, tag), args.map, args.min_games, args.top)
    if args.cmd == "history":
        report_history(conn, tag, args.limit)
    elif args.cmd == "brawlers":
        report_brawlers(rows, args.min_games)
    elif args.cmd == "modes":
        report_modes(rows, args.min_games)
    elif args.cmd == "maps":
        report_maps(rows, args.min_games)
    elif args.cmd == "matchups":
        report_matchups(load_matchups(conn, tag), args.min_games, args.brawler)
    elif args.cmd == "recommend":
        return recommend(rows, load_matchups(conn, tag), args.map, args.enemy,
                         args.min_games, args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())

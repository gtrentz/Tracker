#!/usr/bin/env python3
"""
Brawl Stars Tracker - desktop window.

Run:  py brawl_gui.py
Keep this file in the same folder as brawl_tracker.py. On first launch click
"Settings" and enter your player tag and API key (saved to a .env file).
Optional: install the free "Lilita One" font (Google Fonts) and it is used everywhere;
without it the window falls back to Nougat / Segoe UI.
"""
import io
import os
import queue
import sqlite3
import threading
import time
import tkinter as tk
from contextlib import redirect_stdout
from tkinter import font as tkfont
from tkinter import ttk

import brawl_tracker as bt

HERE = os.path.dirname(os.path.abspath(__file__))
AUTO_SYNC_MS = 5 * 60 * 1000

# ---------------------------------------------------------------- colors
BG = "#1b1347"        # deep indigo window background
PANEL = "#2b2063"     # table rows / cards
PANEL_ALT = "#34287a"  # alternate rows
EDGE = "#0d0830"      # dark outline around everything
YELLOW, YELLOW_DK = "#ffc712", "#c47f00"
GREEN, GREEN_DK = "#3fcf5b", "#1f8f3a"
BLUE, BLUE_DK = "#2f8bff", "#1553b8"
CYAN = "#4fd8ff"
WIN, LOSS = "#6df08c", "#ff6b75"
FIELD, INK = "#f4efff", "#1b1347"
MUTED = "#b4abe6"


# ---------------------------------------------------------------- helpers
def creds():
    key = os.environ.get("BRAWL_API_KEY") or bt.API_KEY
    tag = os.environ.get("BRAWL_TAG") or bt.PLAYER_TAG
    return key, (bt.norm_tag(tag) if tag else "")


def open_db():
    conn = sqlite3.connect(bt.DB_PATH)
    conn.executescript(bt.SCHEMA)
    return conn


def run(fn, *args):
    """Call a tracker report function; return (return_code, [tables], printed_text)."""
    sink, buf = [], io.StringIO()
    bt.TABLE_SINK = sink
    try:
        with redirect_stdout(buf):
            rc = fn(*args)
    finally:
        bt.TABLE_SINK = None
    return rc, sink, buf.getvalue().strip()


def write_env(tag, key):
    path = os.path.join(HERE, ".env")
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = [l for l in f.read().splitlines()
                     if not l.strip().startswith(("BRAWL_API_KEY", "BRAWL_TAG"))]
    lines += [f"BRAWL_API_KEY={key}", f"BRAWL_TAG={tag}"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def mix(c1, c2, t):
    """Blend two #rrggbb colors; t=0 gives c1, t=1 gives c2."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def pick_font(root, names, default):
    have = {f.lower(): f for f in tkfont.families(root)}
    for n in names:
        n = n.lower()
        if n in have:
            return have[n]
        for low, real in have.items():
            if low.startswith(n):
                return real
    return default


def outlined(canvas, x, y, text, font, fill="white", outline=EDGE, anchor="center", t=2):
    """Game-style text: a dark outline drawn around bright text."""
    for dx in (-t, 0, t):
        for dy in (-t, 0, t):
            if dx or dy:
                canvas.create_text(x + dx, y + dy, text=text, font=font, fill=outline, anchor=anchor)
    canvas.create_text(x, y, text=text, font=font, fill=fill, anchor=anchor)


class Chunky(tk.Canvas):
    """A raised 3D button: rounded, outlined, with a thick bottom edge that squashes when pressed."""
    DEPTH = 5

    def __init__(self, parent, text, command, font, w=140, h=40, face=YELLOW, edge=YELLOW_DK, bg=BG):
        super().__init__(parent, width=w, height=h + self.DEPTH + 2, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self._text, self._font, self.command = text, font, command
        self._bw, self._bh, self._face, self._edge = w, h, face, edge
        self._state = "idle"
        self.bind("<Enter>", lambda e: self._set("hover"))
        self.bind("<Leave>", lambda e: self._set("idle"))
        self.bind("<ButtonPress-1>", lambda e: self._set("down"))
        self.bind("<ButtonRelease-1>", self._release)
        self._draw()

    def set_text(self, text):
        self._text = text
        self._draw()

    def _set(self, state):
        self._state = state
        self._draw()

    def _release(self, e):
        inside = 0 <= e.x <= self._bw and 0 <= e.y <= self._bh + self.DEPTH
        self._set("hover" if inside else "idle")
        if inside and self.command:
            self.command()

    def _rr(self, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
               x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.create_polygon(pts, smooth=True, **kw)

    def _draw(self):
        self.delete("all")
        d, w, h = self.DEPTH, self._bw, self._bh
        face = mix(self._face, "#ffffff", 0.18) if self._state == "hover" else self._face
        y = d - 1 if self._state == "down" else 0
        self._rr(2, d, w - 2, h + d, 12, fill=self._edge, outline=EDGE, width=3)       # thick edge
        self._rr(2, y + 2, w - 2, y + h, 12, fill=face, outline=EDGE, width=3)         # face
        self.create_line(16, y + 9, w - 16, y + 9, fill=mix(face, "#ffffff", 0.5),
                         width=3, capstyle="round")                                    # gloss
        outlined(self, w / 2, y + h / 2 + 2, self._text, self._font)


class Grid(ttk.Frame):
    """A striped table with a scrollbar. Win/Loss cells color the whole row."""

    def __init__(self, parent, height=8, highlight_first=False):
        super().__init__(parent)
        self.highlight_first = highlight_first
        self.tree = ttk.Treeview(self, show="headings", height=height)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.tag_configure("odd", background=PANEL)
        self.tree.tag_configure("even", background=PANEL_ALT)
        self.tree.tag_configure("win", foreground=WIN)
        self.tree.tag_configure("loss", foreground=LOSS)
        self.tree.tag_configure("top", foreground=YELLOW)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def show(self, table):
        t = self.tree
        t.delete(*t.get_children())
        if not table:
            t["columns"] = ()
            return
        headers, rows = table
        ids = [f"c{i}" for i in range(len(headers))]
        t["columns"] = ids
        for cid, h, col in zip(ids, headers, zip(*rows) if rows else [[]] * len(headers)):
            width = max([len(h)] + [len(str(x)) for x in col]) * 9 + 28
            t.heading(cid, text=h, anchor="w")
            t.column(cid, width=min(max(width, 54), 440), anchor="w", stretch=True)
        for i, r in enumerate(rows):
            tags = ["even" if i % 2 else "odd"]
            if "Win" in r:
                tags.append("win")
            elif "Loss" in r:
                tags.append("loss")
            elif self.highlight_first and i == 0:
                tags.append("top")
            t.insert("", "end", values=r, tags=tags)


# ---------------------------------------------------------------- app
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Brawl Stars Tracker")
        self.geometry("1040x780")
        self.minsize(860, 600)
        self.configure(bg=BG)
        self.q = queue.Queue()
        self.syncing = False
        self.status = tk.StringVar(value="Ready")
        self.map_var, self.enemy_var, self.mode_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.min_var = tk.IntVar(value=3)
        self.view = tk.StringVar(value="Brawlers")
        self.auto = tk.BooleanVar(value=True)
        self.setup_fonts_and_styles()
        self.build()
        self.refresh_lists()
        self.after(200, self.poll)
        self.after(AUTO_SYNC_MS, self.tick)
        key, tag = creds()
        if key and tag:
            self.sync()
        else:
            self.status.set("Click Settings to enter your player tag and API key.")

    # ---- look & feel
    def setup_fonts_and_styles(self):
        self.title_family = pick_font(self, ["lilita one", "nougat", "segoe ui black", "arial black"], "Arial")
        heavy = any(k in self.title_family.lower() for k in ("nougat", "black", "lilita"))
        weight = "normal" if heavy else "bold"
        self.body_family = pick_font(self, ["lilita one", "segoe ui", "helvetica neue", "helvetica"], "Arial")
        bweight = "normal" if "lilita" in self.body_family.lower() else "bold"
        self.f_title = lambda size: (self.title_family, size, weight)
        self.f_body = (self.body_family, 11)

        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground="white", font=self.f_body,
                    bordercolor=EDGE, lightcolor=BG, darkcolor=BG, troughcolor=PANEL, focuscolor=BG)
        s.configure("TFrame", background=BG)
        s.configure("Card.TFrame", background=PANEL, bordercolor=EDGE, relief="solid", borderwidth=2)
        s.configure("TLabel", background=BG, foreground="white")
        s.configure("Card.TLabel", background=PANEL, foreground=MUTED, font=(self.body_family, 10, bweight))
        s.configure("H.TLabel", background=BG, foreground=YELLOW, font=self.f_title(15))
        s.configure("Hint.TLabel", background=BG, foreground=MUTED, font=(self.body_family, 10))
        s.configure("Status.TLabel", background=BG, foreground=CYAN, font=(self.body_family, 11, bweight))
        s.configure("TCheckbutton", background=BG, foreground="white")
        s.map("TCheckbutton", background=[("active", BG)], foreground=[("active", YELLOW)])

        for w in ("TEntry", "TCombobox", "TSpinbox"):
            s.configure(w, fieldbackground=FIELD, foreground=INK, background=FIELD,
                        bordercolor=EDGE, padding=5, arrowcolor=INK, insertcolor=INK)
        s.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", INK)],
              selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", INK)])
        self.option_add("*TCombobox*Listbox.background", FIELD)
        self.option_add("*TCombobox*Listbox.foreground", INK)
        self.option_add("*TCombobox*Listbox.selectBackground", YELLOW)
        self.option_add("*TCombobox*Listbox.selectForeground", INK)

        s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(2, 6, 2, 0))
        s.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(22, 8),
                    font=self.f_title(12), borderwidth=2, bordercolor=EDGE)
        s.map("TNotebook.Tab", background=[("selected", YELLOW), ("active", PANEL_ALT)],
              foreground=[("selected", INK), ("active", "white")], expand=[("selected", (0, 0, 0, 2))])

        s.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground="white",
                    rowheight=30, borderwidth=0, font=self.f_body)
        s.map("Treeview", background=[("selected", BLUE)], foreground=[("selected", "white")])
        s.configure("Treeview.Heading", background=YELLOW, foreground=INK, relief="flat",
                    font=self.f_title(10), padding=(6, 6), borderwidth=0)
        s.map("Treeview.Heading", background=[("active", mix(YELLOW, "#ffffff", 0.25))])
        s.configure("Vertical.TScrollbar", background=BLUE, troughcolor=PANEL, bordercolor=EDGE,
                    arrowcolor="white")

    # ---- layout
    def build(self):
        head = ttk.Frame(self, padding=(16, 12, 16, 0))
        head.pack(fill="x")
        title = tk.Canvas(head, width=360, height=52, bg=BG, highlightthickness=0)
        title.pack(side="left")
        outlined(title, 4, 26, "BRAWL TRACKER", self.f_title(26), fill=YELLOW, anchor="w", t=3)
        Chunky(head, "Settings", self.settings, self.f_title(12), w=120, face=BLUE, edge=BLUE_DK).pack(side="right", padx=(8, 0))
        self.sync_btn = Chunky(head, "Sync now", self.sync, self.f_title(12), w=130)
        self.sync_btn.pack(side="right")

        sbar = ttk.Frame(self, padding=(18, 4, 16, 6))
        sbar.pack(fill="x")
        ttk.Label(sbar, textvariable=self.status, style="Status.TLabel").pack(side="left")
        ttk.Checkbutton(sbar, text="Auto-sync every 5 min", variable=self.auto).pack(side="right")

        self.nb = nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        # -- Pick & Ban tab
        f = ttk.Frame(nb, padding=14)
        nb.add(f, text="  Pick & Ban  ")
        form = ttk.Frame(f, style="Card.TFrame", padding=12)
        form.pack(fill="x")
        ttk.Label(form, text="MAP", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        self.map_box = ttk.Combobox(form, textvariable=self.map_var, width=26)
        self.map_box.grid(row=1, column=0, padx=(0, 10), pady=(2, 0))
        ttk.Label(form, text="ENEMY BRAWLERS (optional, comma-separated)", style="Card.TLabel").grid(row=0, column=1, sticky="w")
        enemy = ttk.Entry(form, textvariable=self.enemy_var, width=32)
        enemy.grid(row=1, column=1, padx=(0, 10), pady=(2, 0))
        ttk.Label(form, text="MODE (new maps only)", style="Card.TLabel").grid(row=0, column=2, sticky="w")
        self.mode_box = ttk.Combobox(form, textvariable=self.mode_var, width=13)
        self.mode_box.grid(row=1, column=2, padx=(0, 10), pady=(2, 0))
        ttk.Label(form, text="MIN GAMES", style="Card.TLabel").grid(row=0, column=3, sticky="w")
        ttk.Spinbox(form, from_=1, to=50, width=5, textvariable=self.min_var).grid(row=1, column=3, padx=(0, 14), pady=(2, 0))
        Chunky(form, "Get picks & bans", self.recommend, self.f_title(12), w=190, face=GREEN,
               edge=GREEN_DK, bg=PANEL).grid(row=0, column=4, rowspan=2, sticky="e")
        form.columnconfigure(4, weight=1)
        for w in (self.map_box, enemy):
            w.bind("<Return>", lambda e: self.recommend())

        ttk.Label(f, text="BEST PICKS", style="H.TLabel").pack(anchor="w", pady=(14, 2))
        self.pick_info = ttk.Label(f, justify="left", wraplength=960)
        self.pick_info.pack(anchor="w")
        self.picks = Grid(f, 8, highlight_first=True)
        self.picks.pack(fill="both", expand=True, pady=(4, 2))
        ttk.Label(f, text="Predicted = estimated win chance, blending your results on this map, "
                          "in this mode and overall.", style="Hint.TLabel").pack(anchor="w")

        ttk.Label(f, text="BAN SUGGESTIONS", style="H.TLabel").pack(anchor="w", pady=(14, 2))
        self.ban_info = ttk.Label(f, justify="left", wraplength=960)
        self.ban_info.pack(anchor="w")
        self.bans = Grid(f, 5, highlight_first=True)
        self.bans.pack(fill="both", expand=True, pady=(4, 2))
        ttk.Label(f, text="Loss rate = how often you lose when they're on the enemy team "
                          "(all your brawlers combined).", style="Hint.TLabel").pack(anchor="w")

        # -- Stats tab
        s = ttk.Frame(nb, padding=14)
        nb.add(s, text="  Stats  ")
        top = ttk.Frame(s)
        top.pack(fill="x")
        ttk.Label(top, text="SHOW", style="H.TLabel").pack(side="left", padx=(0, 10))
        cb = ttk.Combobox(top, textvariable=self.view, state="readonly", width=30,
                          values=["Brawlers", "Maps", "Matchups (enemy brawlers)", "History"])
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self.show_stats())
        self.stats_info = ttk.Label(s, justify="left")
        self.stats_info.pack(anchor="w", pady=8)
        self.stats = Grid(s, 20)
        self.stats.pack(fill="both", expand=True)
        nb.bind("<<NotebookTabChanged>>", lambda e: self.show_stats())

    # ---- data refresh
    def refresh_lists(self):
        _, tag = creds()
        maps, modes = [], []
        if tag:
            conn = open_db()
            maps = [r[0] for r in conn.execute(
                "SELECT map FROM battles WHERE player_tag=? GROUP BY map ORDER BY COUNT(*) DESC", (tag,))]
            modes = [r[0] for r in conn.execute(
                "SELECT DISTINCT mode FROM battles WHERE player_tag=? ORDER BY mode", (tag,))]
            conn.close()
        self.map_box["values"] = maps
        self.mode_box["values"] = modes

    def show_stats(self):
        _, tag = creds()
        conn = open_db()
        rows = bt.load_battles(conn, tag) if tag else []
        v = self.view.get()
        if not rows:
            self.stats_info.config(text="No battles stored yet. Click Sync now.")
            self.stats.show(None)
            conn.close()
            return
        if v == "Brawlers":
            _, t, txt = run(bt.report_brawlers, rows, 1)
        elif v == "Maps":
            _, t, txt = run(bt.report_maps, rows, 1)
        elif v.startswith("Matchups"):
            _, t, txt = run(bt.report_matchups, bt.load_matchups(conn, tag), 3, None)
        else:
            _, t, txt = run(bt.report_history, conn, tag, 100)
        conn.close()
        self.stats_info.config(text=txt)
        self.stats.show(t[0] if t else None)

    def recommend(self):
        _, tag = creds()
        map_name = self.map_var.get().strip()
        if not tag:
            self.status.set("Set your player tag in Settings first.")
            return
        if not map_name:
            self.status.set("Enter the current map.")
            return
        try:
            min_games = int(self.min_var.get())
        except (tk.TclError, ValueError):
            min_games = 3
        enemies = [e.strip() for e in self.enemy_var.get().split(",") if e.strip()]
        mode = self.mode_var.get().strip() or None

        conn = open_db()
        rows = bt.load_battles(conn, tag)
        if not rows:
            self.pick_info.config(text="No battles stored yet. Click Sync now.")
            self.picks.show(None)
            self.ban_info.config(text="")
            self.bans.show(None)
            conn.close()
            return
        rc, t, txt = run(bt.recommend, rows, bt.load_matchups(conn, tag), map_name,
                         enemies, min_games, 8, mode)
        self.pick_info.config(text=txt.split("Predicted =")[0].strip())
        self.picks.show(t[0] if t else None)
        if rc == 0:
            _, t2, txt2 = run(bt.report_bans, rows, bt.load_enemy_rows(conn, tag), map_name, 2, 5)
            self.ban_info.config(text=txt2.split("Loss rate =")[0].strip())
            self.bans.show(t2[0] if t2 else None)
        else:
            self.ban_info.config(text="")
            self.bans.show(None)
        conn.close()
        self.status.set(f"Showing picks for {map_name}")

    # ---- syncing (network runs in a background thread so the window stays responsive)
    def sync(self):
        key, tag = creds()
        if not key or not tag:
            self.status.set("Click Settings to enter your player tag and API key.")
            return
        if self.syncing:
            return
        self.syncing = True
        self.status.set("Syncing...")
        self.sync_btn.set_text("Syncing...")

        def work():
            try:
                conn = open_db()
                new, _, total = bt.sync(conn, tag, key, verbose=False)
                conn.close()
                self.q.put((True, f"Synced at {time.strftime('%H:%M')}: {new} new, {total} stored"))
            except RuntimeError as e:
                self.q.put((False, f"Sync failed: {e} (showing stored data)"))
            except Exception as e:  # keep the window alive no matter what
                self.q.put((False, f"Sync error: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            while True:
                ok, msg = self.q.get_nowait()
                self.syncing = False
                self.sync_btn.set_text("Sync now")
                self.status.set(msg)
                self.refresh_lists()
                if ok and self.map_var.get().strip():
                    self.recommend()
                    self.status.set(msg)
                if self.nb.index("current") == 1:
                    self.show_stats()
        except queue.Empty:
            pass
        self.after(200, self.poll)

    def tick(self):
        if self.auto.get():
            self.sync()
        self.after(AUTO_SYNC_MS, self.tick)

    # ---- settings dialog
    def settings(self):
        key, tag = creds()
        win = tk.Toplevel(self)
        win.title("Settings")
        win.configure(bg=BG)
        win.transient(self)
        win.resizable(False, False)
        f = ttk.Frame(win, padding=18)
        f.pack()
        tv, kv = tk.StringVar(value=tag), tk.StringVar(value=key)
        ttk.Label(f, text="PLAYER TAG (e.g. #ABC123)", style="Hint.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=tv, width=48).grid(row=1, column=0, pady=(2, 10))
        ttk.Label(f, text="API KEY", style="Hint.TLabel").grid(row=2, column=0, sticky="w")
        ttk.Entry(f, textvariable=kv, width=48, show="*").grid(row=3, column=0, pady=(2, 0))
        ttk.Label(f, text="Saved to a .env file next to the script.\nAdd .env to your .gitignore.",
                  style="Hint.TLabel").grid(row=4, column=0, sticky="w", pady=(10, 0))

        def save():
            t = bt.norm_tag(tv.get()) if tv.get().strip() else ""
            k = kv.get().strip()
            write_env(t, k)
            os.environ["BRAWL_TAG"], os.environ["BRAWL_API_KEY"] = t, k
            win.destroy()
            self.refresh_lists()
            self.sync()

        Chunky(f, "Save", save, self.f_title(12), w=110, face=GREEN, edge=GREEN_DK).grid(
            row=5, column=0, sticky="e", pady=(12, 0))


if __name__ == "__main__":
    App().mainloop()

"""Durable prompt/settings storage: SQLite locally, PostgreSQL on the free host."""
from __future__ import annotations
import random
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PROMPTS = [
    ("truth", 1, "icebreaker", "What is a small thing that made you smile today?"),
    ("truth", 2, "fun", "What is the most spontaneous thing you have done recently?"),
    ("truth", 3, "stories", "What is a harmless secret talent you have?"),
    ("dare", 1, "icebreaker", "Share a photo of something near you that has a story."),
    ("dare", 2, "fun", "Describe your day using only three emojis."),
    ("dare", 3, "creative", "Write a two-line dramatic review of your favorite snack."),
]

@dataclass(frozen=True)
class Prompt:
    id: int
    kind: str
    intensity: int
    theme: str | None
    text: str

class Store:
    def __init__(self, path: str | Path):
        self.postgres = str(path).startswith(("postgres://", "postgresql://"))
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row
            self.conn = psycopg.connect(str(path), autocommit=True, row_factory=dict_row,
                                       connect_timeout=10, options="-c statement_timeout=10000")
        else:
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, timeout=10)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys=ON")
        self._initialize()

    def execute(self, sql, values=()):
        return self.conn.execute(sql.replace("?", "%s") if self.postgres else sql, values)

    def transaction(self):
        return self.conn.transaction() if self.postgres else self.conn

    def close(self):
        self.conn.close()

    def _initialize(self):
        identity = "SERIAL PRIMARY KEY" if self.postgres else "INTEGER PRIMARY KEY"
        timestamp = "(CURRENT_TIMESTAMP::text)" if self.postgres else "CURRENT_TIMESTAMP"
        with self.transaction():
            self.execute(f"""CREATE TABLE IF NOT EXISTS prompts (
                id {identity}, kind TEXT NOT NULL CHECK(kind IN ('truth','dare')),
                intensity INTEGER NOT NULL CHECK(intensity BETWEEN 1 AND 5),
                theme TEXT, text TEXT NOT NULL UNIQUE)""")
            self.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self.execute(f"""CREATE TABLE IF NOT EXISTS history (id {identity},
                prompt_id INTEGER NOT NULL REFERENCES prompts(id),
                posted_at TEXT NOT NULL DEFAULT {timestamp})""")
            self.execute("CREATE TABLE IF NOT EXISTS scheduled_days (day TEXT PRIMARY KEY, status TEXT NOT NULL)")
        if not self.execute("SELECT 1 FROM prompts LIMIT 1").fetchone():
            self.add_prompts(DEFAULT_PROMPTS)

    def get_setting(self, key, default=None):
        row = self.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_settings(self, values):
        with self.transaction():
            for key, value in values.items():
                self.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def set_setting(self, key, value):
        self.set_settings({key: value})

    def add_prompts(self, prompts):
        # Validate the entire batch before the first write.
        normalized = []
        for kind, intensity, theme, text in prompts:
            kind, text = kind.strip().lower(), text.strip()
            theme = theme.strip() if theme else None
            if kind not in {"truth", "dare"} or isinstance(intensity, bool) or str(intensity) not in {"1", "2", "3", "4", "5"}:
                raise ValueError("Prompt needs truth/dare and intensity 1-5.")
            if not 1 <= len(text) <= 3500 or (theme and len(theme) > 80):
                raise ValueError("Prompt text must be 1-3500 characters; theme at most 80.")
            normalized.append((kind, int(intensity), theme, text))
            if len(normalized) > 500:
                raise ValueError("Import at most 500 prompts per batch.")
        added = 0
        with self.transaction():
            for row in normalized:
                added += self.execute("INSERT INTO prompts(kind,intensity,theme,text) VALUES(?,?,?,?) ON CONFLICT(text) DO NOTHING", row).rowcount
        return added

    def choose_prompt(self, mode="mixed", theme=None, intensity=None):
        if mode not in {"truth", "dare", "mixed"}:
            raise ValueError("Mode must be truth, dare, or mixed.")
        if intensity is not None and (isinstance(intensity, bool) or intensity not in range(1, 6)):
            raise ValueError("Intensity must be 1-5.")
        clauses, values = [], []
        if mode != "mixed": clauses.append("p.kind=?"); values.append(mode)
        if theme: clauses.append("lower(p.theme)=lower(?)"); values.append(theme.strip())
        if intensity is not None: clauses.append("p.intensity=?"); values.append(intensity)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.execute(f"""SELECT p.*, (SELECT MAX(h.id) FROM history h WHERE h.prompt_id=p.id) AS last_used
                               FROM prompts p {where}""", values).fetchall()
        if not rows: return None
        recent = {r["prompt_id"] for r in self.execute("SELECT prompt_id FROM history ORDER BY id DESC LIMIT 20").fetchall()}
        fresh = [r for r in rows if r["id"] not in recent]
        if not fresh:
            oldest = min(r["last_used"] or 0 for r in rows)
            fresh = [r for r in rows if (r["last_used"] or 0) == oldest]
        row = random.choice(fresh)
        return Prompt(*(row[k] for k in ("id", "kind", "intensity", "theme", "text")))

    def record_post(self, prompt_id):
        with self.transaction():
            self.execute("INSERT INTO history(prompt_id) VALUES(?)", (prompt_id,))

    def claim_day(self, day):
        # An interrupted/ambiguous send stays claimed rather than risking a duplicate.
        with self.transaction():
            return self.execute("INSERT INTO scheduled_days(day,status) VALUES(?,?) ON CONFLICT(day) DO NOTHING", (day, "claimed")).rowcount == 1

    def complete_day(self, day):
        with self.transaction():
            self.execute("UPDATE scheduled_days SET status='posted' WHERE day=?", (day,))
        self.set_setting("last_post_date", day)

    def paused(self):
        return self.get_setting("paused", "false") == "true"

    def count_prompts(self):
        return self.execute("SELECT COUNT(*) AS n FROM prompts").fetchone()["n"]

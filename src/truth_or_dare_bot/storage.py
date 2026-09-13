from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

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
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._initialize()

    def close(self) -> None:
        self.conn.close()

    def _initialize(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS prompts (
          id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('truth', 'dare')),
          intensity INTEGER NOT NULL CHECK(intensity BETWEEN 1 AND 5), theme TEXT,
          text TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS history (
          id INTEGER PRIMARY KEY, prompt_id INTEGER NOT NULL,
          posted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(prompt_id) REFERENCES prompts(id)
        );
        """)
        self.conn.commit()
        if not self.conn.execute("SELECT 1 FROM prompts LIMIT 1").fetchone():
            self.add_prompts(DEFAULT_PROMPTS)

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.conn.commit()

    def add_prompts(self, prompts: Iterable[tuple[str, int, str | None, str]]) -> int:
        before = self.conn.total_changes
        for kind, intensity, theme, text in prompts:
            kind = kind.strip().lower()
            if kind not in {"truth", "dare"} or not 1 <= int(intensity) <= 5 or not text.strip():
                raise ValueError("Prompt needs truth/dare, intensity 1-5, and text.")
            self.conn.execute("INSERT OR IGNORE INTO prompts(kind,intensity,theme,text) VALUES(?,?,?,?)", (kind, int(intensity), theme.strip() if theme else None, text.strip()))
        self.conn.commit()
        return self.conn.total_changes - before

    def choose_prompt(self, mode: str = "mixed", theme: str | None = None, intensity: int | None = None) -> Prompt | None:
        clauses, values = [], []
        if mode in {"truth", "dare"}: clauses.append("kind = ?"); values.append(mode)
        if theme: clauses.append("lower(theme) = lower(?)"); values.append(theme)
        if intensity: clauses.append("intensity = ?"); values.append(intensity)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        fresh_where = where + (" AND " if where else "WHERE ") + "id NOT IN (SELECT prompt_id FROM history ORDER BY id DESC LIMIT 20)"
        rows = self.conn.execute(f"SELECT * FROM prompts {fresh_where}", values).fetchall()
        if not rows: rows = self.conn.execute(f"SELECT * FROM prompts {where}", values).fetchall()
        if not rows: return None
        row = random.choice(rows)
        return Prompt(row["id"], row["kind"], row["intensity"], row["theme"], row["text"])

    def record_post(self, prompt_id: int) -> None:
        self.conn.execute("INSERT INTO history(prompt_id) VALUES(?)", (prompt_id,))
        self.conn.commit()

    def paused(self) -> bool:
        return self.get_setting("paused", "false") == "true"

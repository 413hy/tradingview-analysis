import json
import sqlite3
import time
from contextlib import contextmanager
from common.config import settings


class Store:
    def __init__(self, path=None):
        self.path = path or settings.runtime_dir / "trader.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS bot_updates(id TEXT PRIMARY KEY,at REAL);
            CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,kind TEXT,payload TEXT,status TEXT DEFAULT 'PENDING');
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS signals(id TEXT PRIMARY KEY,at REAL,payload TEXT,status TEXT);
            CREATE TABLE IF NOT EXISTS trades(id TEXT PRIMARY KEY,symbol TEXT,side TEXT,status TEXT,
                created_at REAL,payload TEXT);
            CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY,trade_id TEXT,kind TEXT,
                payload TEXT,status TEXT,attempts INTEGER DEFAULT 0,response TEXT);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,at REAL,kind TEXT,payload TEXT);
            CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY,text TEXT,status TEXT DEFAULT 'PENDING');
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, default=None):
        with self.connect() as db:
            r = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return json.loads(r[0]) if r else default

    def set(self, key, value):
        with self.connect() as db:
            db.execute(
                "INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def rows(self, sql, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args)]

    def execute(self, sql, args=()):
        with self.connect() as db:
            return db.execute(sql, args).rowcount

    def event(self, kind, payload, notify=False):
        text = json.dumps(payload, ensure_ascii=False, default=str)
        with self.connect() as db:
            db.execute("INSERT INTO events(at,kind,payload) VALUES (?,?,?)", (time.time(), kind, text))
            if notify:
                db.execute("INSERT INTO outbox(text) VALUES (?)", (kind + "\n" + text[:3000],))

    def claim(self, signal):
        return (
            self.execute(
                "INSERT OR IGNORE INTO signals VALUES (?,?,?,?)",
                (signal["signal_id"], time.time(), json.dumps(signal), "CLAIMED"),
            )
            > 0
        )

    def update_trade(self, tid, status, payload):
        self.execute("UPDATE trades SET status=?,payload=? WHERE id=?", (status, json.dumps(payload), tid))

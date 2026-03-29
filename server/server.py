#!/usr/bin/env python3
"""
WaterH API Server
Receives sip data from the BLE collector, stores in SQLite, serves API.
Deployed on Coolify at water.syl.rest.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

API_TOKEN = os.environ.get("WATERH_API_TOKEN", "changeme")
DB_PATH = os.environ.get("WATERH_DB_PATH", "/data/waterh.db")

app = FastAPI(title="WaterH API", docs_url="/api/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# --- Database ---

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS sips (
            id INTEGER PRIMARY KEY,
            timestamp TEXT UNIQUE NOT NULL,
            intake_ml INTEGER NOT NULL,
            temp_c REAL,
            unknown INTEGER,
            raw_hex TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_sips_date ON sips (DATE(timestamp))")
    db.commit()
    db.close()


@contextmanager
def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


init_db()


# --- Auth ---

def verify_token(authorization: str = Header(None)):
    if not authorization or authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")


# --- Models ---

class Sip(BaseModel):
    id: int | None = None
    timestamp: str
    intake_ml: int
    temp_c: float | None = None
    unknown: int | None = None
    raw_hex: str | None = None


class IngestPayload(BaseModel):
    sips: list[Sip]


# --- Endpoints ---

@app.post("/api/ingest")
def ingest(payload: IngestPayload, authorization: str = Header(None)):
    verify_token(authorization)
    inserted = 0
    with get_db() as db:
        for sip in payload.sips:
            try:
                db.execute(
                    "INSERT INTO sips (timestamp, intake_ml, temp_c, unknown, raw_hex) VALUES (?, ?, ?, ?, ?)",
                    (sip.timestamp, sip.intake_ml, sip.temp_c, sip.unknown, sip.raw_hex),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                pass
        db.commit()
    return {"inserted": inserted, "total": len(payload.sips)}


@app.get("/api/today")
def today():
    today_str = date.today().isoformat()
    with get_db() as db:
        rows = db.execute(
            "SELECT timestamp, intake_ml, temp_c FROM sips WHERE DATE(timestamp) = ? ORDER BY timestamp",
            (today_str,),
        ).fetchall()

    sips = [{"timestamp": r["timestamp"], "intake_ml": r["intake_ml"], "temp_c": r["temp_c"]} for r in rows]
    total_ml = sum(s["intake_ml"] for s in sips)
    goal_ml = 2500

    return {
        "date": today_str,
        "total_ml": total_ml,
        "goal_ml": goal_ml,
        "goal_pct": min(100, round(total_ml / goal_ml * 100)),
        "sip_count": len(sips),
        "last_temp_c": sips[-1]["temp_c"] if sips else None,
        "sips": sips,
    }


@app.get("/api/history")
def history(days: int = 30):
    days = min(days, 365)
    start = (date.today() - timedelta(days=days)).isoformat()
    with get_db() as db:
        rows = db.execute(
            """
            SELECT DATE(timestamp) as day, SUM(intake_ml) as total_ml,
                   COUNT(*) as sip_count, ROUND(AVG(temp_c), 1) as avg_temp_c
            FROM sips WHERE DATE(timestamp) >= ?
            GROUP BY DATE(timestamp) ORDER BY day
            """,
            (start,),
        ).fetchall()

    data = [
        {"date": r["day"], "total_ml": r["total_ml"], "sip_count": r["sip_count"], "avg_temp_c": r["avg_temp_c"]}
        for r in rows
    ]

    totals = [d["total_ml"] for d in data]
    streak = 0
    for d in reversed(data):
        if d["total_ml"] > 0:
            streak += 1
        else:
            break

    return {
        "days": data,
        "avg_daily_ml": round(sum(totals) / len(totals)) if totals else 0,
        "best_day_ml": max(totals) if totals else 0,
        "current_streak": streak,
    }


@app.get("/api/sips")
def sips(date_filter: str | None = None, limit: int = 100, offset: int = 0):
    limit = min(limit, 500)
    with get_db() as db:
        if date_filter:
            rows = db.execute(
                "SELECT timestamp, intake_ml, temp_c, raw_hex FROM sips WHERE DATE(timestamp) = ? ORDER BY timestamp LIMIT ? OFFSET ?",
                (date_filter, limit, offset),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT timestamp, intake_ml, temp_c, raw_hex FROM sips ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()

    return {
        "sips": [dict(r) for r in rows],
        "count": len(rows),
    }


@app.get("/api/widget")
def widget():
    data = today()
    return {
        "today_ml": data["total_ml"],
        "goal_pct": data["goal_pct"],
        "sip_count": data["sip_count"],
        "last_temp_c": data["last_temp_c"],
    }


@app.get("/api/status")
def status():
    with get_db() as db:
        last = db.execute("SELECT MAX(created_at) as last FROM sips").fetchone()
    last_sync = last["last"] if last else None
    online = False
    if last_sync:
        try:
            dt = datetime.fromisoformat(last_sync)
            online = (datetime.now() - dt).total_seconds() < 120
        except ValueError:
            pass
    return {"online": online, "last_sync": last_sync}

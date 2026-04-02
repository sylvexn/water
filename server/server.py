#!/usr/bin/env python3
"""
WaterH API Server
Receives sip data from the BLE collector, stores in SQLite, serves API.
Deployed on Coolify at water.syl.rest.
"""

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiosqlite
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

WATERH_TZ = ZoneInfo(os.environ.get("WATERH_TZ", "America/New_York"))
API_TOKEN = os.environ.get("WATERH_API_TOKEN", "changeme")
DB_PATH = os.environ.get("WATERH_DB_PATH", "/data/waterh.db")

db: aiosqlite.Connection | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("""
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
    await db.execute("CREATE INDEX IF NOT EXISTS idx_sips_date ON sips (DATE(timestamp))")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS heartbeats (
            id INTEGER PRIMARY KEY,
            state TEXT NOT NULL,
            detail TEXT,
            collector_ts TEXT,
            received_at TEXT DEFAULT (datetime('now'))
        )
    """)
    await db.commit()
    yield
    await db.close()


app = FastAPI(title="WaterH API", docs_url="/api/docs", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


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


class HeartbeatPayload(BaseModel):
    state: str
    detail: str = ""
    timestamp: str


# --- Endpoints ---

@app.post("/api/ingest")
async def ingest(payload: IngestPayload, authorization: str = Header(None)):
    verify_token(authorization)
    inserted = 0
    for sip in payload.sips:
        try:
            await db.execute(
                "INSERT INTO sips (timestamp, intake_ml, temp_c, unknown, raw_hex) VALUES (?, ?, ?, ?, ?)",
                (sip.timestamp, sip.intake_ml, sip.temp_c, sip.unknown, sip.raw_hex),
            )
            inserted += 1
        except Exception:
            pass
    await db.commit()
    return {"inserted": inserted, "total": len(payload.sips)}


@app.post("/api/heartbeat")
async def heartbeat(payload: HeartbeatPayload, authorization: str = Header(None)):
    verify_token(authorization)
    await db.execute(
        "INSERT INTO heartbeats (state, detail, collector_ts) VALUES (?, ?, ?)",
        (payload.state, payload.detail, payload.timestamp),
    )
    # Clean up old heartbeats (keep last 7 days)
    await db.execute("DELETE FROM heartbeats WHERE received_at < datetime('now', '-7 days')")
    await db.commit()
    return {"ok": True}


@app.get("/api/today")
async def today():
    today_str = datetime.now(WATERH_TZ).date().isoformat()
    rows = await (await db.execute(
        "SELECT timestamp, intake_ml, temp_c FROM sips WHERE DATE(timestamp) = ? ORDER BY timestamp",
        (today_str,),
    )).fetchall()

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
async def history(days: int = 30):
    days = min(days, 365)
    start = (datetime.now(WATERH_TZ).date() - timedelta(days=days)).isoformat()
    rows = await (await db.execute(
        """
        SELECT DATE(timestamp) as day, SUM(intake_ml) as total_ml,
               COUNT(*) as sip_count, ROUND(AVG(temp_c), 1) as avg_temp_c
        FROM sips WHERE DATE(timestamp) >= ?
        GROUP BY DATE(timestamp) ORDER BY day
        """,
        (start,),
    )).fetchall()

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
async def sips(date_filter: str | None = None, limit: int = 100, offset: int = 0):
    limit = min(limit, 500)
    if date_filter:
        rows = await (await db.execute(
            "SELECT timestamp, intake_ml, temp_c, raw_hex FROM sips WHERE DATE(timestamp) = ? ORDER BY timestamp LIMIT ? OFFSET ?",
            (date_filter, limit, offset),
        )).fetchall()
    else:
        rows = await (await db.execute(
            "SELECT timestamp, intake_ml, temp_c, raw_hex FROM sips ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )).fetchall()

    return {
        "sips": [dict(r) for r in rows],
        "count": len(rows),
    }


@app.get("/api/widget")
async def widget():
    data = await today()
    return {
        "today_ml": data["total_ml"],
        "goal_pct": data["goal_pct"],
        "sip_count": data["sip_count"],
        "last_temp_c": data["last_temp_c"],
    }


@app.get("/api/status")
async def status():
    row = await (await db.execute(
        "SELECT state, detail, collector_ts, received_at FROM heartbeats ORDER BY id DESC LIMIT 1"
    )).fetchone()

    if not row:
        # Fallback: check last sip created_at for backward compat with old collector
        last = await (await db.execute("SELECT MAX(created_at) as last FROM sips")).fetchone()
        last_sync = last["last"] if last else None
        online = False
        if last_sync:
            try:
                dt = datetime.fromisoformat(last_sync).replace(tzinfo=timezone.utc)
                online = (datetime.now(timezone.utc) - dt).total_seconds() < 120
            except ValueError:
                pass
        return {"online": online, "state": "unknown", "detail": "", "last_seen": last_sync}

    received = datetime.fromisoformat(row["received_at"]).replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - received).total_seconds()
    online = age_seconds < 120 and row["state"] in ("connected", "scanning")

    return {
        "online": online,
        "state": row["state"],
        "detail": row["detail"],
        "last_seen": row["received_at"],
        "collector_ts": row["collector_ts"],
    }


@app.get("/api/health")
async def health():
    try:
        await (await db.execute("SELECT 1")).fetchone()
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="db unavailable")

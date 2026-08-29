"""
collect.py - Gimpo airport flight collector
Fetches Korea Airports Corp real-time arrivals API, keeps Gimpo(GMP) only, saves to DB.
Usage:
  py collect.py --inspect   # print Gimpo flights (no save)
  py collect.py             # collect Gimpo flights -> save to DB
"""

import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

from db import get_connection, init_db

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_URL = "https://apis.data.go.kr/B551178/flight-status/arrival"
SERVICE_KEY = (os.getenv("KAC_SERVICE_KEY") or "").strip()
KST = timezone(timedelta(hours=9))
TARGET_AIRPORT = "GMP"
GIMPO_ENCODED = "%EA%B9%80%ED%8F%AC"


def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def fetch_all_arrivals(max_pages=25):
    if not SERVICE_KEY:
        raise RuntimeError("KAC_SERVICE_KEY is missing")

    flights = []
    total_count = None

    for page in range(1, max_pages + 1):
        url = (
            f"{BASE_URL}?serviceKey={SERVICE_KEY}"
            f"&numOfRows=100&pageNo={page}&airport={GIMPO_ENCODED}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode("utf-8")

        root = ET.fromstring(text)
        result_code = root.findtext(".//resultCode")
        if result_code and result_code != "00":
            msg = root.findtext(".//resultMsg")
            raise RuntimeError("API error: " + str(result_code) + " / " + str(msg))

        if total_count is None:
            total_count = int(root.findtext(".//totalCount") or 0)

        page_items = root.findall(".//item")
        if not page_items:
            break

        for item in page_items:
            flights.append({
                "fid":       item.findtext("fid"),
                "flightid":  item.findtext("flightid"),
                "airline":   item.findtext("airline"),
                "dep_code":  item.findtext("depAirportCode"),
                "arr_code":  item.findtext("arrAirportCode"),
                "scheduled": item.findtext("scheduledatetime"),
                "estimated": item.findtext("estimateddatetime"),
                "status":    item.findtext("rmkKor"),
                "io":        item.findtext("io"),
                "line":      item.findtext("line"),
            })

        if len(flights) >= total_count:
            break

    print("[collect] fetched " + str(len(flights)) + " flights (total=" + str(total_count) + ")")
    return flights


def filter_gimpo(flights):
    return [f for f in flights if f["arr_code"] == TARGET_AIRPORT]


def inspect():
    print("[inspect] calling API...")
    gmp = filter_gimpo(fetch_all_arrivals())
    print("[inspect] Gimpo arrivals: " + str(len(gmp)))
    for f in gmp[:15]:
        print("  " + str(f["flightid"]) + " " + str(f["airline"]) +
              " " + str(f["dep_code"]) + "->GMP  sched " + str(f["scheduled"]) +
              "  est " + str(f["estimated"]) + "  [" + str(f["status"]) + "]")


def save(flights):
    conn = get_connection()
    cur = conn.cursor()
    now = now_kst()
    changed = 0

    for f in flights:
        key = f["fid"]
        cur.execute("SELECT status, estimated_dt FROM flights WHERE flight_key=?", (key,))
        row = cur.fetchone()

        cur.execute("""
            INSERT INTO flights
                (flight_key, flight_id, airline, airport, io_type,
                 scheduled_dt, estimated_dt, status, collected_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(flight_key) DO UPDATE SET
                estimated_dt=excluded.estimated_dt,
                status=excluded.status,
                updated_at=excluded.updated_at
        """, (key, f["flightid"], f["airline"], f["arr_code"], f["io"],
              f["scheduled"], f["estimated"], f["status"], now, now))

        if row is None or row["status"] != f["status"] or row["estimated_dt"] != f["estimated"]:
            cur.execute("""
                INSERT INTO flight_events (flight_key, status, estimated_dt, collected_at)
                VALUES (?,?,?,?)
            """, (key, f["status"], f["estimated"], now))
            changed += 1

    cur.execute("""
        INSERT INTO collection_log
            (collected_at, airport, rows_fetched, rows_changed, success, error_message)
        VALUES (?,?,?,?,?,?)
    """, (now, TARGET_AIRPORT, len(flights), changed, 1, None))

    conn.commit()
    conn.close()
    print("[save] done: Gimpo " + str(len(flights)) + " (changed " + str(changed) + ") @ " + now)


def collect():
    init_db()
    gmp = filter_gimpo(fetch_all_arrivals())
    save(gmp)


if __name__ == "__main__":
    if "--inspect" in sys.argv:
        inspect()
    else:
        collect()

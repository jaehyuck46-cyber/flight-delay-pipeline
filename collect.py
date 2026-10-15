"""
collect.py - Gimpo airport flight collector
Fetches Korea Airports Corp real-time arrivals API, keeps Gimpo(GMP) only, saves to DB.
Usage:
  py collect.py --inspect   # print Gimpo flights (no save)
  py collect.py             # collect Gimpo flights -> save to DB
"""

import os
import sys
import time
import urllib.request
import urllib.error
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

        # 재시도: 타임아웃 등 통신 에러가 나면 쉬었다가 다시 (최대 3번)
        # URLError는 '연결' 단계 실패만 잡는다. 연결된 뒤 응답을 '읽다가' 시간이 넘으면
        # TimeoutError(= socket.timeout)가 따로 나서, 둘 다 같은 규칙으로 재시도한다.
        text = None
        for attempt in range(1, 4):                     # 1, 2, 3번째 시도
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    text = r.read().decode("utf-8")
                break                                    # 성공하면 반복 탈출
            except (urllib.error.URLError, TimeoutError) as e:
                print("[retry] page " + str(page) + " attempt " + str(attempt) + " failed: " + str(e))
                if attempt == 3:                         # 3번째도 실패하면
                    raise                                # 그때는 진짜 포기(에러 던짐)
                time.sleep(attempt * 3)                  # 3초, 6초 쉬고 재시도

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


def log_failure(error):
    """수집이 실패했을 때 collection_log에 success=0과 에러 내용을 남긴다.

    실패를 기록하지 않으면 Actions가 빨간불이어도 DB에는 아무 흔적이 없어서,
    brief.py의 '실패' 횟수가 늘 0으로 보인다. 그래서 종료하기 전에 한 줄 남긴다.
    받아온 편이 없으므로 rows_fetched/rows_changed는 NULL로 둔다.
    """
    conn = get_connection()
    conn.execute("""
        INSERT INTO collection_log
            (collected_at, airport, rows_fetched, rows_changed, success, error_message)
        VALUES (?,?,?,?,?,?)
    """, (now_kst(), TARGET_AIRPORT, None, None, 0, f"{type(error).__name__}: {error}"))
    conn.commit()
    conn.close()


def collect():
    init_db()
    try:
        gmp = filter_gimpo(fetch_all_arrivals())
    except Exception as e:
        # 재시도 3번 실패, API 에러 코드, 키 누락 등 '받아오기' 단계의 모든 실패.
        # 실패를 기록한 뒤 exit 1로 끝내야 Actions 단계도 빨간불로 남는다.
        log_failure(e)
        print("[collect] failed: " + type(e).__name__ + ": " + str(e))
        sys.exit(1)
    save(gmp)


if __name__ == "__main__":
    if "--inspect" in sys.argv:
        inspect()
    else:
        collect()

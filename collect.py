"""
collect.py — 데이터 수집 담당 파일

역할: 한국공항공사 실시간 도착편 API를 호출 → 김포(GMP) 편만 골라 → DB 저장
사용법:
  py collect.py --inspect   # 김포 편만 화면에 출력 (저장 안 함)
  py collect.py             # 김포 편 수집 → DB 저장
"""

import os
import sys
import xml.etree.ElementTree as ET   # XML 파싱용 (파이썬 기본 내장)
from datetime import datetime, timezone, timedelta

import urllib.request, urllib.parse
from dotenv import load_dotenv

from db import get_connection, init_db

load_dotenv()

# ── 설정 ──
BASE_URL = "https://apis.data.go.kr/B551178/flight-status/arrival"
SERVICE_KEY = os.getenv("KAC_SERVICE_KEY")
KST = timezone(timedelta(hours=9))

# 김포공항 코드 (도착편이니 arrAirportCode 가 GMP 인 것만)
TARGET_AIRPORT = "GMP"

# 지연 판단 기준: 예정보다 15분 초과
DELAY_THRESHOLD_MIN = 15


def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
def fetch_all_arrivals(max_pages=25):
    """API를 여러 페이지 돌며 전국 도착편을 다 받아 dict 리스트로 반환.
    이 API는 한 번에 최대 100건만 주므로 pageNo를 넘겨가며 모은다.
    """
    import urllib.request

    if not SERVICE_KEY:
        raise RuntimeError("❌ .env 에 KAC_SERVICE_KEY 가 없어!")

    flights = []
    total_count = None

    for page in range(1, max_pages + 1):
        # ⚠️ numOfRows 는 반드시 100 이하! (초과하면 빈 응답 옴)
               # airport=김포 로 김포 도착편만 받는다 (전국 다 받고 거르는 것보다 훨씬 가벼움)
        airport = urllib.parse.quote("김포")
        url = f"{BASE_URL}?serviceKey={SERVICE_KEY}&numOfRows=100&pageNo={page}&airport={airport}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode("utf-8")

        root = ET.fromstring(text)
        result_code = root.findtext(".//resultCode")
        if result_code and result_code != "00":
            msg = root.findtext(".//resultMsg")
            raise RuntimeError(f"API 에러: {result_code} / {msg}")

        if total_count is None:
            total_count = int(root.findtext(".//totalCount") or 0)

        page_items = root.findall(".//item")
        if not page_items:      # 더 이상 데이터 없으면 중단
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

        # 다 받았으면 중단
        if len(flights) >= total_count:
            break

    print(f"📦 전국 도착편 {len(flights)}건 수집 (totalCount={total_count})")
    return flights


def filter_gimpo(flights):
    """전국 도착편 중 김포(GMP) 도착만 골라낸다."""
    return [f for f in flights if f["arr_code"] == TARGET_AIRPORT]


def inspect():
    """저장 없이 김포 도착편만 화면에 출력."""
    print("🔍 API 호출 → 김포 도착편 추리는 중...\n")
    all_flights = fetch_all_arrivals()
    gmp = filter_gimpo(all_flights)

    print(f"전체 도착편: {len(all_flights)}건 / 그중 김포(GMP): {len(gmp)}건\n")
    print("── 김포 도착편 미리보기 ──")
    for f in gmp[:15]:
        print(f"  {f['flightid']:8} {f['airline']:10} "
              f"{f['dep_code']}→GMP  예정 {f['scheduled']}  "
              f"예상 {f['estimated']}  [{f['status']}]")


def save(flights):
    """김포 도착편을 DB에 저장 (UPSERT + 상태변경 이력)."""
    conn = get_connection()
    cur = conn.cursor()
    now = now_kst()
    changed = 0

    for f in flights:
        # 고유키: API의 fid 사용 (편별 고유)
        key = f["fid"]

        # 기존 상태 조회
        cur.execute("SELECT status, estimated_dt FROM flights WHERE flight_key=?", (key,))
        row = cur.fetchone()

        # flights 테이블 UPSERT
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

        # 상태나 예상시각이 바뀌었으면 이력 남기기
        if row is None or row["status"] != f["status"] or row["estimated_dt"] != f["estimated"]:
            cur.execute("""
                INSERT INTO flight_events (flight_key, status, estimated_dt, collected_at)
                VALUES (?,?,?,?)
            """, (key, f["status"], f["estimated"], now))
            changed += 1

    # 수집 로그
    cur.execute("""
        INSERT INTO collection_log
            (collected_at, airport, rows_fetched, rows_changed, success, error_message)
        VALUES (?,?,?,?,?,?)
    """, (now, TARGET_AIRPORT, len(flights), changed, 1, None))

    conn.commit()
    conn.close()
    print(f"✅ 저장 완료: 김포 {len(flights)}건 (상태변경 {changed}건) @ {now}")


def collect():
    """실제 수집: API → 김포 필터 → 저장."""
    init_db()   # 테이블 없으면 만들기
    all_flights = fetch_all_arrivals()
    gmp = filter_gimpo(all_flights)
    save(gmp)


if __name__ == "__main__":
    if "--inspect" in sys.argv:
        inspect()
    else:
        collect()
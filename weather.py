"""
weather.py — 서울(108) ASOS 시간관측 수집 파일

역할: 기상청 ASOS 시간자료 API를 호출해서, '실제 관측된' 과거 날씨를
      DB의 weather 테이블에 저장한다. (예보 아님 — 검증된 관측값)
      김포엔 ASOS 지점이 없어 가장 가까운 서울(108)을 대체 지점으로 쓴다.

      collect.py 가 항공편을 담당하듯, 이 파일은 날씨를 담당한다.

사용법:
  py weather.py --inspect                  # 어제 하루치 관측 출력만 (저장 안 함)
  py weather.py --backfill 20260826 20260913   # 범위 백필 -> DB 저장
  py weather.py                            # 어제 하루치 수집 -> DB 저장
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

from db import get_connection, init_db

# .env 파일에서 API 키를 읽어온다 (collect.py 와 동일한 방식)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# ── 기본 설정 ──────────────────────────────────────────────
ASOS_URL = "https://apis.data.go.kr/1360000/AsosHourlyInfoService/getWthrDataList"
SERVICE_KEY = (os.getenv("KMA_SERVICE_KEY") or "").strip()   # 기상청 키 (항공편과 별도)
KST = timezone(timedelta(hours=9))
SEOUL_STN = "108"       # 서울 지점 (김포 대체 — 김포엔 ASOS 지점 없음)


def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def yesterday():
    """어제 날짜를 YYYYMMDD로 반환.

    ASOS는 '전일(D-1)까지'만 제공한다 (관측값이 품질검증을 거쳐 하루 뒤 확정).
    그래서 오늘이 아니라 어제까지가 안전하다.
    """
    return (datetime.now(KST) - timedelta(days=1)).strftime("%Y%m%d")


def fetch_asos(start_dt, end_dt, stn=SEOUL_STN):
    """ASOS 시간자료 API를 호출해 관측 데이터(item 리스트)를 받아온다.

    start_dt, end_dt: "YYYYMMDD" 문자열 (예: "20260826")
    stn: 지점번호 (기본 108=서울)
    반환: 원본 item 리스트 (한 시간 = 한 item)
    """
    if not SERVICE_KEY:
        raise RuntimeError("KMA_SERVICE_KEY is missing")

    url = (
        f"{ASOS_URL}?serviceKey={SERVICE_KEY}"
        f"&numOfRows=800&pageNo=1&dataType=JSON"
        f"&dataCd=ASOS&dateCd=HR"
        f"&startDt={start_dt}&startHh=00"
        f"&endDt={end_dt}&endHh=23"
        f"&stnIds={stn}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

    # 재시도: 통신 에러가 나면 쉬었다가 다시 (최대 3번) — collect.py 와 동일 패턴
    text = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                text = r.read().decode("utf-8")
            break
        except urllib.error.URLError as e:
            print("[retry] attempt " + str(attempt) + " failed: " + str(e))
            if attempt == 3:
                raise
            time.sleep(attempt * 3)

    data = json.loads(text)

    # 응답 맨 앞에 결과 코드가 있다. "00"이 아니면 에러
    header = data["response"]["header"]
    if header["resultCode"] != "00":
        raise RuntimeError("ASOS API error: " + header["resultCode"] + " / " + header["resultMsg"])

    items = data["response"]["body"]["items"]["item"]
    print("[asos] fetched " + str(len(items)) + " rows (" + start_dt + "~" + end_dt + ", stn " + stn + ")")
    return items


def _to_float(v, default=None):
    """빈 문자열/None -> default, 아니면 float로."""
    if v in (None, ""):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v, default=None):
    """빈 문자열/None -> default, 아니면 int로 (소수점 오는 경우 대비해 float 경유)."""
    if v in (None, ""):
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def parse_rows(items, stn):
    """원본 item 리스트를 weather 테이블 컬럼 형태로 정리한다.
    핵심: rn(강수량)이 비 안오면 빈 문자열 ""로 옴 -> 0 으로 변환."""
    rows = []
    collected_at = now_kst()
    for item in items:
        tm = (item.get("tm") or "").replace("-", "").replace(":", "").replace(" ", "")
        rows.append({
            "stn_id": stn,
            "obs_time": tm,
            "temp": _to_float(item.get("ta")),
            "rain": _to_float(item.get("rn"), default=0.0),
            "wind_speed": _to_float(item.get("ws")),
            "humidity": _to_int(item.get("hm")),
            "cloud": _to_int(item.get("dc10Tca")),
            "visibility": _to_int(item.get("vs")),
            "collected_at": collected_at,
        })
    return rows


def save_many(rows):
    """여러 관측 행을 한 번에 저장한다 (INSERT OR IGNORE로 중복 방지)."""
    if not rows:
        print("[save_many] 저장할 행 없음")
        return

    conn = get_connection()
    cur = conn.cursor()
    cur.executemany("""
        INSERT OR IGNORE INTO weather
            (stn_id, obs_time, temp, rain, wind_speed, humidity, cloud, visibility, collected_at)
        VALUES
            (:stn_id, :obs_time, :temp, :rain, :wind_speed, :humidity, :cloud, :visibility, :collected_at)
    """, rows)
    conn.commit()
    conn.close()
    print("[save_many] " + str(len(rows)) + " rows 저장 시도 (중복 제외)")


def inspect():
    """어제 하루치를 받아서 몇 줄 왔는지, 원본이 어떻게 생겼는지 확인만."""
    print("[inspect] calling ASOS API...")
    y = yesterday()
    items = fetch_asos(y, y)
    print("[inspect] " + str(len(items)) + " rows 수신")
    if items:
        # 첫 줄 원본을 그대로 보여준다 (필드 이름 확인용)
        print("[inspect] 첫 행 원본:")
        print(json.dumps(items[0], ensure_ascii=False, indent=2))


def collect():
    """기본 실행: 어제 하루치를 수집해 저장한다."""
    init_db()
    y = yesterday()
    items = fetch_asos(y, y)
    # rows = parse_rows(items, SEOUL_STN)   # 3단계에서 활성화
    # save_many(rows)                        # 3단계에서 활성화
    print("[collect] " + str(len(items)) + " rows 수신 (저장은 3단계 구현 후)")


def backfill(start_dt, end_dt):
    """범위 백필: start_dt~end_dt 관측을 한 번에 받아 저장한다."""
    init_db()
    items = fetch_asos(start_dt, end_dt)
    rows = parse_rows(items, SEOUL_STN)   
    save_many(rows)                        
    print("[backfill] " + str(len(items)) + " rows 수신 -> 저장 시도)")


if __name__ == "__main__":
    if "--inspect" in sys.argv:
        inspect()
    elif "--backfill" in sys.argv:
        idx = sys.argv.index("--backfill")
        try:
            start_dt = sys.argv[idx + 1]
            end_dt = sys.argv[idx + 2]
        except IndexError:
            print("사용법: py weather.py --backfill 시작일(YYYYMMDD) 끝일(YYYYMMDD)")
            sys.exit(1)
        backfill(start_dt, end_dt)
    else:
        collect()
        
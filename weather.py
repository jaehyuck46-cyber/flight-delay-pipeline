"""
weather.py — 김포공항(GMP) 날씨 수집 파일

역할: 기상청 단기예보 API를 호출해서, '지금 시각의 김포 날씨'를 뽑아
      DB의 weather 테이블에 1행 저장한다.
      (collect.py 가 항공편을 담당하듯, 이 파일은 날씨를 담당)

사용법:
  py weather.py --inspect   # 날씨 출력만 (저장 안 함)
  py weather.py             # 날씨 수집 -> DB 저장
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
BASE_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
SERVICE_KEY = (os.getenv("KMA_SERVICE_KEY") or "").strip()   # 기상청 키 (항공편과 별도)
KST = timezone(timedelta(hours=9))
TARGET_AIRPORT = "GMP"

# 김포공항의 기상청 격자 좌표 (위경도가 아니라 기상청 전용 바둑판 좌표)
GIMPO_NX = 58
GIMPO_NY = 125

# 단기예보 발표 시각: 하루 8번, 정해진 시각에만 발표된다
BASE_TIMES = [2, 5, 8, 11, 14, 17, 20, 23]

# 우리가 뽑아 쓸 카테고리 코드 → weather 테이블 컬럼 이름 매핑
#   기상청은 값을 사람 말이 아니라 코드(TMP, PTY...)로 준다
WANTED = {
    "TMP": "temp",        # 기온(℃)
    "PTY": "rain_type",   # 강수형태 0없음/1비/2비눈/3눈/4소나기
    "SKY": "sky",         # 하늘상태 1맑음/3구름많음/4흐림
    "WSD": "wind_speed",  # 풍속(m/s)
    "REH": "humidity",    # 습도(%)
    "POP": "rain_prob",   # 강수확률(%)
}


def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def latest_base(now):
    """지금 시각 기준으로 '가장 최근 발표'의 날짜·시각을 계산한다.

    기상청은 아무 때나 예보를 안 주고 02,05,08,11,14,17,20,23시에만 발표한다.
    발표 직후엔 데이터가 없어서 10분 정도 여유를 두고 이전 발표를 쓴다.
    """
    hour, minute = now.hour, now.minute
    avail = [h for h in BASE_TIMES if (hour > h) or (hour == h and minute >= 10)]
    if avail:
        bh = max(avail)
        bdate = now.strftime("%Y%m%d")
    else:
        # 오늘 02시 발표조차 아직이면 -> 어제 23시 발표를 사용
        bh = 23
        bdate = (now - timedelta(days=1)).strftime("%Y%m%d")
    return bdate, f"{bh:02d}00"


def fetch_forecast():
    """기상청 API를 호출해 예보 항목(item) 목록을 통째로 받아온다."""
    if not SERVICE_KEY:
        raise RuntimeError("KMA_SERVICE_KEY is missing")

    now = datetime.now(KST)
    base_date, base_time = latest_base(now)

    # 요청 파라미터를 URL에 붙인다
    url = (
        f"{BASE_URL}?serviceKey={SERVICE_KEY}"
        f"&numOfRows=1000&pageNo=1&dataType=JSON"
        f"&base_date={base_date}&base_time={base_time}"
        f"&nx={GIMPO_NX}&ny={GIMPO_NY}"
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
        raise RuntimeError("KMA API error: " + header["resultCode"] + " / " + header["resultMsg"])

    items = data["response"]["body"]["items"]["item"]
    print("[weather] fetched " + str(len(items)) + " items (base " + base_date + " " + base_time + ")")
    return items, base_date, base_time


def pick_nearest(items):
    """받아온 예보 중 '가장 가까운 미래 시각' 한 묶음만 골라 한 줄로 정리한다.

    단기예보는 3일치가 다 오기 때문에, 그중 지금과 가장 가까운
    fcst 시각 하나만 골라서 우리가 원하는 항목만 뽑는다.
    """
    now = datetime.now(KST)

    # 각 item에는 fcstDate + fcstTime(예보 대상 시각)이 있다.
    # 지금 이후(또는 지금과 같은) 시각 중 가장 이른 것을 고른다.
    def to_dt(it):
        return datetime.strptime(it["fcstDate"] + it["fcstTime"], "%Y%m%d%H%M").replace(tzinfo=KST)

    future = [it for it in items if to_dt(it) >= now]
    target_items = future if future else items
    nearest_dt = min(to_dt(it) for it in target_items)

    # 그 시각의 항목들만 남긴다
    same = [it for it in target_items if to_dt(it) == nearest_dt]

    # WANTED에 있는 카테고리만 뽑아서 컬럼 이름으로 담는다
    row = {col: None for col in WANTED.values()}
    for it in same:
        cat = it["category"]
        if cat in WANTED:
            val = it["fcstValue"]
            # 숫자로 바꿀 수 있으면 숫자로 (기온·풍속은 소수, 나머지는 정수)
            try:
                row[WANTED[cat]] = float(val) if cat in ("TMP", "WSD") else int(float(val))
            except (ValueError, TypeError):
                row[WANTED[cat]] = None

    row["fcst_date"] = nearest_dt.strftime("%Y%m%d")
    row["fcst_time"] = nearest_dt.strftime("%H%M")
    return row


# 코드 → 사람이 읽는 말로 바꾸는 표 (출력용)
PTY_KOR = {0: "없음", 1: "비", 2: "비/눈", 3: "눈", 4: "소나기"}
SKY_KOR = {1: "맑음", 3: "구름많음", 4: "흐림"}


def inspect():
    print("[inspect] calling KMA API...")
    items, bd, bt = fetch_forecast()
    row = pick_nearest(items)
    print("[inspect] 김포 " + row["fcst_date"] + " " + row["fcst_time"] + " 예보")
    print("  기온: " + str(row["temp"]) + "℃")
    print("  강수: " + PTY_KOR.get(row["rain_type"], "?") +
          "  (강수확률 " + str(row["rain_prob"]) + "%)")
    print("  하늘: " + SKY_KOR.get(row["sky"], "?"))
    print("  풍속: " + str(row["wind_speed"]) + " m/s")
    print("  습도: " + str(row["humidity"]) + "%")


def save(row, base_date, base_time):
    conn = get_connection()
    cur = conn.cursor()
    now = now_kst()

    cur.execute("""
        INSERT INTO weather
            (airport, base_date, base_time, fcst_date, fcst_time,
             temp, rain_type, sky, wind_speed, humidity, rain_prob, collected_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (TARGET_AIRPORT, base_date, base_time, row["fcst_date"], row["fcst_time"],
          row["temp"], row["rain_type"], row["sky"], row["wind_speed"],
          row["humidity"], row["rain_prob"], now))

    conn.commit()
    conn.close()
    print("[save] weather saved @ " + now +
          "  (" + PTY_KOR.get(row["rain_type"], "?") +
          ", " + str(row["temp"]) + "℃, 풍속 " + str(row["wind_speed"]) + ")")


def collect():
    init_db()
    items, base_date, base_time = fetch_forecast()
    row = pick_nearest(items)
    save(row, base_date, base_time)


if __name__ == "__main__":
    if "--inspect" in sys.argv:
        inspect()
    else:
        collect()
        
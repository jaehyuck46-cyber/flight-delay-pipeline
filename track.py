"""
track.py — 김포공항(GMP) 상공 항공기 위치 수집 파일

역할: OpenSky Network API(ADS-B)를 호출해서, '지금 김포 주변 하늘'에 뜬
      항공기들의 위치를 받아 DB의 flight_tracks 테이블에 저장한다.
      (weather.py가 날씨를 담당하듯, 이 파일은 항공기 실시간 위치를 담당한다.)

      ※ ADS-B는 백필(과거 소급 수집)이 안 된다.
        "지금 이 순간"만 받아진다 → 30분마다 찍어서 궤적을 쌓아야 한다.

사용법:
  py track.py --inspect   # 김포 상공 항공기 화면 출력만 (DB 저장 안 함)
  py track.py             # 수집 → flight_tracks 테이블에 저장

인증(OAuth2):
  OpenSky는 2026-03 부터 OAuth2(client_credentials) 방식만 지원한다.
  아이디/비번을 매번 보내는 대신, '임시 출입증(access token)'을 한 장 받아
  그걸로 데이터를 조회하는 구조다.
  .env 에 아래 두 줄을 넣어둔다 (계정 Account 페이지에서 API 클라이언트 발급):
    OPENSKY_CLIENT_ID=...
    OPENSKY_CLIENT_SECRET=...
  (없으면 익명 모드로 동작 — 되긴 하지만 요청 제한이 빡세다.)
"""

import os
import sys
import json
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

from db import get_connection, init_db

# .env 에서 자격증명을 읽어온다 (collect.py / weather.py 와 동일한 방식)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# ── 기본 설정 ──────────────────────────────────────────────
STATES_URL = "https://opensky-network.org/api/states/all"
TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/"
             "opensky-network/protocol/openid-connect/token")
CLIENT_ID = (os.getenv("OPENSKY_CLIENT_ID") or "").strip()
CLIENT_SECRET = (os.getenv("OPENSKY_CLIENT_SECRET") or "").strip()
KST = timezone(timedelta(hours=9))

# 김포공항 주변 영역 (bounding box) — 위경도 '네모 상자'로 김포 근처만 필터.
#   states/all 은 이 네모 안에 든 항공기만 돌려준다.
#   김포공항(위도 37.558, 경도 126.791) 기준 대략 ±0.3도(약 30km).
GIMPO_LAT_MIN, GIMPO_LAT_MAX = 37.25, 37.85
GIMPO_LON_MIN, GIMPO_LON_MAX = 126.49, 127.09

# states/all 응답의 각 항공기는 '배열(리스트)'로 온다.
#   그 배열의 몇 번째 자리가 무슨 값인지를 OpenSky가 문서로 정해놨다.
#   (예: 0번=기체번호, 1번=편명, 6번=위도 …)
IDX = {
    "icao24": 0, "callsign": 1, "time_position": 3,
    "longitude": 5, "latitude": 6, "baro_altitude": 7,
    "on_ground": 8, "velocity": 9, "true_track": 10,
}


def now_kst():
    """지금 시각(한국시간)을 'YYYY-MM-DD HH:MM:SS' 문자열로."""
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def get_token():
    """OAuth2 client_credentials 로 액세스 토큰(임시 출입증)을 받아온다.

    자격증명이 없으면 None 을 돌려주고, 호출부에서 익명 모드로 넘어간다.
    """
    if not (CLIENT_ID and CLIENT_SECRET):
        return None  # 키 없음 → 익명 모드

    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode("utf-8")

    req = urllib.request.Request(
        TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            tok = json.loads(r.read().decode("utf-8"))
        return tok.get("access_token")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        print(f"⚠ 토큰 발급 실패 (HTTP {e.code}) — client_id/secret 확인 필요")
        print(f"  서버 응답: {body[:200]}")
        sys.exit(1)


def fetch_states():
    """김포 주변 영역의 현재 항공기 상태(state vector) 목록을 받아온다.

    - 토큰이 있으면 인증 모드(요청 제한 넉넉), 없으면 익명 모드.
    - 실패하면 최대 3회까지 다시 시도한다(재시도 로직).
    """
    token = get_token()
    mode = "인증" if token else "익명"

    params = urllib.parse.urlencode({
        "lamin": GIMPO_LAT_MIN, "lamax": GIMPO_LAT_MAX,
        "lomin": GIMPO_LON_MIN, "lomax": GIMPO_LON_MAX,
    })
    url = f"{STATES_URL}?{params}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    last_err = None
    for attempt in range(1, 4):  # 1, 2, 3회 시도
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = json.loads(r.read().decode("utf-8"))
            print(f"[track] OpenSky 응답 수신 ({mode} 모드, {attempt}회차)")
            return raw
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code == 429:
                print(f"  요청 제한(429) — 잠깐 쉬고 재시도… ({attempt}/3)")
            else:
                print(f"  HTTP 오류 {e.code} — 재시도… ({attempt}/3)")
        except urllib.error.URLError as e:
            last_err = str(e.reason)
            print(f"  네트워크 오류({e.reason}) — 재시도… ({attempt}/3)")
        time.sleep(2 * attempt)  # 갈수록 조금 더 쉬었다 재시도

    print(f"❌ OpenSky 호출 3회 모두 실패: {last_err}")
    sys.exit(1)


def parse_states(raw):
    """원본 응답(배열 덩어리)을 저장하기 좋은 딕셔너리 리스트로 정리한다.

    - captured_at: OpenSky가 준 '스냅샷 시각'을 한국시간으로 변환해 쓴다.
      한 번의 응답 안에서는 모든 항공기가 같은 captured_at → UNIQUE 제약이
      "한 기체당 한 스냅샷에 1행"을 보장한다.
    """
    states = raw.get("states") or []          # 없으면 빈 리스트
    snap_ts = raw.get("time")                 # 스냅샷 시각(유닉스 초)
    captured = (datetime.fromtimestamp(snap_ts, KST).strftime("%Y%m%d%H%M%S")
                if snap_ts else datetime.now(KST).strftime("%Y%m%d%H%M%S"))
    collected = now_kst()

    rows = []
    for s in states:
        rows.append({
            "icao24": s[IDX["icao24"]],
            "callsign": (s[IDX["callsign"]] or "").strip(),  # 앞뒤 공백 제거
            "captured_at": captured,
            "latitude": s[IDX["latitude"]],
            "longitude": s[IDX["longitude"]],
            "baro_altitude": s[IDX["baro_altitude"]],
            "velocity": s[IDX["velocity"]],
            "true_track": s[IDX["true_track"]],
            "on_ground": 1 if s[IDX["on_ground"]] else 0,
            "collected_at": collected,
        })
    return rows


def save_many(rows):
    """flight_tracks 테이블에 넣는다. 같은 기체·같은 시각은 자동 무시(중복 방지)."""
    if not rows:
        return 0
    conn = get_connection()
    cur = conn.cursor()
    cur.executemany("""
        INSERT OR IGNORE INTO flight_tracks
            (icao24, callsign, captured_at, latitude, longitude,
             baro_altitude, velocity, true_track, on_ground, collected_at)
        VALUES
            (:icao24, :callsign, :captured_at, :latitude, :longitude,
             :baro_altitude, :velocity, :true_track, :on_ground, :collected_at)
    """, rows)
    conn.commit()
    inserted = conn.total_changes   # 실제로 새로 들어간 행 수 (중복은 안 셈)
    conn.close()
    return inserted


def print_table(rows):
    """--inspect 용 — 받아온 항공기를 표로 화면에 뿌린다."""
    print(f"\n{'편명':<9}{'위도':>9}{'경도':>10}{'고도(m)':>9}{'속도(m/s)':>10}  지상")
    print("─" * 58)
    for r in rows:
        alt = f"{r['baro_altitude']:.0f}" if r['baro_altitude'] is not None else "-"
        spd = f"{r['velocity']:.0f}" if r['velocity'] is not None else "-"
        lat = f"{r['latitude']:.3f}" if r['latitude'] is not None else "-"
        lon = f"{r['longitude']:.3f}" if r['longitude'] is not None else "-"
        ground = "Y" if r["on_ground"] else ""
        name = r["callsign"] or r["icao24"]
        print(f"{name:<9}{lat:>9}{lon:>10}{alt:>9}{spd:>10}  {ground}")


# ── 이 파일을 직접 실행할 때만 아래가 돌아간다 ──────────────
if __name__ == "__main__":
    init_db()                            # flight_tracks 테이블 없으면 만들기
    inspect = "--inspect" in sys.argv    # --inspect 붙었나?

    raw = fetch_states()
    rows = parse_states(raw)

    if not rows:
        print("… 지금 김포 상공(네모 범위)에 잡힌 항공기가 없어.")
        print("  (새벽이거나 잠깐 비는 시간대면 0대일 수 있음 — 낮에 다시 돌려봐.)")
        sys.exit(0)

    if inspect:
        print(f"[track] 김포 상공 {len(rows)}대 (미리보기 — 저장 안 함)")
        print_table(rows)
    else:
        inserted = save_many(rows)
        print(f"[track] {len(rows)}대 수신 → {inserted}대 신규 저장 @ {now_kst()}")
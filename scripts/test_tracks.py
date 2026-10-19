"""
scripts/test_tracks.py — OpenSky /tracks/all 엔드포인트 시험용 (DB에 쓰지 않음)

/tracks/all 로 기체의 비행 궤적을 받아 김포 접근 궤적 분석에 쓸 만한지 숫자로 확인한다.
/tracks/all 은 OpenSky가 '실험적'이라고 밝힌 엔드포인트라 404·빈 응답이 날 수 있다.

  1차(--live)   : flight_tracks에서 최근 24시간 안에 떠 있던 기체 3대 → time=0(지금 비행 중인 궤적)
  2차(--history): 최근 7일 김포 도착편 중 콜사인이 flight_tracks와 맞는 3편
                  → time=그 편을 포착한 시각(과거 특정 비행의 궤적)

사용법:
  py scripts/test_tracks.py             # 1차 + 2차
  py scripts/test_tracks.py --live      # 1차만
  py scripts/test_tracks.py --history   # 2차만
"""

import json
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import track  # noqa: E402  OAuth2 토큰(get_token)과 김포 bounding box 상수를 재사용

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

TRACKS_URL = "https://opensky-network.org/api/tracks/all"
DB_PATH = ROOT / "data" / "flights.db"
SAMPLE_SIZE = 3

# /tracks/all 응답의 path 한 점은 [시각, 위도, 경도, 기압고도(m), 진행방향, 지상여부] 순서다.
P_TIME, P_LAT, P_LON, P_ALT = 0, 1, 2, 3

# ── 2차 테스트 설정 ─────────────────────────────────────────
# 항공편 번호(flights.flight_id)는 IATA 코드(KE1238), ADS-B 콜사인은 ICAO 코드(KAL1238)를 쓴다.
# 그래서 앞 두 글자를 ICAO 3글자로 바꿔야 flight_tracks.callsign과 맞춰볼 수 있다.
IATA_TO_ICAO = {
    "KE": "KAL",  # 대한항공
    "OZ": "AAR",  # 아시아나항공
    "7C": "JJA",  # 제주항공
    "LJ": "JNA",  # 진에어
    "TW": "TWB",  # 티웨이항공
    "RS": "ASV",  # 에어서울
    "BX": "ABL",  # 에어부산
    "ZE": "ESR",  # 이스타항공
    "WE": "PTA",  # 파라타항공 — 확인 필요 (신생 항공사라 ICAO 코드 미검증)
}
HISTORY_DAYS = 7          # 최근 며칠 안의 도착편에서 고를지
OPENSKY_MAX_DAYS = 30     # /tracks/all은 30일보다 오래된 궤적을 주지 않는다
BIG_DELAY_MIN = 60        # 가능하면 1편은 이만큼 이상 지연된 편으로
# 같은 콜사인은 매일 반복되므로, 그 편의 예상 도착 시각 근처에서 포착된 것만 '같은 비행'으로 본다.
MATCH_BEFORE = timedelta(hours=3)
MATCH_AFTER = timedelta(minutes=30)


def ro_conn():
    """DB를 읽기 전용(mode=ro)으로 연다. 실수로라도 쓰기를 못 하게."""
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def kst_str_to_unix(s):
    """'YYYYMMDDHHMMSS' 또는 'YYYYMMDDHHMM' 한국시간 문자열 → 유닉스 초(UTC 기준).

    OpenSky의 time은 UTC 기준 유닉스 초라서, 한국시간(+9)이라는 정보를 붙인 뒤 변환한다.
    시간대를 안 붙이면 컴퓨터의 지역 시간으로 해석돼 Actions(UTC)와 내 PC 결과가 달라진다.
    """
    fmt = "%Y%m%d%H%M%S" if len(s) == 14 else "%Y%m%d%H%M"
    return int(datetime.strptime(s, fmt).replace(tzinfo=track.KST).timestamp())


def to_icao_callsign(flight_id):
    """'KE1238' → 'KAL1238'. 매핑에 없는 항공사면 None. 편 번호 앞의 0은 콜사인에서 빠진다."""
    if not flight_id or len(flight_id) < 3:
        return None
    icao = IATA_TO_ICAO.get(flight_id[:2])
    number = flight_id[2:].lstrip("0")
    if icao is None or not number.isdigit():
        return None
    return icao + number


def fetch_track(icao24, token, t=0):
    """한 기체의 궤적을 요청한다. t=0이면 지금 비행 중, 아니면 그 시각을 포함하는 비행.

    반환: (HTTP 상태, 남은 크레딧 헤더, 본문 JSON 또는 오류 텍스트)
    """
    url = f"{TRACKS_URL}?icao24={icao24}&time={t}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            remaining = r.headers.get("X-Rate-Limit-Remaining")
            body = r.read().decode("utf-8")
            return r.status, remaining, (json.loads(body) if body.strip() else None)
    except urllib.error.HTTPError as e:
        with e:  # 오류 응답도 연결을 닫아야 ResourceWarning이 안 난다
            return e.code, e.headers.get("X-Rate-Limit-Remaining"), e.read().decode("utf-8", "ignore")[:200]


def in_gimpo_box(lat, lon):
    return (lat is not None and lon is not None
            and track.GIMPO_LAT_MIN <= lat <= track.GIMPO_LAT_MAX
            and track.GIMPO_LON_MIN <= lon <= track.GIMPO_LON_MAX)


def fmt_ts(ts):
    return datetime.fromtimestamp(ts, track.KST).strftime("%Y-%m-%d %H:%M:%S KST")


def report(data, expected_callsign=None):
    """궤적 응답 하나를 요약 출력한다."""
    got = str(data.get("callsign") or "").strip()
    if expected_callsign is None:
        print(f"    응답 콜사인           : {got or '-'}")
    else:
        mark = "✅ 일치" if got == expected_callsign else "❌ 불일치"
        print(f"    응답 콜사인           : {got or '-'} (기대 {expected_callsign}) {mark}")
    path = data.get("path") or []
    print(f"    경로 점 개수          : {len(path)}")
    if not path:
        print("    (path가 비어 있음)")
        return
    times = [p[P_TIME] for p in path]
    gaps = [b - a for a, b in zip(times, times[1:])]
    alts = [p[P_ALT] for p in path if p[P_ALT] is not None]
    print(f"    시간 범위             : {fmt_ts(times[0])} ~ {fmt_ts(times[-1])}")
    if gaps:
        print(f"    점 사이 간격          : 평균 {sum(gaps) / len(gaps):.0f}초, 최대 {max(gaps)}초")
    else:
        print("    점 사이 간격          : - (점 1개)")
    print(f"    김포 박스 안 점 개수  : {sum(in_gimpo_box(p[P_LAT], p[P_LON]) for p in path)}")
    print(f"    고도 0m 이하 점 개수  : {sum(1 for a in alts if a <= 0)}"
          f"   (최저 {min(alts):.0f}m)" if alts else "    고도 0m 이하 점 개수  : - (고도 없음)")


def print_response(status, remaining, body, expected_callsign=None):
    print(f"    HTTP {status}   남은 크레딧(X-Rate-Limit-Remaining): {remaining}")
    if status != 200:
        print(f"    응답 본문: {body!r}")
    elif body is None:
        print("    빈 응답 (본문 없음)")
    else:
        report(body, expected_callsign)
    print()


# ── 1차: 지금 비행 중인 궤적 (time=0) ───────────────────────
def pick_icao24s():
    """최근 24시간 안에 하늘에 떠 있던(on_ground=0) 기체 중 가장 최근에 잡힌 3대를 고른다."""
    cutoff = (datetime.now(track.KST) - timedelta(hours=24)).strftime("%Y%m%d%H%M%S")
    conn = ro_conn()
    rows = conn.execute("""
        SELECT icao24, MAX(callsign) AS callsign, MAX(captured_at) AS last_seen
        FROM flight_tracks
        WHERE captured_at >= ? AND on_ground = 0
        GROUP BY icao24
        ORDER BY last_seen DESC
        LIMIT ?
    """, (cutoff, SAMPLE_SIZE)).fetchall()
    conn.close()
    return rows


def test_live(token):
    print("==== 1차: time=0 (지금 비행 중인 궤적) ====\n")
    picks = pick_icao24s()
    if not picks:
        print("최근 24시간 안에 하늘에 떠 있던 기체가 flight_tracks에 없습니다.\n")
        return
    for r in picks:
        print(f"■ {r['icao24']} ({r['callsign'] or '-'}) — flight_tracks 마지막 포착 {r['last_seen']} KST")
        print_response(*fetch_track(r["icao24"], token, 0))


# ── 2차: 과거 특정 도착편의 궤적 (time=포착 시각) ──────────────
def pick_matched_flights():
    """최근 7일 김포 도착편 중, 콜사인이 flight_tracks와 맞고 '같은 비행'으로 볼 수 있는 편을 고른다.

    같은 비행 판정: 그 편의 예상 도착 시각 3시간 전 ~ 30분 후 사이에 포착된 기록만 인정한다.
    한 편에 여러 번 포착됐으면 도착 시각에 가장 가까운 것 하나를 쓴다.
    반환: 후보 리스트(최근 편 먼저). 각 항목은 dict.
    """
    now = datetime.now(track.KST).replace(tzinfo=None)
    since = (now - timedelta(days=HISTORY_DAYS)).strftime("%Y%m%d%H%M")
    oldest_ok = now - timedelta(days=OPENSKY_MAX_DAYS)

    conn = ro_conn()
    flights = conn.execute("""
        SELECT flight_id, airline, scheduled_dt, estimated_dt
        FROM flights
        WHERE status = '도착' AND airport = 'GMP'
          AND length(scheduled_dt) = 12 AND length(estimated_dt) = 12
          AND scheduled_dt >= ?
        ORDER BY estimated_dt DESC
    """, (since,)).fetchall()
    tracks = conn.execute("""
        SELECT icao24, callsign, captured_at FROM flight_tracks
        WHERE captured_at >= ? AND callsign != ''
    """, (since + "00",)).fetchall()
    conn.close()

    by_callsign = {}
    for t in tracks:
        by_callsign.setdefault(t["callsign"], []).append(t)

    candidates = []
    for f in flights:
        cs = to_icao_callsign(f["flight_id"])
        if cs is None or cs not in by_callsign:
            continue
        sched = datetime.strptime(f["scheduled_dt"], "%Y%m%d%H%M")
        est = datetime.strptime(f["estimated_dt"], "%Y%m%d%H%M")
        if sched < oldest_ok:
            continue  # OpenSky 조회 한도(30일) 밖
        near = [t for t in by_callsign[cs]
                if est - MATCH_BEFORE <= datetime.strptime(t["captured_at"], "%Y%m%d%H%M%S") <= est + MATCH_AFTER]
        if not near:
            continue
        best = min(near, key=lambda t: abs(datetime.strptime(t["captured_at"], "%Y%m%d%H%M%S") - est))
        candidates.append({
            "flight_id": f["flight_id"], "airline": f["airline"], "callsign": cs,
            "scheduled_dt": f["scheduled_dt"], "estimated_dt": f["estimated_dt"],
            "delay_min": (est - sched).total_seconds() / 60,
            "icao24": best["icao24"], "captured_at": best["captured_at"],
        })
    return candidates


def choose_three(candidates):
    """가능하면 60분+ 지연 1편을 넣고, 나머지는 최근 편 중 다른 항공사 위주로 채운다."""
    chosen = []
    big = [c for c in candidates if c["delay_min"] >= BIG_DELAY_MIN]
    if big:
        chosen.append(big[0])
    for c in candidates:
        if len(chosen) >= SAMPLE_SIZE:
            break
        if c in chosen or c["airline"] in {x["airline"] for x in chosen}:
            continue
        chosen.append(c)
    for c in candidates:  # 항공사가 모자라면 같은 항공사라도 채운다
        if len(chosen) >= SAMPLE_SIZE:
            break
        if c not in chosen:
            chosen.append(c)
    return chosen


def test_history(token):
    print(f"==== 2차: time=포착 시각 (최근 {HISTORY_DAYS}일 도착편) ====\n")
    candidates = pick_matched_flights()
    big_n = sum(1 for c in candidates if c["delay_min"] >= BIG_DELAY_MIN)
    print(f"콜사인·시각이 맞는 후보 {len(candidates)}편 (그중 {BIG_DELAY_MIN}분+ 지연 {big_n}편)\n")
    if not candidates:
        print("고를 편이 없습니다.\n")
        return
    for c in choose_three(candidates):
        t = kst_str_to_unix(c["captured_at"])
        print(f"■ {c['flight_id']} {c['airline']} → {c['callsign']} / icao24 {c['icao24']}")
        print(f"    계획 {c['scheduled_dt']} · 예상 {c['estimated_dt']} KST (지연 {c['delay_min']:+.0f}분)")
        print(f"    포착 {c['captured_at']} KST → time={t} (UTC {datetime.fromtimestamp(t, timezone.utc):%Y-%m-%d %H:%M:%S})")
        print_response(*fetch_track(c["icao24"], token, t), expected_callsign=c["callsign"])


def main():
    run_live = "--history" not in sys.argv
    run_history = "--live" not in sys.argv
    token = track.get_token()
    print(f"인증 모드: {'OAuth2 토큰' if token else '익명'}\n")
    if run_live:
        test_live(token)
    if run_history:
        test_history(token)


if __name__ == "__main__":
    main()

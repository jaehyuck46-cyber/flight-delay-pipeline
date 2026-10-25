"""
scripts/test_tracks.py — OpenSky /tracks/all 엔드포인트 시험용 (DB에 쓰지 않음)

/tracks/all 로 기체의 비행 궤적을 받아 김포 접근 궤적 분석에 쓸 만한지 숫자로 확인한다.
/tracks/all 은 OpenSky가 '실험적'이라고 밝힌 엔드포인트라 404·빈 응답이 날 수 있다.

  1차(--live)    : flight_tracks에서 최근 24시간 안에 떠 있던 기체 3대 → time=0(지금 비행 중인 궤적)
  2차(--history) : 최근 7일 김포 도착편 중 콜사인이 flight_tracks와 맞는 3편
                   → time=그 편을 포착한 시각(과거 특정 비행의 궤적)
  3차(--arrivals): /flights/arrival 로 어제 김포(RKSS) 도착편 목록을 받아 flights 테이블과 비교
  --snapshot     : 지금 김포 상공을 나는 기체 1대의 궤적(time=0)을 scripts/out/에 JSON으로 저장
                   (몇 시간 뒤 같은 비행을 과거 조회해서 점이 줄어드는지 비교하려는 준비)

사용법:
  py scripts/test_tracks.py              # 1차 + 2차
  py scripts/test_tracks.py --live       # 1차만 (옵션은 여러 개 같이 써도 된다)
  py scripts/test_tracks.py --history    # 2차만
  py scripts/test_tracks.py --arrivals   # 3차만
  py scripts/test_tracks.py --snapshot   # 스냅샷 저장만
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

# ── 3차·스냅샷 설정 ─────────────────────────────────────────
ARRIVAL_URL = "https://opensky-network.org/api/flights/arrival"
GIMPO_ICAO = "RKSS"
# 2차의 매핑을 뒤집은 것: 콜사인 'KAL1238' → 편명 'KE1238'
ICAO_TO_IATA = {icao: iata for iata, icao in IATA_TO_ICAO.items()}
SNAPSHOT_DIR = Path(__file__).resolve().parent / "out"


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


# ── 공통: 크레딧 소비량 재기 ────────────────────────────────
def get_json(url, token):
    """GET 요청 하나. 반환: (HTTP 상태, 남은 크레딧 헤더, 본문 JSON 또는 오류 텍스트)."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            remaining = r.headers.get("X-Rate-Limit-Remaining")
            body = r.read().decode("utf-8")
            return r.status, remaining, (json.loads(body) if body.strip() else None)
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.headers.get("X-Rate-Limit-Remaining"), e.read().decode("utf-8", "ignore")[:200]


def states_in_gimpo(token):
    """지금 김포 박스 안의 기체 목록(states/all). 남은 크레딧 기준값을 재는 데도 쓴다.

    작은 영역 조회라 크레딧이 가장 적게 드는 요청이다(소비량 측정의 기준점).
    """
    url = (f"{track.STATES_URL}?lamin={track.GIMPO_LAT_MIN}&lamax={track.GIMPO_LAT_MAX}"
           f"&lomin={track.GIMPO_LON_MIN}&lomax={track.GIMPO_LON_MAX}")
    return get_json(url, token)


# ── 3차: 공항 도착편 목록 (/flights/arrival) ─────────────────
def kst_day_bounds_unix(day):
    """한국시간 날짜(date) 하루의 [00:00, 24:00) 를 UTC 유닉스 초로. 예: 10/18 KST = 10/17 15:00 UTC부터."""
    start = datetime(day.year, day.month, day.day, tzinfo=track.KST)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def to_iata_flight_id(callsign):
    """'KAL1238' → 'KE1238'. 매핑에 없는 항공사면 None."""
    cs = (callsign or "").strip()
    iata = ICAO_TO_IATA.get(cs[:3])
    number = cs[3:]
    if iata is None or not number.isdigit():
        return None
    return iata + number


def arrived_flight_ids(day_str):
    """flights 테이블에서 그 날(KST, 계획 시각 기준) 김포 '도착' 편명 집합."""
    conn = ro_conn()
    rows = conn.execute("""
        SELECT DISTINCT flight_id FROM flights
        WHERE status = '도착' AND airport = 'GMP' AND substr(scheduled_dt, 1, 8) = ?
    """, (day_str,)).fetchall()
    conn.close()
    # 편 번호 앞 0 표기가 섞여도 맞춰지게 정규화 (7C0122 → 7C122)
    return {r["flight_id"][:2] + r["flight_id"][2:].lstrip("0") for r in rows if r["flight_id"]}


def try_arrivals(day, token):
    """하루치 도착편을 요청해 결과를 출력한다. 응답에 편이 있으면 True."""
    begin, end = kst_day_bounds_unix(day)
    day_str = day.strftime("%Y%m%d")
    print(f"■ {day:%Y-%m-%d} KST  (begin={begin}, end={end} / UTC "
          f"{datetime.fromtimestamp(begin, timezone.utc):%m-%d %H:%M} ~ "
          f"{datetime.fromtimestamp(end, timezone.utc):%m-%d %H:%M})")

    _, before, _ = states_in_gimpo(token)
    status, after, body = get_json(f"{ARRIVAL_URL}?airport={GIMPO_ICAO}&begin={begin}&end={end}", token)
    used = (int(before) - int(after)) if (before and after) else None
    print(f"    HTTP {status}   남은 크레딧 {before} → {after}"
          f"   (이 요청 소비 ≈ {used if used is not None else '측정 불가'})")

    if status != 200 or not isinstance(body, list) or not body:
        print(f"    응답 없음/비어 있음: {body!r}\n" if status != 200 else "    반환 편수 0\n")
        return False

    ours = arrived_flight_ids(day_str)
    mapped = {to_iata_flight_id(f.get("callsign")) for f in body} - {None}
    unmapped = sum(1 for f in body if to_iata_flight_id(f.get("callsign")) is None)
    print(f"    OpenSky 반환 편수                 : {len(body)}")
    print(f"    flights '도착' 편수 (같은 날)      : {len(ours)}")
    print(f"    콜사인 역변환 성공 (서로 다른 편명): {len(mapped)}   (매핑 밖 콜사인 {unmapped}건)")
    print(f"    flights와 일치한 편수             : {len(mapped & ours)}"
          f"   (일치율: 우리 도착편 기준 {len(mapped & ours) / len(ours) * 100:.0f}%)" if ours else "")
    print(f"    OpenSky에만 있음 / 우리에만 있음   : {len(mapped - ours)} / {len(ours - mapped)}")
    others = sorted({(f.get('callsign') or '').strip()[:3] for f in body
                     if to_iata_flight_id(f.get('callsign')) is None})
    print(f"    매핑 밖 콜사인 앞 3글자           : {', '.join(others) or '-'}\n")
    return True


def test_arrivals(token):
    print("==== 3차: /flights/arrival (어제 김포 도착편) ====\n")
    today = datetime.now(track.KST).date()
    # 도착 목록은 야간 배치로 갱신될 수 있어, 어제가 비면 그저께로 한 번 더 시도한다.
    if not try_arrivals(today - timedelta(days=1), token):
        print("  어제 응답이 비어 그저께로 다시 시도합니다.\n")
        try_arrivals(today - timedelta(days=2), token)


# ── 스냅샷: 지금 김포 상공 기체 1대의 궤적 저장 ────────────────
def take_snapshot(token):
    print("==== 스냅샷: 김포 박스 안 비행 중 기체 1대 → scripts/out/ ====\n")
    status, _, raw = states_in_gimpo(token)
    states = (raw.get("states") or []) if status == 200 and isinstance(raw, dict) else []
    airborne = [s for s in states if not s[track.IDX["on_ground"]]
                and s[track.IDX["baro_altitude"]] is not None]
    if not airborne:
        print(f"    지금 김포 박스 안에 비행 중인 기체가 없습니다 (states HTTP {status}).\n")
        return
    # 가장 낮게 나는 기체 = 이착륙 중일 가능성이 커서 김포 접근 궤적 비교에 알맞다
    s = min(airborne, key=lambda x: x[track.IDX["baro_altitude"]])
    icao24 = s[track.IDX["icao24"]]
    callsign = (s[track.IDX["callsign"]] or "").strip()
    status, remaining, body = fetch_track(icao24, token, 0)
    print(f"    대상 {icao24} ({callsign or '-'}) 고도 {s[track.IDX['baro_altitude']]:.0f}m"
          f"   tracks HTTP {status}, 남은 크레딧 {remaining}")
    if status != 200 or not body:
        print(f"    궤적을 받지 못해 저장하지 않습니다: {body!r}\n")
        return

    fetched = datetime.now(track.KST)
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    out = SNAPSHOT_DIR / f"track_{icao24}_{fetched:%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps({
        "fetched_at_kst": fetched.strftime("%Y-%m-%d %H:%M:%S"),
        "fetched_at_unix": int(fetched.timestamp()),
        "icao24": icao24,
        "callsign_from_states": callsign,
        "query": {"endpoint": TRACKS_URL, "icao24": icao24, "time": 0},
        "response": body,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    report(body)
    print(f"    저장: {out}\n")


def main():
    opts = {a for a in sys.argv[1:] if a.startswith("--")}
    if not opts:
        opts = {"--live", "--history"}  # 옵션 없이 실행하면 예전처럼 1차 + 2차
    token = track.get_token()
    print(f"인증 모드: {'OAuth2 토큰' if token else '익명'}\n")
    if "--live" in opts:
        test_live(token)
    if "--history" in opts:
        test_history(token)
    if "--arrivals" in opts:
        test_arrivals(token)
    if "--snapshot" in opts:
        take_snapshot(token)


if __name__ == "__main__":
    main()

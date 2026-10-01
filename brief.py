"""
brief.py — 파이프라인 상태 브리핑

수집 로그 · 항공편 · 날씨 · 항공기 위치 테이블의 현재 상태를 한 화면에 요약한다.
DB를 읽기만 하고 아무것도 쓰지 않는다.

사용법:
  py brief.py
"""

import sqlite3
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "flights.db"
STALE_HOURS = 5           # 마지막 수집이 이 시간 이상 지났으면 경고
LOW_VISIBILITY_M = 1000   # 시정이 이 값(m) 미만이면 경고
KST_OFFSET = timedelta(hours=9)

# 파이프로 출력될 때(예: 다른 프로그램이 결과를 받아갈 때) 윈도우 기본 인코딩(cp949)은
# ⚠ 같은 기호를 못 찍고 에러가 난다. 그래서 출력 인코딩을 UTF-8로 고정한다.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def utcnow():
    """datetime.utcnow()와 같은 값(시간대 정보 없는 UTC 시각).
    utcnow()는 Python 3.12부터 사용 중단 경고가 떠서 이 방식으로 대체한다."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── 출력 정렬 도우미 ───────────────────────────────────────
# 한글은 터미널에서 두 칸을 차지해서, 글자 수 기준으로 맞추면 줄이 어긋난다.
# 그래서 '화면에 보이는 폭' 기준으로 칸을 채운다.
def _width(s):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _ljust(s, width):
    s = str(s)
    return s + " " * max(width - _width(s), 0)


def _rjust(s, width):
    s = str(s)
    return " " * max(width - _width(s), 0) + s


def row(label, value):
    print("  " + _ljust(label, 30) + _rjust(value, 20))


def section(title):
    print()
    print("■ " + title)
    print("  " + "-" * 50)


def fmt_elapsed(delta):
    total_min = int(delta.total_seconds() // 60)
    if total_min < 0:
        return "방금 전"
    days, rem = divmod(total_min, 1440)
    hours, minutes = divmod(rem, 60)
    if days:
        return f"{days}일 {hours}시간 {minutes}분 전"
    if hours:
        return f"{hours}시간 {minutes}분 전"
    return f"{minutes}분 전"


def fmt_dt12(s):
    """'YYYYMMDDHHMM' 또는 'YYYYMMDDHHMMSS' -> 'YYYY-MM-DD HH:MM(:SS)'."""
    if not s or not s.isdigit() or len(s) not in (12, 14):
        return str(s)
    out = f"{s[0:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}"
    if len(s) == 14:
        out += ":" + s[12:14]
    return out


def table_exists(conn, name):
    r = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return r is not None


# ── 1. 수집 로그 ───────────────────────────────────────────
def brief_collection_log(conn):
    section("수집 로그 (collection_log)")
    r = conn.execute("""
        SELECT MAX(collected_at)              AS last_at,
               COUNT(*)                       AS total,
               COALESCE(SUM(success), 0)      AS ok,
               COALESCE(SUM(1 - success), 0)  AS fail
        FROM collection_log
    """).fetchone()
    if r["total"] == 0 or r["last_at"] is None:
        print("  ⚠ 데이터 없음")
        return

    # collected_at은 수집 스크립트가 한국시간(KST)으로 기록한다.
    # 비교 기준은 UTC라서, 9시간을 빼서 UTC로 맞춘 뒤 경과 시간을 계산한다.
    last_kst = datetime.strptime(r["last_at"], "%Y-%m-%d %H:%M:%S")
    elapsed = utcnow() - (last_kst - KST_OFFSET)

    row("마지막 수집 (KST)", r["last_at"])
    row("경과 시간", fmt_elapsed(elapsed))
    row("총 실행 횟수", f"{r['total']:,}회")
    row("성공", f"{r['ok']:,}회")
    row("실패", f"{r['fail']:,}회")
    if elapsed >= timedelta(hours=STALE_HOURS):
        print(f"  ⚠ 경고: 마지막 수집 후 {STALE_HOURS}시간 이상 지났습니다. 수집이 멈췄는지 확인하세요.")


# ── 2. 항공편 ──────────────────────────────────────────────
def brief_flights(conn):
    section("항공편 (flights)")
    total = conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0]
    if total == 0:
        print("  ⚠ 데이터 없음")
        return

    today = utcnow().strftime("%Y%m%d")
    today_cnt = conn.execute(
        "SELECT COUNT(*) FROM flights WHERE substr(scheduled_dt,1,8) = ?", (today,)
    ).fetchone()[0]

    row("전체 편수", f"{total:,}편")
    row(f"오늘 편수 ({today}, UTC 기준)", f"{today_cnt:,}편")

    print("  [상태별]")
    for r in conn.execute("""
        SELECT COALESCE(NULLIF(status, ''), '(빈값)') AS st, COUNT(*) AS n
        FROM flights
        GROUP BY st
        ORDER BY n DESC
    """):
        row("    " + r["st"], f"{r['n']:,}편")


# ── 3. 날씨 ────────────────────────────────────────────────
def brief_weather(conn):
    section("날씨 최신 관측 (weather)")
    r = conn.execute("""
        SELECT stn_id, obs_time, temp, rain, wind_speed, humidity, cloud, visibility
        FROM weather
        ORDER BY obs_time DESC
        LIMIT 1
    """).fetchone()
    if r is None:
        print("  ⚠ 데이터 없음")
        return

    def val(v, unit):
        return "-" if v is None else f"{v}{unit}"

    row("관측 시각 (KST)", fmt_dt12(r["obs_time"]))
    row("지점", r["stn_id"])
    row("기온", val(r["temp"], "℃"))
    row("강수량", val(r["rain"], "mm"))
    row("풍속", val(r["wind_speed"], "m/s"))
    row("습도", val(r["humidity"], "%"))
    row("전운량 (0~10)", val(r["cloud"], ""))
    row("시정", "-" if r["visibility"] is None else f"{r['visibility']:,}m")
    if r["visibility"] is not None and r["visibility"] < LOW_VISIBILITY_M:
        print(f"  ⚠ 경고: 시정 {r['visibility']}m — {LOW_VISIBILITY_M}m 미만으로 지연 가능성이 높습니다.")


# ── 4. 항공기 위치 ─────────────────────────────────────────
def brief_tracks(conn):
    # 위치 수집은 나중에 추가된 기능이라, 테이블이 없는 DB면 조용히 넘어간다.
    if not table_exists(conn, "flight_tracks"):
        return
    section("항공기 위치 (flight_tracks)")
    r = conn.execute("""
        SELECT COUNT(*)               AS n,
               COUNT(DISTINCT icao24) AS planes,
               MAX(captured_at)       AS last_at
        FROM flight_tracks
    """).fetchone()
    if r["n"] == 0:
        print("  ⚠ 데이터 없음")
        return
    row("누적 행수", f"{r['n']:,}행")
    row("고유 기체 수 (icao24)", f"{r['planes']:,}대")
    row("마지막 스냅샷", fmt_dt12(r["last_at"]))


def main():
    # DB가 없을 때 sqlite3.connect를 하면 빈 파일이 새로 생겨버리므로 먼저 확인한다.
    if not DB_PATH.exists():
        print(f"❌ DB 파일이 없습니다: {DB_PATH}")
        sys.exit(1)

    print("=" * 50)
    print(f"  파이프라인 상태 브리핑  (UTC {utcnow():%Y-%m-%d %H:%M})")
    print("=" * 50)

    conn = get_conn()
    try:
        # 한 섹션에서 에러가 나도 나머지 섹션은 계속 보여주기 위해 하나씩 감싼다.
        for fn in (brief_collection_log, brief_flights, brief_weather, brief_tracks):
            try:
                fn(conn)
            except Exception as e:
                print(f"  ❌ 오류 ({fn.__name__}): {e}")
    finally:
        conn.close()
    print()


if __name__ == "__main__":
    main()

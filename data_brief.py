"""
data_brief.py — 데이터 분석 브리핑

쌓인 항공편·날씨 데이터로 커버리지, 지연 요약, 항공사별/시간대별 지연율,
날씨 × 지연 결합 분석, 최근 이상치(2시간+ 지연)를 한 화면에 요약한다.
DB를 읽기만 하고 아무것도 쓰지 않는다.

사용법:
  py data_brief.py
"""

import sqlite3
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "flights.db"
MIN_AIRLINE_SAMPLE = 30   # 항공사별 지연율은 이 편수 이상인 항공사만 (표본이 적으면 비율이 튐)
OUTLIER_MIN = 120         # 이 분 이상 지연이면 이상치
OUTLIER_DAYS = 7
WEATHER_STN = "108"       # 서울 ASOS (김포 대체 지점)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _to_ts(col):
    """'YYYYMMDDHHMM' 컬럼을 julianday가 읽을 수 있는 'YYYY-MM-DD HH:MM'로 바꾸는 SQL 조각."""
    return (f"substr({col},1,4)||'-'||substr({col},5,2)||'-'||substr({col},7,2)||' '||"
            f"substr({col},9,2)||':'||substr({col},11,2)")


# 지연(분) = (예상 시각 - 계획 시각) × 1440.
# julianday 차이는 소수라서 1분 차이가 0.9999… 처럼 나올 수 있다.
# 그러면 '120분 이상' 같은 비교가 경계에서 빠지므로 ROUND로 정수 분으로 맞춘다.
DELAY_EXPR = f"ROUND((julianday({_to_ts('estimated_dt')}) - julianday({_to_ts('scheduled_dt')})) * 1440)"

# 지연 분석 대상: 실제로 도착했고, 두 시각이 모두 12자리로 온전한 편만.
BASE_WHERE = ("status = '도착' AND estimated_dt IS NOT NULL "
              "AND length(scheduled_dt) = 12 AND length(estimated_dt) = 12")


def utcnow():
    """datetime.utcnow()와 같은 값(시간대 정보 없는 UTC 시각).
    utcnow()는 Python 3.12부터 사용 중단 경고가 떠서 이 방식으로 대체한다."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── 출력 정렬 도우미 (한글은 화면에서 두 칸이라 '보이는 폭' 기준으로 맞춘다) ──
def _width(s):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _ljust(s, width):
    s = str(s)
    return s + " " * max(width - _width(s), 0)


def _rjust(s, width):
    s = str(s)
    return " " * max(width - _width(s), 0) + s


def row(label, value):
    print("  " + _ljust(label, 30) + _rjust(value, 24))


def section(title):
    print()
    print("■ " + title)
    print("  " + "-" * 54)


def fmt_dt12(s):
    if not s or len(s) != 12:
        return str(s)
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}"


def fmt_ymd(s):
    if not s or len(s) != 8:
        return str(s)
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def stat_line(label, n, avg, rate):
    """그룹 하나(편수·평균지연·지연율)를 한 줄로."""
    print("  " + _ljust(label, 30)
          + _rjust(f"{n:,}편", 9)
          + _rjust(f"평균 {avg:+.1f}분", 15)
          + _rjust(f"지연율 {rate:.1f}%", 15))


# ── 1. 커버리지 ────────────────────────────────────────────
def brief_coverage(conn):
    section("1. 데이터 커버리지")
    f = conn.execute("""
        SELECT MIN(substr(scheduled_dt,1,8)) AS d0, MAX(substr(scheduled_dt,1,8)) AS d1, COUNT(*) AS n
        FROM flights
    """).fetchone()
    w = conn.execute("""
        SELECT MIN(substr(obs_time,1,8)) AS d0, MAX(substr(obs_time,1,8)) AS d1, COUNT(*) AS n
        FROM weather
    """).fetchone()

    if f["n"] == 0:
        row("flights", "⚠ 데이터 부족")
    else:
        row("flights 기간", f"{fmt_ymd(f['d0'])} ~ {fmt_ymd(f['d1'])}")
        row("flights 행수", f"{f['n']:,}편")
    if w["n"] == 0:
        row("weather", "⚠ 데이터 부족")
    else:
        row("weather 기간", f"{fmt_ymd(w['d0'])} ~ {fmt_ymd(w['d1'])}")
        row("weather 행수", f"{w['n']:,}행")


# ── 2. 지연 요약 ───────────────────────────────────────────
def brief_delay_summary(conn):
    section("2. 지연 요약 (도착편 기준)")
    r = conn.execute(f"""
        WITH base AS (
            SELECT {DELAY_EXPR} AS delay_min FROM flights WHERE {BASE_WHERE}
        )
        SELECT COUNT(*)            AS n,
               AVG(delay_min)      AS avg_delay,
               SUM(delay_min > 0)  AS delayed,
               MAX(delay_min)      AS max_delay
        FROM base
    """).fetchone()
    if r["n"] == 0:
        print("  ⚠ 데이터 부족")
        return
    rate = 100.0 * r["delayed"] / r["n"]
    row("분석 대상 편수", f"{r['n']:,}편")
    row("평균 지연", f"{r['avg_delay']:+.1f}분")
    row("지연 편수 (>0분)", f"{r['delayed']:,}편")
    row("지연율", f"{rate:.1f}%")
    row("최대 지연", f"{r['max_delay']:.0f}분")
    print("  ※ 평균이 음수면 계획보다 일찍 도착한 편이 더 많다는 뜻")


# ── 3. 항공사별 지연율 TOP 5 ──────────────────────────────
def brief_airline(conn):
    section(f"3. 항공사별 지연율 TOP 5 (표본 ≥ {MIN_AIRLINE_SAMPLE}편)")
    rows = conn.execute(f"""
        WITH base AS (
            SELECT airline, {DELAY_EXPR} AS delay_min FROM flights WHERE {BASE_WHERE}
        )
        SELECT airline,
               COUNT(*)                                AS n,
               AVG(delay_min)                          AS avg_delay,
               100.0 * SUM(delay_min > 0) / COUNT(*)   AS delay_rate
        FROM base
        GROUP BY airline
        HAVING COUNT(*) >= {MIN_AIRLINE_SAMPLE}
        ORDER BY delay_rate DESC
        LIMIT 5
    """).fetchall()
    if not rows:
        print("  ⚠ 데이터 부족")
        return
    for r in rows:
        bar = "█" * int(r["delay_rate"] / 5)
        print("  " + _ljust(r["airline"] or "(미상)", 14)
              + _rjust(f"{r['n']:,}편", 8)
              + _rjust(f"{r['delay_rate']:.1f}%", 8) + "  " + bar)


# ── 4. 시간대별 지연율 ─────────────────────────────────────
def brief_hourly(conn):
    section("4. 시간대별 지연율 (계획 시각 기준, KST)")
    rows = conn.execute(f"""
        WITH base AS (
            SELECT substr(scheduled_dt, 9, 2) AS hour, {DELAY_EXPR} AS delay_min
            FROM flights WHERE {BASE_WHERE}
        )
        SELECT hour,
               COUNT(*)                                AS n,
               100.0 * SUM(delay_min > 0) / COUNT(*)   AS delay_rate
        FROM base
        GROUP BY hour
        ORDER BY hour
    """).fetchall()
    if not rows:
        print("  ⚠ 데이터 부족")
        return
    for r in rows:
        bar = "█" * int(r["delay_rate"] / 2.5)
        print(f"  {r['hour']}시" + _rjust(f"{r['n']:,}편", 9)
              + _rjust(f"{r['delay_rate']:.1f}%", 8) + "  " + bar)


# ── 5. 날씨 × 지연 ─────────────────────────────────────────
def brief_weather_join(conn):
    section("5. 날씨 × 지연 결합 분석")
    # 계획 시각의 '시(YYYYMMDDHH)'와 같은 시각의 관측을 붙인다.
    # LEFT JOIN이라 관측이 없는 편도 남고, 그 편은 obs_time이 NULL이 된다.
    joined_cte = f"""
        WITH base AS (
            SELECT substr(scheduled_dt, 1, 10) AS hh, {DELAY_EXPR} AS delay_min
            FROM flights WHERE {BASE_WHERE}
        ),
        joined AS (
            SELECT b.delay_min, w.obs_time, w.rain, w.visibility
            FROM base b
            LEFT JOIN weather w
              ON w.stn_id = '{WEATHER_STN}' AND substr(w.obs_time, 1, 10) = b.hh
        )
    """

    m = conn.execute(joined_cte + """
        SELECT COUNT(*) AS total, COUNT(obs_time) AS matched FROM joined
    """).fetchone()
    if m["total"] == 0:
        print("  ⚠ 데이터 부족")
        return
    match_rate = 100.0 * m["matched"] / m["total"]
    row("날씨 매칭률 (도착편 기준)", f"{m['matched']:,} / {m['total']:,}편 ({match_rate:.1f}%)")
    if m["matched"] == 0:
        print("  ⚠ 데이터 부족 — 날씨와 겹치는 기간의 항공편이 없습니다")
        return

    print("  [비 유무별]")
    rain_rows = {r["grp"]: r for r in conn.execute(joined_cte + """
        SELECT CASE WHEN rain > 0 THEN 'rain' ELSE 'dry' END AS grp,
               COUNT(*)                                AS n,
               AVG(delay_min)                          AS avg_delay,
               100.0 * SUM(delay_min > 0) / COUNT(*)   AS delay_rate
        FROM joined
        WHERE obs_time IS NOT NULL AND rain IS NOT NULL
        GROUP BY grp
    """)}
    for key, label in (("rain", "    비 옴 (rain > 0)"), ("dry", "    비 안 옴 (rain = 0)")):
        r = rain_rows.get(key)
        if r is None:
            print("  " + _ljust(label, 30) + "⚠ 데이터 부족")
        else:
            stat_line(label, r["n"], r["avg_delay"], r["delay_rate"])

    print("  [시정 구간별]")
    vis_rows = {r["bucket"]: r for r in conn.execute(joined_cte + """
        SELECT CASE
                 WHEN visibility < 500  THEN 1
                 WHEN visibility < 1000 THEN 2
                 WHEN visibility < 5000 THEN 3
                 ELSE 4
               END                                     AS bucket,
               COUNT(*)                                AS n,
               AVG(delay_min)                          AS avg_delay,
               100.0 * SUM(delay_min > 0) / COUNT(*)   AS delay_rate
        FROM joined
        WHERE obs_time IS NOT NULL AND visibility IS NOT NULL
        GROUP BY bucket
    """)}
    labels = {
        1: "    ① <500m (매우 나쁨)",
        2: "    ② 500~999m (나쁨)",
        3: "    ③ 1000~4999m (보통)",
        4: "    ④ ≥5000m (좋음)",
    }
    for b, label in labels.items():
        r = vis_rows.get(b)
        if r is None:
            print("  " + _ljust(label, 30) + "⚠ 데이터 부족")
        else:
            stat_line(label, r["n"], r["avg_delay"], r["delay_rate"])


# ── 6. 이상치 ──────────────────────────────────────────────
def brief_outliers(conn):
    section(f"6. 이상치 — 최근 {OUTLIER_DAYS}일 {OUTLIER_MIN}분+ 지연")
    cutoff = (utcnow() - timedelta(days=OUTLIER_DAYS)).strftime("%Y%m%d")
    rows = conn.execute(f"""
        WITH base AS (
            SELECT flight_id, airline, scheduled_dt, {DELAY_EXPR} AS delay_min
            FROM flights
            WHERE {BASE_WHERE} AND substr(scheduled_dt, 1, 8) >= ?
        )
        SELECT * FROM base
        WHERE delay_min >= {OUTLIER_MIN}
        ORDER BY delay_min DESC
        LIMIT 10
    """, (cutoff,)).fetchall()
    if not rows:
        print("  ✅ 최근 7일 2시간+ 지연 없음")
        return
    for r in rows:
        print("  " + _ljust(r["flight_id"] or "-", 10)
              + _ljust(r["airline"] or "-", 14)
              + _ljust(fmt_dt12(r["scheduled_dt"]), 18)
              + _rjust(f"+{r['delay_min']:.0f}분", 8))


def main():
    # DB가 없을 때 sqlite3.connect를 하면 빈 파일이 새로 생겨버리므로 먼저 확인한다.
    if not DB_PATH.exists():
        print(f"❌ DB 파일이 없습니다: {DB_PATH}")
        sys.exit(1)

    print("=" * 50)
    print(f"  데이터 분석 브리핑  (UTC {utcnow():%Y-%m-%d %H:%M})")
    print("=" * 50)

    conn = get_conn()
    try:
        # 한 섹션에서 에러가 나도 나머지 섹션은 계속 보여주기 위해 하나씩 감싼다.
        for fn in (brief_coverage, brief_delay_summary, brief_airline,
                   brief_hourly, brief_weather_join, brief_outliers):
            try:
                fn(conn)
            except Exception as e:
                print(f"  ❌ 오류 ({fn.__name__}): {e}")
    finally:
        conn.close()
    print()


if __name__ == "__main__":
    main()

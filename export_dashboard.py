"""
export_dashboard.py — 웹 대시보드용 JSON 내보내기

data/flights.db를 읽어 docs/data/dashboard.json(schema_version 1)을 만든다.
지연 계산·유령 행·시정 단위·날씨 매칭·p-값은 data_brief.py / brief.py / weather_delay.py의
정의를 그대로 가져다 쓴다. DB를 읽기만 하고 아무것도 쓰지 않는다.

사용법:
  py export_dashboard.py
"""

import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

from brief import KST_OFFSET, VIS_UNIT_M
from data_brief import (BASE_WHERE, D15_MIN, DB_PATH, DELAY_EXPR, MIN_AIRLINE_SAMPLE,
                        WEATHER_STN, get_conn, utcnow)
from weather_delay import (DELAY_THRESHOLD_MIN, build_dataset, chi2_2x2_pvalue,
                           load_arrived_flights, load_weather_by_obs_time, nearest_hour,
                           parse_dt, split_by, summarize)

OUT_PATH = Path(__file__).parent / "docs" / "data" / "dashboard.json"
SCHEMA_VERSION = 1
TREND_DAYS = 30

FLIGHTS_WARN_MIN = 6 * 60
FLIGHTS_STALE_MIN = 24 * 60
# ASOS는 D-1 자료까지만 올라오므로 날씨는 기준을 넉넉하게 잡는다.
WEATHER_WARN_MIN = 36 * 60
WEATHER_STALE_MIN = 72 * 60

# summary 등은 data_brief.D15_MIN, weather_effect는 weather_delay.DELAY_THRESHOLD_MIN을 쓴다.
# meta에는 한 값만 적으므로 둘이 어긋나면 바로 멈춘다.
assert D15_MIN == DELAY_THRESHOLD_MIN, "data_brief.D15_MIN과 weather_delay.DELAY_THRESHOLD_MIN이 다릅니다"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def now_kst():
    return utcnow() + KST_OFFSET


def to_kst_iso(kst_dt):
    return None if kst_dt is None else kst_dt.strftime("%Y-%m-%dT%H:%M:%S") + "+09:00"


def parse_dt12(s):
    try:
        return datetime.strptime(s, "%Y%m%d%H%M")
    except (TypeError, ValueError):
        return None


def round1(x):
    return None if x is None else round(x, 1)


def round_sig(x, sig=2):
    if x is None:
        return None
    if x == 0:
        return 0.0
    return round(x, sig - 1 - math.floor(math.log10(abs(x))))


def status_of(elapsed_min, warn_min, stale_min):
    if elapsed_min is None or elapsed_min > stale_min:
        return "stale"
    if elapsed_min > warn_min:
        return "warn"
    return "ok"


def elapsed_since(kst_dt):
    return None if kst_dt is None else int((now_kst() - kst_dt).total_seconds() // 60)


def delay_row_stats(r):
    """{n, delayed, avg_delay} 행을 arrived/delayed/delay_rate_pct/avg_delay_min으로 바꾼다."""
    n = r["n"] or 0
    return {
        "arrived": n,
        "delayed": r["delayed"] or 0,
        "delay_rate_pct": round1(100.0 * r["delayed"] / n) if n else None,
        "avg_delay_min": round1(r["avg_delay"]) if n else None,
    }


DELAY_AGG = f"""COUNT(*) AS n,
               SUM(delay_min > {D15_MIN}) AS delayed,
               AVG(delay_min) AS avg_delay"""


def build_meta():
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": to_kst_iso(now_kst().replace(microsecond=0)),
        "delay_threshold_min": D15_MIN,
        "min_airline_flights": MIN_AIRLINE_SAMPLE,
        "ghost_rows_excluded": True,
        "weather_station": f"서울({WEATHER_STN})",
    }


def build_health_flights(conn):
    last_ok = conn.execute(
        "SELECT MAX(collected_at) FROM collection_log WHERE success = 1"
    ).fetchone()[0]
    last_kst = datetime.fromisoformat(last_ok) if last_ok else None
    cutoff = (now_kst() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    failures = conn.execute(
        "SELECT COUNT(*) FROM collection_log WHERE success = 0 AND collected_at >= ?", (cutoff,)
    ).fetchone()[0]
    elapsed = elapsed_since(last_kst)
    return {
        "last_success": to_kst_iso(last_kst),
        "elapsed_min": elapsed,
        "failures_24h": failures,
        "status": status_of(elapsed, FLIGHTS_WARN_MIN, FLIGHTS_STALE_MIN),
    }


def build_health_weather(conn):
    last = conn.execute(
        "SELECT MAX(obs_time) FROM weather WHERE stn_id = ?", (WEATHER_STN,)
    ).fetchone()[0]
    last_kst = parse_dt12(last)
    elapsed = elapsed_since(last_kst)
    return {
        "last_obs": to_kst_iso(last_kst),
        "elapsed_min": elapsed,
        "status": status_of(elapsed, WEATHER_WARN_MIN, WEATHER_STALE_MIN),
    }


def build_summary(conn):
    today = now_kst().strftime("%Y%m%d")
    t = conn.execute(f"""
        WITH base AS (
            SELECT {DELAY_EXPR} AS delay_min FROM flights
            WHERE {BASE_WHERE} AND substr(scheduled_dt, 1, 8) = ?
        )
        SELECT {DELAY_AGG} FROM base
    """, (today,)).fetchone()
    c = conn.execute(f"""
        WITH base AS (
            SELECT {DELAY_EXPR} AS delay_min, scheduled_dt FROM flights WHERE {BASE_WHERE}
        )
        SELECT {DELAY_AGG}, MIN(substr(scheduled_dt, 1, 8)) AS since FROM base
    """).fetchone()
    cumulative = delay_row_stats(c)
    since = c["since"]
    cumulative["since"] = f"{since[0:4]}-{since[4:6]}-{since[6:8]}" if since else None
    return {"today": delay_row_stats(t), "cumulative": cumulative}


def build_latest_weather(conn):
    r = conn.execute("""
        SELECT obs_time, temp, rain, wind_speed, humidity, cloud, visibility
        FROM weather WHERE stn_id = ?
        ORDER BY obs_time DESC LIMIT 1
    """, (WEATHER_STN,)).fetchone()
    if r is None:
        return {k: None for k in ("obs_time", "temp_c", "rain_mm", "wind_ms",
                                  "humidity_pct", "cloud_0_10", "visibility_m")}
    return {
        "obs_time": to_kst_iso(parse_dt12(r["obs_time"])),
        "temp_c": r["temp"],
        "rain_mm": r["rain"],
        "wind_ms": r["wind_speed"],
        "humidity_pct": r["humidity"],
        "cloud_0_10": r["cloud"],
        "visibility_m": None if r["visibility"] is None else r["visibility"] * VIS_UNIT_M,
    }


def build_daily_trend(conn):
    rows = conn.execute(f"""
        WITH base AS (
            SELECT substr(scheduled_dt, 1, 8) AS d, {DELAY_EXPR} AS delay_min
            FROM flights WHERE {BASE_WHERE}
        ),
        daily AS (
            SELECT d, {DELAY_AGG} FROM base GROUP BY d ORDER BY d DESC LIMIT {TREND_DAYS}
        ),
        rain AS (
            SELECT substr(obs_time, 1, 8) AS d, SUM(rain) AS rain_mm
            FROM weather WHERE stn_id = ? GROUP BY d
        )
        SELECT daily.*, rain.rain_mm
        FROM daily LEFT JOIN rain ON rain.d = daily.d
        ORDER BY daily.d
    """, (WEATHER_STN,)).fetchall()
    out = []
    for r in rows:
        s = delay_row_stats(r)
        out.append({
            "date": f"{r['d'][0:4]}-{r['d'][4:6]}-{r['d'][6:8]}",
            "arrived": s["arrived"],
            "delay_rate_pct": s["delay_rate_pct"],
            "rain_mm": round1(r["rain_mm"]),
        })
    return out


def build_by_airline(conn):
    rows = conn.execute(f"""
        WITH base AS (
            SELECT airline, {DELAY_EXPR} AS delay_min FROM flights WHERE {BASE_WHERE}
        )
        SELECT airline, {DELAY_AGG}
        FROM base
        GROUP BY airline
        HAVING COUNT(*) >= {MIN_AIRLINE_SAMPLE}
        ORDER BY 1.0 * delayed / n DESC
    """).fetchall()
    return [{"name": r["airline"],
             "arrived": r["n"],
             "delay_rate_pct": round1(100.0 * r["delayed"] / r["n"])} for r in rows]


def build_by_hour(conn):
    rows = conn.execute(f"""
        WITH base AS (
            SELECT substr(scheduled_dt, 9, 2) AS hour, {DELAY_EXPR} AS delay_min
            FROM flights WHERE {BASE_WHERE}
        )
        SELECT hour, {DELAY_AGG} FROM base GROUP BY hour ORDER BY hour
    """).fetchall()
    return [{"hour": int(r["hour"]),
             "arrived": r["n"],
             "delay_rate_pct": round1(100.0 * r["delayed"] / r["n"])} for r in rows]


def build_weather_effect(conn):
    cur = conn.cursor()
    weather_map = load_weather_by_obs_time(cur)
    # build_dataset은 행에 날짜를 남기지 않으므로 편마다 호출해 매칭된 관측 정각의 날짜를 붙인다.
    matched = []
    for f in load_arrived_flights(cur):
        rows, _ = build_dataset([f], weather_map)
        if rows:
            rows[0]["date"] = nearest_hour(parse_dt(f["estimated_dt"])).strftime("%Y%m%d")
            matched.append(rows[0])
    rainy, clear = split_by(matched, "rain", lambda v: v > 0)

    def group(rows):
        s = summarize(rows)
        return {"flights": s["count"],
                "delay_rate_pct": round1(s["delay_rate"]),
                "avg_delay_min": round1(s["avg_delay"])}

    a_yes = sum(1 for r in rainy if r["delay_min"] > DELAY_THRESHOLD_MIN)
    b_yes = sum(1 for r in clear if r["delay_min"] > DELAY_THRESHOLD_MIN)
    p = chi2_2x2_pvalue(a_yes, len(rainy) - a_yes, b_yes, len(clear) - b_yes)
    # 표본 한계를 보여주는 값이라, rain 그룹에 실제로 들어간 행의 날짜만 센다.
    rain_days = len({r["date"] for r in rainy})
    return {
        "rain": group(rainy),
        "clear": group(clear),
        "p_value": round_sig(p),
        "rain_days": rain_days,
    }


def main():
    # DB가 없을 때 sqlite3.connect를 하면 빈 파일이 새로 생겨버리므로 먼저 확인한다.
    if not DB_PATH.exists():
        print(f"❌ DB 파일이 없습니다: {DB_PATH}")
        sys.exit(1)

    conn = get_conn()
    try:
        data = {
            "meta": build_meta(),
            "health": {
                "flights": build_health_flights(conn),
                "weather": build_health_weather(conn),
            },
            "summary": build_summary(conn),
            "latest_weather": build_latest_weather(conn),
            "daily_trend": build_daily_trend(conn),
            "by_airline": build_by_airline(conn),
            "by_hour": build_by_hour(conn),
            "weather_effect": build_weather_effect(conn),
        }
    finally:
        conn.close()

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ {OUT_PATH.relative_to(Path(__file__).parent)} 생성 완료")


if __name__ == "__main__":
    main()

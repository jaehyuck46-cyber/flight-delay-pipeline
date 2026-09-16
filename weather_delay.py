"""
weather_delay.py — 날씨와 항공편 지연의 관계를 분석하는 파일

역할: flights 테이블(도착편)과 weather 테이블(정시 관측값)을
      '예상도착시각에서 가장 가까운 정각' 기준으로 결합해서,
      비/시정/바람 조건에 따라 지연율이 어떻게 달라지는지 비교한다.

이 파일은 DB를 읽기만 한다 (SELECT만 사용). 테이블 구조나 데이터를 바꾸지 않는다.

사용법: py weather_delay.py
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

# DB 파일 경로: 이 프로젝트 폴더 안의 data/flights.db (db.py와 동일한 위치)
DB_PATH = Path(__file__).parent / "data" / "flights.db"

# '지연편' 판정 기준 (분) — 이 값을 초과하면 지연으로 본다
DELAY_THRESHOLD_MIN = 15


def parse_dt(text):
    """'YYYYMMDDHHMM' 형식의 문자열을 datetime으로 바꾼다. 형식이 이상하면 None."""
    try:
        return datetime.strptime(text, "%Y%m%d%H%M")
    except (ValueError, TypeError):
        return None


def nearest_hour(dt):
    """어떤 시각에 가장 가까운 '정각(0분)'을 구한다.

    규칙: 분(minute)이 30 미만이면 그 시각 정각으로 내리고,
          30 이상이면 다음 정각으로 올린다.
    예: 07시26분 -> 07시00분 (내림) / 07시47분 -> 08시00분 (올림)

    timedelta로 시간을 더하기 때문에, 23시40분처럼 다음날로 넘어가는
    경우(자정/월말 등)도 datetime이 알아서 날짜를 굴려준다.
    """
    floored = dt.replace(minute=0, second=0, microsecond=0)
    if dt.minute < 30:
        return floored
    return floored + timedelta(hours=1)


def load_arrived_flights(cur):
    """status='도착'인 편만 가져온다.

    빈 status·결항·사전결항·회항은 실제로 '도착'하지 않았거나 착륙 여부가
    불확실해서, 도착 지연(분)을 계산할 수 없기 때문에 제외한다.
    """
    return cur.execute("""
        SELECT flight_id, scheduled_dt, estimated_dt
        FROM flights
        WHERE status = '도착'
    """).fetchall()


def load_weather_by_obs_time(cur):
    """weather 테이블을 obs_time(관측 정각)을 키로 하는 딕셔너리로 바꾼다."""
    rows = cur.execute("""
        SELECT obs_time, rain, wind_speed, visibility
        FROM weather
    """).fetchall()

    weather_map = {}
    for r in rows:
        weather_map[r["obs_time"]] = r
    return weather_map


def build_dataset(flights, weather_map):
    """항공편마다 지연(분)과 그 시각의 날씨를 하나로 묶은 리스트를 만든다.

    scheduled_dt/estimated_dt를 못 읽거나, 가장 가까운 정각의 날씨 관측이
    없는 편은 '매칭 실패'로 세고 분석 대상(matched)에서 제외한다.
    """
    matched = []
    unmatched_count = 0

    for f in flights:
        sched = parse_dt(f["scheduled_dt"])
        est = parse_dt(f["estimated_dt"])
        if sched is None or est is None:
            unmatched_count += 1
            continue

        # 예상도착시각(estimated_dt) 기준으로 가장 가까운 정각을 구해서 날씨를 붙인다
        target_hour = nearest_hour(est)
        obs_key = target_hour.strftime("%Y%m%d%H%M")

        w = weather_map.get(obs_key)
        if w is None:
            unmatched_count += 1
            continue

        delay_min = (est - sched).total_seconds() / 60
        matched.append({
            "delay_min": delay_min,
            "rain": w["rain"],
            "wind_speed": w["wind_speed"],
            "visibility": w["visibility"],
        })

    return matched, unmatched_count


def split_by(rows, key, classify):
    """rows 중 key 값이 있는(None이 아닌) 행만 골라, classify(값) 기준으로
    참(True) 그룹과 거짓(False) 그룹, 두 리스트로 나눈다.
    """
    valid = [r for r in rows if r[key] is not None]
    group_true = [r for r in valid if classify(r[key])]
    group_false = [r for r in valid if not classify(r[key])]
    return group_true, group_false


def summarize(rows):
    """편수 / 평균 지연(분) / 15분초과 지연율(%)을 계산한다."""
    n = len(rows)
    if n == 0:
        return {"count": 0, "avg_delay": None, "delay_rate": None}
    avg_delay = sum(r["delay_min"] for r in rows) / n
    delayed = sum(1 for r in rows if r["delay_min"] > DELAY_THRESHOLD_MIN)
    delay_rate = delayed / n * 100
    return {"count": n, "avg_delay": avg_delay, "delay_rate": delay_rate}


def print_stat_row(label, stats):
    """한 그룹의 통계를 표처럼 한 줄로 출력한다."""
    if stats["count"] == 0:
        print(f"  {label:<10} : 데이터 없음")
        return
    print(
        f"  {label:<10} : {stats['count']:>5}건   "
        f"평균지연 {stats['avg_delay']:>6.1f}분   "
        f"지연율(15분초과) {stats['delay_rate']:>5.1f}%"
    )


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    flights = load_arrived_flights(cur)
    weather_map = load_weather_by_obs_time(cur)
    matched, unmatched_count = build_dataset(flights, weather_map)

    conn.close()

    # 1) 요약: 대상 편 수 / 날씨 매칭 성공·실패 수
    print("── 1) 요약 ──")
    print(f"  status='도착' 편 수 : {len(flights)}건")
    print(f"  날씨 매칭 성공     : {len(matched)}건")
    print(f"  날씨 매칭 실패     : {unmatched_count}건")

    # 2) 강수 여부별 (rain > 0 이면 '비 온 날')
    print("\n── 2) 강수 여부별 지연 비교 ──")
    rainy, clear = split_by(matched, "rain", lambda v: v > 0)
    print_stat_row("비 온 날", summarize(rainy))
    print_stat_row("맑은 날", summarize(clear))

    # 3) 시정별 (visibility < 1000m 이면 '시정 나쁨')
    print("\n── 3) 시정별 지연 비교 ──")
    bad_vis, good_vis = split_by(matched, "visibility", lambda v: v < 1000)
    print_stat_row("시정 나쁨", summarize(bad_vis))
    print_stat_row("시정 좋음", summarize(good_vis))

    # 4) 풍속별 (wind_speed >= 8 이면 '강풍')
    print("\n── 4) 풍속별 지연 비교 ──")
    strong_wind, weak_wind = split_by(matched, "wind_speed", lambda v: v >= 8)
    print_stat_row("강풍", summarize(strong_wind))
    print_stat_row("약풍", summarize(weak_wind))


if __name__ == "__main__":
    main()

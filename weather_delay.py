"""
weather_delay.py — 날씨와 항공편 지연의 관계를 분석하는 파일

역할: flights 테이블(도착편)과 weather 테이블(정시 관측값)을
      '예상도착시각에서 가장 가까운 정각' 기준으로 결합해서,
      비/시정/바람 조건에 따라 지연율이 어떻게 달라지는지 비교한다.

이 파일은 DB를 읽기만 한다 (SELECT만 사용). 테이블 구조나 데이터를 바꾸지 않는다.

사용법: py weather_delay.py
"""

import sqlite3
import sys
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

# 결과를 파일로 저장하거나 다른 프로그램으로 넘길 때, 윈도우 기본 인코딩(cp949)은
# ⚠ 같은 기호를 못 찍고 에러가 난다. 그래서 출력 인코딩을 UTF-8로 고정한다.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# DB 파일 경로: 이 프로젝트 폴더 안의 data/flights.db (db.py와 동일한 위치)
DB_PATH = Path(__file__).parent / "data" / "flights.db"

# '지연편' 판정 기준 (분) — 이 값을 초과하면 지연으로 본다
DELAY_THRESHOLD_MIN = 15

# 기상청 ASOS의 시정(vs) 값은 m가 아니라 '10m 단위'로 온다.
# 예: DB에 2557이 있으면 실제 시정은 2557 × 10 = 25,570m (약 25.6km).
# 그래서 m 기준 구간과 비교하기 전에 이 값을 곱해서 m로 바꾼다.
VIS_UNIT_M = 10

# 시정 구간 정의 (m 환산 후 기준): (라벨, 이상, 미만). None이면 그쪽 끝이 없다는 뜻.
# 서울 관측값은 대부분 10km 이상이라, 항공 기준(1km 등)으로 나누면 거의 한 구간에 몰린다.
# 그래서 실제 데이터 분포에 맞춰 나눴다. 경계를 바꾸고 싶으면 여기 숫자만 고치면 된다.
VIS_BINS = [
    ("흐림·박무", None, 10000),
    ("보통", 10000, 20000),
    ("양호", 20000, 30000),
    ("매우 좋음", 30000, None),
]

# 구간 하나의 건수가 이보다 적으면 비율이 우연에 크게 흔들리므로 '⚠ 표본 부족' 표시
MIN_SAMPLE = 30

# 표에서 라벨이 차지할 화면 폭 (한글은 2칸으로 계산)
LABEL_WIDTH = 18


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


def split_into_bins(rows, key, bins, unit=1):
    """rows를 bins에 정의된 여러 구간으로 나눈다 (split_by의 여러 구간 버전).

    - key 값이 None인 행은 어느 구간에도 넣지 않고, 개수만 따로 센다.
    - 값에 unit을 곱해서(단위 환산) 구간 경계와 비교한다.
    - 아무 행도 안 들어간 구간도 빈 리스트로 남겨둔다 (출력에서 구간이 사라지지 않게).

    반환: ({라벨: [행, ...]}, None이라 제외된 행 수)
    """
    groups = {label: [] for label, _, _ in bins}
    missing = 0
    for r in rows:
        value = r[key]
        if value is None:
            missing += 1
            continue
        value = value * unit
        for label, low, high in bins:
            if (low is None or value >= low) and (high is None or value < high):
                groups[label].append(r)
                break
    return groups, missing


def fmt_range_km(low, high):
    """구간 경계(m)를 읽기 쉬운 km 표기로 바꾼다. 예: (10000, 20000) -> '10~20km'"""
    if low is None:
        return f"<{high / 1000:g}km"
    if high is None:
        return f"≥{low / 1000:g}km"
    return f"{low / 1000:g}~{high / 1000:g}km"


def summarize(rows):
    """편수 / 평균 지연(분) / 15분초과 지연율(%)을 계산한다."""
    n = len(rows)
    if n == 0:
        return {"count": 0, "avg_delay": None, "delay_rate": None}
    avg_delay = sum(r["delay_min"] for r in rows) / n
    delayed = sum(1 for r in rows if r["delay_min"] > DELAY_THRESHOLD_MIN)
    delay_rate = delayed / n * 100
    return {"count": n, "avg_delay": avg_delay, "delay_rate": delay_rate}


def visual_len(s):
    """글자가 화면에서 차지하는 폭을 센다.

    한글처럼 전각(W/F) 문자는 터미널에서 2칸을 차지하는데, len()은 1로 세서
    f"{label:<10}" 같은 정렬이 어긋난다. 그래서 전각은 2칸, 나머지는 1칸으로 센다.
    """
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def print_stat_row(label, stats, min_sample=None):
    """한 그룹의 통계를 표처럼 한 줄로 출력한다.

    min_sample을 주면, 건수가 그보다 적을 때 줄 끝에 '⚠ 표본 부족'을 붙인다.
    """
    name = label + " " * max(LABEL_WIDTH - visual_len(label), 0)
    if stats["count"] == 0:
        print(f"  {name} : 데이터 없음")
        return
    line = (
        f"  {name} : {stats['count']:>5}건   "
        f"평균지연 {stats['avg_delay']:>6.1f}분   "
        f"지연율(15분초과) {stats['delay_rate']:>5.1f}%"
    )
    if min_sample is not None and stats["count"] < min_sample:
        line += "   ⚠ 표본 부족"
    print(line)


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

    # 3) 시정 구간별 (visibility × VIS_UNIT_M 으로 m 환산 후 VIS_BINS 구간으로 나눔)
    print("\n── 3) 시정 구간별 지연 비교 ──")
    vis_groups, vis_missing = split_into_bins(matched, "visibility", VIS_BINS, VIS_UNIT_M)
    for label, low, high in VIS_BINS:
        print_stat_row(
            f"{label} ({fmt_range_km(low, high)})",
            summarize(vis_groups[label]),
            min_sample=MIN_SAMPLE,
        )
    print(f"  (시정 값 없음으로 제외: {vis_missing}건)")

    # 4) 풍속별 (wind_speed >= 8 이면 '강풍')
    print("\n── 4) 풍속별 지연 비교 ──")
    strong_wind, weak_wind = split_by(matched, "wind_speed", lambda v: v >= 8)
    print_stat_row("강풍", summarize(strong_wind))
    print_stat_row("약풍", summarize(weak_wind))


if __name__ == "__main__":
    main()

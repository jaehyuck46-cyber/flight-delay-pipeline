"""
weather_delay.py — 날씨와 항공편 지연의 관계를 분석하는 파일

역할: flights 테이블(도착편)과 weather 테이블(정시 관측값)을
      '예상도착시각에서 가장 가까운 정각' 기준으로 결합해서,
      비/시정/바람 조건에 따라 지연율이 어떻게 달라지는지 비교한다.
      마지막에 두 그룹 차이가 '우연인지 아닌지' 통계 검정(카이제곱·t-검정)도 한다.

이 파일은 DB를 읽기만 한다 (SELECT만 사용). 테이블 구조나 데이터를 바꾸지 않는다.

사용법: py weather_delay.py
"""

import math
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

# 유의수준: '이 정도 차이가 순전히 우연으로 나올 확률(p-값)'이 이 값보다 작으면
# 우연이 아니라고 본다. 0.05 = 같은 실험을 100번 해서 5번 이하로만 우연히 나올 정도.
SIG_LEVEL = 0.05

# 5번 섹션(검정) 라벨은 'A vs B' 형태라 길어서 칸을 따로 넓게 잡는다
TEST_LABEL_WIDTH = 22


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


def chi2_2x2_pvalue(a_yes, a_no, b_yes, b_no):
    """카이제곱 검정: 두 그룹의 '지연율' 차이가 우연인지 본다. p-값을 돌려준다.

    비유: 동전 두 개를 던져 앞면 비율이 다를 때, 그 차이가 동전 탓인지
    운 탓인지 따지는 것. '두 그룹이 사실 똑같다'고 가정했을 때 기대되는 개수와
    실제 개수가 얼마나 벌어졌는지를 하나의 점수(chi2)로 만든다.

    표 모양 (2×2):
                 지연     정시
        그룹 A   a_yes    a_no
        그룹 B   b_yes    b_no

    - Yates 보정: 칸이 2×2일 때 점수가 부풀려지는 것을 막으려고 차이에서 0.5씩 깎는다.
      (scipy.stats.chi2_contingency의 기본 동작과 같다)
    - 자유도 1인 카이제곱의 p-값은 math.erfc(√(chi2/2))로 정확히 구할 수 있다.
    - 어떤 행/열 합이 0이면(한쪽이 비었거나 전부 지연/전부 정시) 계산할 수 없어 None.
    """
    n = a_yes + a_no + b_yes + b_no
    row_a, row_b = a_yes + a_no, b_yes + b_no
    col_yes, col_no = a_yes + b_yes, a_no + b_no
    if 0 in (row_a, row_b, col_yes, col_no):
        return None

    # 2×2에서는 네 칸 모두 '실제 - 기대' 차이의 크기가 |ad - bc| / n 으로 같다.
    diff = abs(a_yes * b_no - a_no * b_yes) / n
    # Yates 보정: 차이를 0.5 줄이되, 기대값을 넘어 반대로 가지는 않게 0에서 멈춘다.
    adj = max(diff - 0.5, 0.0)
    # 네 칸의 (차이² / 기대값)을 더한 값. 2×2에서는 아래 한 줄로 정리된다.
    chi2 = adj * adj * n ** 3 / (row_a * row_b * col_yes * col_no)
    return math.erfc(math.sqrt(chi2 / 2))


def _betacf(a, b, x):
    """불완전 베타 함수의 연분수 부분 (Numerical Recipes 'betacf').

    비유: 무한히 이어지는 분수를, 값이 더 이상 거의 안 바뀔 때까지 한 단계씩
    계산해 나가는 방법. t-분포의 꼬리 넓이(p-값)를 구하는 데 쓴다.
    """
    max_iter, eps, fpmin = 300, 3e-14, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betai(a, b, x):
    """정규화 불완전 베타 함수 I_x(a, b) (Numerical Recipes 'betai').

    t-분포의 양쪽 꼬리 넓이 = I_{df/(df+t²)}(df/2, 1/2) 라서 이 함수로 p-값을 구한다.
    x가 크면 연분수가 느리게 수렴하므로, 대칭 관계 I_x(a,b) = 1 - I_{1-x}(b,a)를 쓴다.
    """
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    # 앞에 곱하는 계수. 큰 수끼리 곱하면 넘치므로 로그(lgamma)로 계산한 뒤 exp.
    ln_front = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                + a * math.log(x) + b * math.log(1.0 - x))
    front = math.exp(ln_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def welch_t_pvalue(values_a, values_b):
    """Welch t-검정: 두 그룹의 '평균 지연(분)' 차이가 우연인지 본다. (t, p)를 돌려준다.

    비유: 두 반의 평균 점수 차이가, 각 반 점수가 들쭉날쭉한 정도에 비해
    큰지 작은지를 보는 것. 들쭉날쭉(분산)이 클수록 평균 차이가 우연일 가능성이 크다.

    - Welch 방식: 두 그룹의 분산이 달라도 쓸 수 있는 t-검정
      (비 온 날은 지연이 훨씬 들쭉날쭉해서 '분산이 같다'는 가정을 하면 안 된다).
    - 자유도는 Welch–Satterthwaite 식으로 구하고, p-값은 양측(차이의 방향 무관)이다.
    - 표본이 2개 미만이거나 두 그룹 분산이 모두 0이면 계산할 수 없어 (None, None).
    """
    na, nb = len(values_a), len(values_b)
    if na < 2 or nb < 2:
        return None, None
    mean_a = sum(values_a) / na
    mean_b = sum(values_b) / nb
    # 표본 분산 (n-1로 나눔)
    var_a = sum((v - mean_a) ** 2 for v in values_a) / (na - 1)
    var_b = sum((v - mean_b) ** 2 for v in values_b) / (nb - 1)
    se_a, se_b = var_a / na, var_b / nb
    if se_a + se_b == 0:
        return None, None

    t = (mean_a - mean_b) / math.sqrt(se_a + se_b)
    df = (se_a + se_b) ** 2 / (se_a ** 2 / (na - 1) + se_b ** 2 / (nb - 1))
    p = _betai(df / 2.0, 0.5, df / (df + t * t))
    return t, p


def fmt_p(p):
    """p-값을 읽기 쉽게: None -> '계산불가', 아주 작으면 '<0.001', 나머지는 소수 3자리."""
    if p is None:
        return "계산불가"
    if p < 0.001:
        return "<0.001"
    return f"{p:.3f}"


def print_test_row(label, group_a, group_b):
    """두 그룹에 카이제곱(지연율)·Welch t(평균 지연) 검정을 하고 결과를 한 줄로 출력한다.

    판정은 보수적으로: 두 p-값이 '모두' SIG_LEVEL 미만일 때만 유의미하다고 본다.
    둘 중 하나라도 크면 '아직 판단 불가'(데이터가 더 쌓이면 바뀔 수 있음).
    """
    name = label + " " * max(TEST_LABEL_WIDTH - visual_len(label), 0)
    if not group_a or not group_b:
        print(f"  {name} : 한쪽 그룹이 비어 있어 검정 불가")
        return

    a_yes = sum(1 for r in group_a if r["delay_min"] > DELAY_THRESHOLD_MIN)
    b_yes = sum(1 for r in group_b if r["delay_min"] > DELAY_THRESHOLD_MIN)
    p_chi = chi2_2x2_pvalue(a_yes, len(group_a) - a_yes, b_yes, len(group_b) - b_yes)
    _, p_t = welch_t_pvalue([r["delay_min"] for r in group_a],
                            [r["delay_min"] for r in group_b])

    if p_chi is not None and p_t is not None and p_chi < SIG_LEVEL and p_t < SIG_LEVEL:
        verdict = "✅ 유의미 (우연일 가능성 낮음)"
    else:
        verdict = "❌ 아직 판단 불가"

    line = (
        f"  {name} : {len(group_a):>5} vs {len(group_b):>5}건   "
        f"카이제곱 p={fmt_p(p_chi):>6}   t-검정 p={fmt_p(p_t):>6}   {verdict}"
    )
    if min(len(group_a), len(group_b)) < MIN_SAMPLE:
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

    # 5) 유의성 검정: 위 2)~4)의 차이가 우연인지 아닌지
    print(f"\n── 5) 유의성 검정 (유의수준 {SIG_LEVEL}) ──")
    print_test_row("비 온 날 vs 맑은 날", rainy, clear)
    low_vis, high_vis = split_by(matched, "visibility", lambda v: v * VIS_UNIT_M < 10000)
    print_test_row("시정 <10km vs ≥10km", low_vis, high_vis)
    print_test_row("강풍 vs 약풍", strong_wind, weak_wind)
    print("  ※ 같은 날·같은 비행기 편끼리는 서로 영향을 주므로(연결편 지연),")
    print("    실제 p-값은 위 숫자보다 조금 덜 확실하다고 보는 게 안전하다.")


if __name__ == "__main__":
    main()

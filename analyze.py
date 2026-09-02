"""
analyze.py — 저장된 데이터 확인/요약 파일

역할: flights.db에 쌓인 김포 도착편 데이터를 읽어서 요약해 보여준다.
사용법: py analyze.py
"""

from collections import defaultdict
from datetime import datetime

from db import get_connection


def _parse_dt(text):
    """'YYYYMMDDHHMM' 문자열을 datetime으로 바꾼다. 형식이 이상하면 None."""
    try:
        return datetime.strptime(text, "%Y%m%d%H%M")
    except (ValueError, TypeError):
        return None


def analyze_overall_delay():
    """도착편 전체의 지연율을 계산한다 (15분 기준 / 0분 기준)."""
    conn = get_connection()
    cur = conn.cursor()

    # 도착 완료 + 계획시각·예상시각이 둘 다 있는 편만 대상으로 한다
    rows = cur.execute("""
        SELECT scheduled_dt, estimated_dt
        FROM flights
        WHERE status = '도착'
          AND scheduled_dt IS NOT NULL AND scheduled_dt != ''
          AND estimated_dt IS NOT NULL AND estimated_dt != ''
    """).fetchall()
    conn.close()

    total = 0
    delayed_15 = 0
    delayed_0 = 0
    for r in rows:
        sched = _parse_dt(r["scheduled_dt"])
        est = _parse_dt(r["estimated_dt"])
        if sched is None or est is None:
            continue
        delay_min = (est - sched).total_seconds() / 60
        total += 1
        if delay_min >= 15:
            delayed_15 += 1
        if delay_min > 0:
            delayed_0 += 1

    print("\n── 전체 지연율 ──")
    if total == 0:
        print("  분석 대상 없음 (도착편 데이터 부족)")
        return
    print(f"  대상 {total}건 중")
    print(f"  15분 이상 지연: {delayed_15}건 ({delayed_15 / total * 100:.1f}%)")
    print(f"  0분 초과 지연 : {delayed_0}건 ({delayed_0 / total * 100:.1f}%)")


def analyze_delay_by_airline():
    """항공사별 지연율을 계산한다 (20편 이상인 항공사만, 15분 지연율 높은 순)."""
    conn = get_connection()
    cur = conn.cursor()

    rows = cur.execute("""
        SELECT airline, scheduled_dt, estimated_dt
        FROM flights
        WHERE status = '도착'
          AND scheduled_dt IS NOT NULL AND scheduled_dt != ''
          AND estimated_dt IS NOT NULL AND estimated_dt != ''
    """).fetchall()
    conn.close()

    # 항공사별로 (총 편수, 15분지연 건수, 0분지연 건수)를 누적한다
    stats = defaultdict(lambda: [0, 0, 0])
    for r in rows:
        sched = _parse_dt(r["scheduled_dt"])
        est = _parse_dt(r["estimated_dt"])
        if sched is None or est is None:
            continue
        delay_min = (est - sched).total_seconds() / 60
        s = stats[r["airline"] or "(없음)"]
        s[0] += 1
        if delay_min >= 15:
            s[1] += 1
        if delay_min > 0:
            s[2] += 1

    # 편수 20편 이상만 남기고, 15분 지연율 높은 순으로 정렬
    airlines = [(name, cnt, d15, d0) for name, (cnt, d15, d0) in stats.items() if cnt >= 20]
    airlines.sort(key=lambda x: (x[2] / x[1]) if x[1] else 0, reverse=True)

    print("\n── 항공사별 지연율 (20편 이상, 15분지연율 높은 순) ──")
    if not airlines:
        print("  분석 대상 없음 (20편 이상인 항공사 없음)")
        return
    print(f"  {'항공사':<8} {'편수':>6} {'15분지연율':>10} {'0분지연율':>10}")
    for name, cnt, d15, d0 in airlines:
        print(f"  {name:<8} {cnt:>6} {d15 / cnt * 100:>9.1f}% {d0 / cnt * 100:>9.1f}%")


def analyze_delay_by_hour():
    """계획 시각의 '시(hour)' 기준으로 15분 지연율을 계산한다."""
    conn = get_connection()
    cur = conn.cursor()

    rows = cur.execute("""
        SELECT scheduled_dt, estimated_dt
        FROM flights
        WHERE status = '도착'
          AND scheduled_dt IS NOT NULL AND scheduled_dt != ''
          AND estimated_dt IS NOT NULL AND estimated_dt != ''
    """).fetchall()
    conn.close()

    # 시간대(0~23)별로 (총 편수, 15분지연 건수)를 누적한다
    stats = defaultdict(lambda: [0, 0])
    for r in rows:
        sched = _parse_dt(r["scheduled_dt"])
        est = _parse_dt(r["estimated_dt"])
        if sched is None or est is None:
            continue
        delay_min = (est - sched).total_seconds() / 60
        s = stats[sched.hour]
        s[0] += 1
        if delay_min >= 15:
            s[1] += 1

    print("\n── 시간대별 15분 지연율 ──")
    if not stats:
        print("  분석 대상 없음")
        return
    for hour in sorted(stats):
        cnt, d15 = stats[hour]
        print(f"  {hour:02d}시 : {cnt:>4}건 중 {d15:>4}건 지연 ({d15 / cnt * 100:.1f}%)")


def main():
    conn = get_connection()
    cur = conn.cursor()

    # 1) 전체 저장 건수
    total = cur.execute("SELECT COUNT(*) FROM flights").fetchone()[0]
    print(f"📊 저장된 김포 도착편: 총 {total}건\n")

    # 2) 상태별 분포 (도착/지연/결항 등)
    print("── 상태별 분포 ──")
    rows = cur.execute("""
        SELECT status, COUNT(*) AS cnt
        FROM flights
        GROUP BY status
        ORDER BY cnt DESC
    """).fetchall()
    for r in rows:
        print(f"  {r['status'] or '(없음)'} : {r['cnt']}건")

    # 3) 항공사별 편수 (상위 10)
    print("\n── 항공사별 편수 (상위 10) ──")
    rows = cur.execute("""
        SELECT airline, COUNT(*) AS cnt
        FROM flights
        GROUP BY airline
        ORDER BY cnt DESC
        LIMIT 10
    """).fetchall()
    for r in rows:
        print(f"  {r['airline']} : {r['cnt']}건")

    # 4) 상태변경 이력 건수
    events = cur.execute("SELECT COUNT(*) FROM flight_events").fetchone()[0]
    print(f"\n📝 상태변경 이력: {events}건")

    # 5) 최근 수집 로그 5건
    print("\n── 최근 수집 기록 ──")
    rows = cur.execute("""
        SELECT collected_at, rows_fetched, rows_changed
        FROM collection_log
        ORDER BY id DESC
        LIMIT 5
    """).fetchall()
    for r in rows:
        print(f"  {r['collected_at']} — 수집 {r['rows_fetched']}건 (변경 {r['rows_changed']}건)")

    conn.close()

    # 6) 지연율 분석
    analyze_overall_delay()
    analyze_delay_by_airline()
    analyze_delay_by_hour()


if __name__ == "__main__":
    main()
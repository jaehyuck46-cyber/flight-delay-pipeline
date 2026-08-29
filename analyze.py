"""
analyze.py — 저장된 데이터 확인/요약 파일

역할: flights.db에 쌓인 김포 도착편 데이터를 읽어서 요약해 보여준다.
사용법: py analyze.py
"""

from db import get_connection


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


if __name__ == "__main__":
    main()
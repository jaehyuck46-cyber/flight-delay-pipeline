"""
db.py — 데이터베이스 담당 파일

역할: SQLite에 연결하고, 테이블 3개를 만든다.
      (데이터를 '담을 그릇'을 만드는 파일)
"""

import sqlite3
from pathlib import Path

# DB 파일 경로: 이 프로젝트 폴더 안의 data/flights.db
DB_DIR = Path(__file__).parent / "data"
DB_PATH = DB_DIR / "flights.db"


def get_connection():
    """DB에 연결한다. 파일 없으면 자동 생성됨."""
    DB_DIR.mkdir(exist_ok=True)          # data 폴더 없으면 만들기
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row       # 결과를 딕셔너리처럼 쓰게
    return conn


def init_db():
    """테이블 3개를 만든다. 이미 있으면 그냥 넘어감."""
    conn = get_connection()
    cur = conn.cursor()

    # ── 1. flights: 편별 '현재 상태' (편당 1행) ──
    cur.execute("""
        CREATE TABLE IF NOT EXISTS flights (
            flight_key   TEXT PRIMARY KEY,   -- 편명+날짜+출도착 (중복 방지 고유키)
            flight_id    TEXT,               -- 편명 (예: KE1234)
            airline      TEXT,               -- 항공사
            airport      TEXT,               -- 공항 (GMP 등)
            io_type      TEXT,               -- 출발/도착
            scheduled_dt TEXT,               -- 계획 시각
            estimated_dt TEXT,               -- 예상 시각
            status       TEXT,               -- 상태 (지연/결항/출발 등)
            collected_at TEXT,               -- 처음 수집한 시각
            updated_at   TEXT                -- 마지막 갱신 시각
        )
    """)

    # ── 2. flight_events: 상태 '변경 이력' (편당 N행) ──
    cur.execute("""
        CREATE TABLE IF NOT EXISTS flight_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            flight_key   TEXT,               -- 어느 편인지 (flights와 연결)
            status       TEXT,               -- 그 시점의 상태
            estimated_dt TEXT,               -- 그 시점의 예상 시각
            collected_at TEXT                -- 이 변화를 감지한 시각
        )
    """)

    # ── 3. collection_log: '수집 실행' 기록 (실행당 1행) ──
    cur.execute("""
        CREATE TABLE IF NOT EXISTS collection_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            collected_at  TEXT,              -- 수집한 시각
            airport       TEXT,              -- 어느 공항
            rows_fetched  INTEGER,           -- 받아온 편 수
            rows_changed  INTEGER,           -- 상태가 바뀐 편 수
            success       INTEGER,           -- 성공 1 / 실패 0
            error_message TEXT               -- 실패했으면 이유
        )
    """)

    # ── 4. weather: 김포공항 날씨 스냅샷 (수집 시각당 1행) ──
    #     항공편을 수집하는 그 시각의 김포 날씨를 함께 저장한다.
    #     나중에 flights와 시각 기준으로 엮어서 "날씨별 지연" 분석에 쓴다.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS weather (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            airport       TEXT,               -- 어느 공항 (GMP)
            base_date     TEXT,               -- 기상청 발표 날짜 (YYYYMMDD)
            base_time     TEXT,               -- 기상청 발표 시각 (HHMM)
            fcst_date     TEXT,               -- 예보 대상 날짜
            fcst_time     TEXT,               -- 예보 대상 시각
            temp          REAL,               -- TMP: 기온(℃)
            rain_type     INTEGER,            -- PTY: 강수형태 (0없음/1비/2비눈/3눈/4소나기)
            sky           INTEGER,            -- SKY: 하늘상태 (1맑음/3구름많음/4흐림)
            wind_speed    REAL,               -- WSD: 풍속(m/s)
            humidity      INTEGER,            -- REH: 습도(%)
            rain_prob     INTEGER,            -- POP: 강수확률(%)
            collected_at  TEXT                -- 이 날씨를 수집한 시각
        )
    """)

    conn.commit()
    conn.close()
    print(f"✅ DB 준비 완료 → {DB_PATH}")


# 이 파일을 직접 실행하면 (py db.py) 테이블을 만든다.
if __name__ == "__main__":
    init_db()
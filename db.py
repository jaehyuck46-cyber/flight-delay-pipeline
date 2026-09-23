"""
db.py — 데이터베이스 담당 파일

역할: SQLite에 연결하고, 테이블 5개를 만든다.
      (flights · flight_events · collection_log · weather · flight_tracks)
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
    """테이블 5개를 만든다. 이미 있으면 그냥 넘어감."""
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

    # ── 4. weather: 서울(108) ASOS 시간관측 (관측시각당 1행) ──
    #     기상청 ASOS 시간자료 API로 '실제 관측된' 과거 날씨를 저장한다.
    #     (예보 아님 — 지난 시각의 검증된 관측값. 백필로 8/26부터 채운다)
    #     김포엔 ASOS 지점이 없어 가장 가까운 서울(108)을 대체 지점으로 쓴다.
    #     나중에 flights와 시각(obs_time) 기준으로 엮어 "날씨별 지연"을 분석한다.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS weather (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            stn_id        TEXT,               -- 관측 지점번호 (108=서울)
            obs_time      TEXT,               -- 관측 시각 (tm, YYYYMMDDHHMM)
            temp          REAL,               -- ta: 기온(℃)
            rain          REAL,               -- rn: 강수량(mm), 비 안오면 0
            wind_speed    REAL,               -- ws: 풍속(m/s)
            humidity      INTEGER,            -- hm: 습도(%)
            cloud         INTEGER,            -- dc10Tca: 전운량(0~10)
            visibility    INTEGER,            -- vs: 시정(m) — 항공 지연 핵심 변수
            collected_at  TEXT,               -- 이 행을 수집/적재한 시각
            UNIQUE(stn_id, obs_time)          -- 같은 지점·같은 시각 중복 방지
        )
    """)

    # ── 5. flight_tracks: 김포 상공 항공기 실시간 위치(ADS-B) (스냅샷당 N행) ──
    #     OpenSky Network states/all API로 '지금 이 순간' 김포 주변 하늘의
    #     항공기 위치를 30분마다 찍어 쌓는다. 백필 불가 — 실시간만 수집.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS flight_tracks (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            icao24        TEXT,               -- 기체 고유번호(트랜스폰더 ID)
            callsign      TEXT,               -- 편명(콜사인)
            captured_at   TEXT,               -- OpenSky 스냅샷 시각
            latitude      REAL,               -- 위도
            longitude     REAL,               -- 경도
            baro_altitude REAL,               -- 기압고도(m)
            velocity      REAL,               -- 속도(m/s)
            true_track    REAL,               -- 진행방향(도, 0~360)
            on_ground     INTEGER,            -- 지상 여부 (1/0)
            collected_at  TEXT,               -- 이 행을 수집/적재한 시각
            UNIQUE(icao24, captured_at)       -- 같은 기체·같은 스냅샷 중복 방지
        )
    """)

    conn.commit()
    conn.close()
    print(f"✅ DB 준비 완료 → {DB_PATH}")


# 이 파일을 직접 실행하면 (py db.py) 테이블을 만든다.
if __name__ == "__main__":
    init_db()
    
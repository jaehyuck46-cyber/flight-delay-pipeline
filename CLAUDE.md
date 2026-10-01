## 커스텀 커맨드: 브리핑

사용자가 "브리핑" 이라고 입력하면 아래 순서로 자동 실행해줘.

### 실행 순서

1. `py brief.py` 실행 → 출력 결과 그대로 보여주기
2. `py data_brief.py` 실행 → 출력 결과 그대로 보여주기
3. 두 결과를 종합해서 한국어로 3줄 요약:
   - 파이프라인 상태 (수집 정상 여부)
   - 데이터 현황 (편수, 지연율 핵심 수치)
   - 주목할 점 (이상치·경고·날씨 영향 등)

### 규칙
- brief.py / data_brief.py 둘 다 없으면 "브리핑 파일이 없어. 먼저 brief.py와 data_brief.py를 생성해줄까?" 라고 물어보기
- 한 파일만 없으면 있는 것만 실행하고 없는 파일 이름 언급
- 에러 나면 에러 내용 보여주고 바로 수정 후 재실행
- 브리핑 결과 외 불필요한 설명 없이 결과만 출력

### brief.py 작성 규칙 (없을 때 새로 만드는 경우 적용)

- DB_PATH = Path(__file__).parent / "data" / "flights.db"
- get_conn(): sqlite3.connect + row_factory = sqlite3.Row
- utcnow() 헬퍼 사용:
    def utcnow():
        return datetime.now(timezone.utc).replace(tzinfo=None)
  datetime.utcnow() 직접 호출 금지 (Python 3.14 경고 대상)
- collected_at은 한국시간(KST) 문자열이므로 UTC 변환 후 경과 시간 계산:
    kst_dt = datetime.fromisoformat(collected_at)
    utc_dt = kst_dt - timedelta(hours=9)
    elapsed = utcnow() - utc_dt
- 시정 visibility < 1000이면 경고
- 테이블 없거나 데이터 없으면 "⚠ 데이터 없음" 출력 후 다음 섹션 진행
- try/except로 각 섹션 독립 보호
- flight_tracks: sqlite_master로 존재 확인 후 진행
- 출력 라벨 폭: unicodedata.east_asian_width 기준으로 W/F이면 2칸, 나머지 1칸
    import unicodedata
    def visual_len(s):
        return sum(2 if unicodedata.east_asian_width(c) in ('W','F') else 1 for c in s)
    def row(label, value):
        pad = 30 - visual_len(label)
        print(f"  {label}{' ' * max(pad, 1)}{value}")

### data_brief.py 작성 규칙 (없을 때 새로 만드는 경우 적용)

- utcnow() 헬퍼 동일하게 사용
- DELAY_EXPR 정의 (Python 문자열 변수, 모든 CTE에서 f-string으로 삽입):
    DELAY_EXPR = """ROUND((
        julianday(
            substr(estimated_dt,1,4)||'-'||substr(estimated_dt,5,2)||'-'||
            substr(estimated_dt,7,2)||' '||substr(estimated_dt,9,2)||':'||
            substr(estimated_dt,11,2)
        ) -
        julianday(
            substr(scheduled_dt,1,4)||'-'||substr(scheduled_dt,5,2)||'-'||
            substr(scheduled_dt,7,2)||' '||substr(scheduled_dt,9,2)||':'||
            substr(scheduled_dt,11,2)
        )) * 1440, 4)"""
  ROUND(..., 4) 이유: 소수 오차(예: 119.9999분) 때문에
  >0 또는 >=120 비교가 경계에서 빠지는 것을 방지.
  지금 brief.py는 정수 반올림이지만 경계 비교 결과는 동일하므로 현재 파일은 수정 안 함.
- 출력 row() 헬퍼: unicodedata 방식 동일하게 사용
- try/except로 각 섹션 독립 보호
- 데이터 없거나 표본 부족하면 "⚠ 데이터 부족" 출력 후 다음 진행

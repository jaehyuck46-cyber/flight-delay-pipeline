# AGENTS.md

이 저장소에서 작업하는 AI 에이전트를 위한 구조·관례 안내입니다. "브리핑" 커스텀 커맨드는 `CLAUDE.md`에 있습니다.

## 프로젝트 개요

김포공항(GMP) 도착편의 지연을 날씨(서울 ASOS 108 관측)와 엮어 분석하는 파이프라인입니다.
GitHub Actions가 30분마다 항공편·날씨·항공기 위치를 수집해 `data/flights.db`(SQLite)에 쌓고,
로컬에서는 그 DB를 읽어 브리핑·분석 스크립트를 돌립니다.

## 파일 구성

| 파일 | 역할 |
|---|---|
| `db.py` | SQLite 연결, 테이블 5개(`flights`/`flight_events`/`collection_log`/`weather`/`flight_tracks`) 생성 |
| `collect.py` | 공항공사 API → 김포 도착편 수집 (`flights`, `flight_events`, `collection_log`) |
| `weather.py` | 기상청 ASOS 시간자료 API → 서울(108) 관측 수집 (`weather`). `--backfill 시작일 끝일`로 과거 채우기 가능 |
| `track.py` | OpenSky states/all API → 김포 상공 실시간 위치 수집 (`flight_tracks`). 백필 불가(지금 순간만) |
| `brief.py` | 파이프라인 상태 브리핑 (수집 로그·최신 날씨·누적 현황) |
| `data_brief.py` | 데이터 분석 브리핑 (지연율·항공사별·시간대별·날씨×지연) |
| `weather_delay.py` | 날씨×지연 유의성 검정(카이제곱·Welch t-검정), SELECT 전용 |
| `analyze.py` | 옛 분석 스크립트. `analyze_delay_by_weather()`는 옛 예보 스키마(`fcst_date` 등) 컬럼을 참조해 지금 ASOS 스키마에서 에러남 (알려진 버그, BACKLOG 미등재) |
| `scripts/test_tracks.py` | OpenSky `/tracks/all`, `/flights/arrival` 실험용 (DB 읽기 전용) |
| `BACKLOG.md` | 분석 후속 작업과 알려진 데이터 품질 이슈 |
| `.github/workflows/collect.yml` | 30분 주기 자동 수집 (+ 수동 실행) |
| `.github/workflows/backfill.yml` | 날씨 백필 수동 실행 (`start_dt`, `end_dt` 입력) |

## Actions 워크플로 패턴

- **같은 concurrency 그룹(`db-write`), `cancel-in-progress: false`.** `collect.yml`과 `backfill.yml`이
  동시에 `data/flights.db`를 커밋하다 충돌하지 않도록, 먼저 시작한 쪽이 끝날 때까지 뒤는 대기열에서 기다린다.
- **`actions/checkout@v4`에 `with: ref: main`을 명시한다.** 이게 없으면 체크아웃이 '대기열에 들어간 시점'의
  커밋을 받아서, 앞 실행이 푸시한 뒤에 시작해도 옛 커밋 위에서 작업하다 `git push`가 `rejected (fetch first)`로
  실패한다. `ref: main`을 넣으면 실행이 **시작하는 시점**의 최신 `main`을 받아 이 문제가 사라진다.
- **수집 단계마다 `if: always()`.** `collect.py`/`weather.py`/`track.py`는 서로 독립이라, 하나가 실패해도
  나머지는 계속 돈다. 마지막 "DB를 저장소에 커밋" 단계도 `if: always()`라 무엇이든 하나만 성공해도 커밋된다.
- **DB는 Actions 안에서만 커밋한다.** 로컬에서 `py brief.py`/`py data_brief.py`/`weather_delay.py` 등을
  테스트한 뒤에는 `git restore data/flights.db`로 원상복구하고, 로컬 DB 변경은 절대 커밋하지 않는다.
  (코드 파일만 `git add`하고, 커밋 전에 `git status`로 `data/flights.db`가 빠져 있는지 항상 확인한다.)

## 수집 스크립트의 재시도 로직

`collect.py`·`weather.py`는 같은 패턴을 쓴다: 최대 3회 시도, 실패 시 `attempt * 3`초(3초→6초) 대기,
`except (urllib.error.URLError, TimeoutError)`로 잡는다. `URLError`는 '연결' 단계 실패만 잡고,
응답을 '읽다가' 나는 타임아웃은 `TimeoutError`(`socket.timeout`과 같은 클래스)로 따로 나기 때문에 둘 다 잡아야 한다.

- **`track.py`는 아직 `TimeoutError`를 재시도 대상에 넣지 않았다** (`except urllib.error.HTTPError`,
  `except urllib.error.URLError`만 처리). 읽기 타임아웃이 나면 재시도 없이 바로 실패한다. 고칠 때는
  `collect.py`/`weather.py`와 같은 패턴(`except (urllib.error.URLError, TimeoutError)`)으로 맞춘다.
- **`collect.py`는 실패를 `collection_log`에 남긴다.** 받아오기 단계가 재시도 3회 끝에도 실패하면
  `log_failure()`가 `success=0`과 에러 메시지를 넣고 `sys.exit(1)`로 끝나서 Actions 단계도 빨간불로 남는다.
  `weather.py`/`track.py`는 이 방식이 없다 — 날씨는 매번 어제치를 다시 받아 중복은 무시하므로 자동 복구되고,
  위치는 실시간 전용이라 실패해도 데이터 유실이 복구되지 않는다(경고만 필요하면 `brief.py`에 추가하는 방향).

## 시각·단위 관례

- **`scheduled_dt`/`estimated_dt`(flights)와 `obs_time`(weather)·`collected_at`은 모두 한국시간(KST) 문자열이다.**
  `flights`는 `YYYYMMDDHHMM`(12자리), `collected_at`류는 `YYYY-MM-DD HH:MM:SS`. UTC와 비교할 땐
  `utcnow()`(= `datetime.now(timezone.utc).replace(tzinfo=None)`, `datetime.utcnow()` 직접 호출 금지 —
  Python 3.12+ 경고 대상) 기준에 `KST_OFFSET = timedelta(hours=9)`를 더하거나 빼서 맞춘다.
- **ASOS 시정(`weather.visibility`)은 m가 아니라 10m 단위다.** DB 값 2557 = 실제 25,570m. `VIS_UNIT_M = 10`을
  곱해야 한다 (`brief.py`/`data_brief.py`/`weather_delay.py`에 각각 상수로 있다).
- **지연 기준이 파일마다 다르다.** `data_brief.py`는 "0분 초과"와 "D15(15분 초과)" 둘 다 보여주고,
  `weather_delay.py`는 15분 초과만 쓴다. 두 기준이 섞인 글은 숫자가 맞지 않을 수 있으니 어느 기준인지 먼저 확인한다.

## 알려진 데이터 품질 이슈 (BACKLOG.md 3번)

`collect.py`가 테이블 PK(`flight_key`)로 공항공사 API의 `fid`를 그대로 쓰는데, 같은 편에 API가 `fid`를
새로 매기면 새 행이 생기고 옛 `fid` 행은 상태가 빈 채로 남는다("유령 행"). 판별 조건은
`brief.py`/`data_brief.py`의 `GHOST_COND` 상수에 있다 (상태가 비었는데, 같은 편명·계획 시각이면서
`flight_key`가 다르고 상태가 있는 행이 따로 존재). 두 파일에 같은 조건이 중복돼 있으니 고칠 때는 함께 고친다.
PK를 자연키(편명+계획시각+출도착)로 재설계하는 건 v2(PostgreSQL 이관) 때 할 일로 미뤄져 있다.

## 항공사 코드 매핑 (IATA ↔ ICAO)

`flights.flight_id`는 IATA 2글자 코드(`KE1238`), ADS-B 콜사인·OpenSky API는 ICAO 3글자 코드(`KAL1238`)를
쓴다. 매핑은 `scripts/test_tracks.py`의 `IATA_TO_ICAO`(그 역인 `ICAO_TO_IATA`도 거기서 파생) 하나뿐이고,
다른 파일에는 없다. 9개 항공사만 들어 있고(`KE·OZ·7C·LJ·TW·RS·BX·ZE·WE`), `WE→PTA`(파라타항공)는
정기편 콜사인으로 검증되지 않아 "확인 필요" 주석이 달려 있다. 외항사(JAL·ANA·중국 국적사 등)는
매핑에 없어서, `scripts/test_tracks.py --arrivals`로 비교하면 "매핑 밖 콜사인"으로 빠진다.

## 로컬 실행 환경

- 이 프로젝트를 쓰는 PC에서는 `python`이 Microsoft Store 별칭이라 실패한다 — **`py`로 실행한다.**
  (GitHub Actions는 Linux라 워크플로 안에서는 그대로 `python`을 쓴다.)
- 로컬 콘솔 기본 인코딩이 cp949라 `⚠`/`✅` 같은 기호 출력 시 `UnicodeEncodeError`가 날 수 있다.
  `brief.py`/`data_brief.py`/`weather_delay.py`는 `sys.stdout.reconfigure(encoding="utf-8")`로 이미
  방어돼 있지만, 새 스크립트를 짤 때는 같은 처리를 넣거나 `PYTHONIOENCODING=utf-8 py ...`로 실행한다.
- `.env`에 `KAC_SERVICE_KEY`(공항공사)·`KMA_SERVICE_KEY`(기상청)·`OPENSKY_CLIENT_ID`/`OPENSKY_CLIENT_SECRET`를
  둔다. `.env`와 `data/`는 `.gitignore`에 있다 — 절대 커밋하지 않는다.

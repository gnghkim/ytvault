# 헬스 모니터링 (Uptime Kuma)

Beszel은 `ytvault.service` 프로세스가 떠 있는지만 본다. 프로세스가 살아 있어도 워커 스레드가 멈추거나, 텔레그램 폴링이 끊기거나, Drive 토큰이 만료되면 영상은 저장되지 않는다.
헬스체커는 이런 **실제 기능의 상태**를 1분마다 판정해 Uptime Kuma의 Push 모니터로 보낸다.

```
ytvault.service (계속 실행)                 ytvault-healthcheck.timer (60초마다)
 ├─ 워커: 대기열 확인 시각, 처리 중 작업 ┐        │
 ├─ 봇: getUpdates 성공 시각, 오류 종류  ├─▶ health_state.json ─┐
 └─ 작업 진행 단계·진행률 ──────────────────▶ ytvault.db ────────┤
                                                                ├─▶ healthcheck.py ─▶ Uptime Kuma
                       다운로드 폴더·디스크, token.json ─────────┤      (판정)          (Push)
                       웹 대시보드 (로그인 없이 요청 → 401) ─────┘
```

## 구성 파일

| 파일 | 역할 |
|---|---|
| `health_state.py` | ytvault 안에서 워커·봇의 활동을 `health_state.json`에 기록 (원자적 쓰기) |
| `healthcheck.py` | 상태 파일·DB·디스크·웹을 읽어 판정하고 Kuma로 전송. ytvault와 별개 프로세스 |
| `healthcheck_config.py` | 판정 기준과 설정. 모든 값은 `.env`로 바꿀 수 있다 |
| `deploy/ytvault-healthcheck.service` | 헬스체커를 한 번 실행하는 systemd 서비스 (최소 권한) |
| `deploy/ytvault-healthcheck.timer` | 위 서비스를 60초마다 실행 |
| `tests/test_healthcheck.py`, `tests/test_health_state.py` | 임시 DB와 모의 객체로 만든 자동 테스트 |

## 무엇을 근거로 판단하나

기존에 있던 기록을 최대한 쓰고, 작업이 없을 때는 바뀌지 않아 알 수 없는 것만 새로 기록한다.

| 정보 | 출처 | 비고 |
|---|---|---|
| 작업 큐 (대기·진행·실패 수, 대기 시작 시각) | `jobs.status`, `jobs.updated_at` | 기존 |
| 진행 중 작업의 마지막 진행 시각, 단계, 진행률 | `jobs.updated_at`, `stage`, `progress` | 기존. 진행률이 바뀔 때마다(최소 2초 간격) 갱신된다 |
| 실패 원인 | `jobs.error_msg` | 기존. 메시지로 Drive 인증·용량, 디스크 부족을 구분 |
| 마지막 성공 시각 | `status = 'done'`인 작업의 `updated_at` | 기존 |
| 워커가 대기열을 확인한 시각, 처리 중인 작업 번호, 연속 오류 | `health_state.json`의 `worker` | **신규**. 작업이 없으면 DB가 바뀌지 않아서 추가 |
| 텔레그램 폴링 성공 시각, 연속 오류 수와 종류 | `health_state.json`의 `bot` | **신규**. 메시지가 없어도 롱 폴링은 약 10초마다 성공한다 |
| 디스크 여유 공간, 다운로드 폴더의 파일 | 파일 시스템 | 읽기만 하고 지우지 않는다 |
| Drive 토큰 | `token.json` 존재 여부, 갱신 토큰 유무, 수정 시각 | 네트워크 요청·인증 시험 없음 |
| 웹 대시보드 | `http://127.0.0.1:8000/`에 로그인 없이 요청 | 401(로그인 요구)이면 정상. 새 엔드포인트를 만들지 않았다 |

### ytvault에 추가한 계측 (최소 변경)

- **봇:** getUpdates 전용 HTTP 요청 객체를 감싸 성공 시각과 실패 종류(`auth` / `conflict` / `rate_limit` / `server` / `network`)를 남긴다. 설정은 python-telegram-bot 기본값(연결 1개)과 같다.
- **워커:** 대기열을 확인할 때마다 시각을 남기고, 작업을 시작하고 끝낼 때 작업 번호를 남긴다.
  대기열 확인 중 예외(예: `database is locked`)가 나도 **워커 스레드가 죽지 않고** 5초 뒤 다시 시도하도록 바꿨다. 전에는 이런 예외 한 번에 워커가 조용히 멈췄다.
- **상태 파일:** 임시 파일에 쓰고 `os.replace`로 바꿔 넣는다. 평소에는 15초에 한 번만 쓰고, 작업 시작·종료·오류처럼 상태가 바뀔 때는 바로 쓴다(하루 약 6천 번, 1 KB 미만).
  쓰기에 실패해도 경고만 남기고 다운로드·업로드는 그대로 진행한다. 오류 메시지는 토큰을 가린 뒤 저장한다.

다운로드·업로드 로직, DB 스키마, 웹 인증은 바꾸지 않았다.

## 상태와 Kuma 전송

| 상태 | 뜻 | Kuma |
|---|---|---|
| `HEALTHY` | 작업을 처리 중이고 모두 정상 | up |
| `IDLE` | 처리할 작업이 없고 정상 대기 | up |
| `DEGRADED` | 일부 기능이나 작업 하나에 문제 (주의) | up (`HEALTH_DEGRADED_STATUS=down`이면 down) |
| `UNHEALTHY` | 핵심 기능 장애 | down |

`DEGRADED`는 기본적으로 up으로 보내 알림은 울리지 않고, Kuma의 메시지에서 확인한다.
주의 상태에서도 알림을 받고 싶으면 `.env`에 `HEALTH_DEGRADED_STATUS=down`을 넣는다.

메시지 예:

```
IDLE | no pending jobs | bot OK | worker OK | disk 189GB free
HEALTHY | downloading #68 45% | progress OK | bot OK | queue 1 | disk 180GB free
DEGRADED | 1 failed in 6h (last #70: ERROR: [youtube] x: Video unavailable) | no pending jobs | bot OK | worker OK | queue 0 | disk 189GB free
UNHEALTHY | Drive authentication error (job #71) | no pending jobs | bot OK | worker OK | queue 0 | disk 189GB free
UNHEALTHY | worker stalled 5m | bot OK | queue 3 | disk 189GB free
UNHEALTHY | ytvault not responding 15m (stopped or hung) | web UI not responding (Connection refused) | …
```

ping 값에는 웹 대시보드 응답 시간(ms)을 보낸다.

## 판정 기준

시간은 초, 용량은 GB. 괄호 안의 환경변수로 바꿀 수 있다.

### 텔레그램 봇

| 상황 | 판정 | 기본값 |
|---|---|---|
| 새 메시지가 없음 | 정상 (롱 폴링이 계속 성공하므로) | — |
| 일시적인 연결 오류 뒤 다시 성공 | 정상 | — |
| 마지막 폴링 성공 이후 시간 | 주의 / 장애 | 120 / 300 (`HEALTH_BOT_POLL_WARN` / `_FAIL`) |
| 텔레그램이 토큰을 거부 (401·404) | 장애 (즉시) | — |
| 다른 곳에서 같은 봇을 폴링 (409) | 위 시간 기준에 따라 판정하고 원인을 메시지에 표시 | — |

### 워커와 진행 중 작업

| 상황 | 판정 | 기본값 |
|---|---|---|
| 작업이 없고 5초마다 대기열 확인 | 정상 (`IDLE`) | — |
| 쉬는 워커의 마지막 대기열 확인 이후 시간 | 장애 | 120 (`HEALTH_WORKER_TICK_FAIL`) |
| 대기열 확인 연속 실패 | 1회 이상 주의, 3회 이상 장애 | 3 (`HEALTH_WORKER_ERRORS_FAIL`) |
| 진행 중 작업의 **마지막 진행 이후** 시간 | 주의 / 장애 | 600 / 1800 (`HEALTH_JOB_STALL_WARN` / `_FAIL`) |
| 봇과 워커가 모두 멈춤 | 장애: "ytvault not responding" 한 줄로 표시 | — |
| 워커가 맡지 않은 작업이 진행 중 상태로 남음 | 주의 | 120 |
| 시작 직후 첫 기록 전 | 판정 유예 | 120 (`HEALTH_STARTUP_GRACE`) |

진행 중 작업은 **전체 소요 시간이 아니라 마지막 진행 이후 시간**으로 본다.
7 GB 영상은 다운로드와 업로드에 각각 20분 넘게 걸리지만, 그동안 진행률이 몇 초마다 바뀌므로 정상이다.
파일 크기와 네트워크 속도에 따라 기준을 바꿀 필요가 없다.
멈춰 있어도 되는 구간(병합, Gemini 요약, 403 재시도 대기)은 실측으로 길어야 몇 분이라 10분을 주의 기준으로 잡았다(7 GB 병합 62초).

### 작업 큐와 실패

| 상황 | 판정 | 기본값 |
|---|---|---|
| 워커가 쉬고 있는데 대기 작업이 머묾 | 장애 | 300 (`HEALTH_QUEUE_STUCK`) |
| 워커가 다른 작업을 처리 중이고 대기가 김 | 주의 | 7200 (`HEALTH_QUEUE_WAIT_WARN`) |
| 해상도 선택 대기 (`awaiting_choice`) | 정상 (사람을 기다리는 중) | — |
| 최근 실패 1건 이상 | 주의 | 기간 21600 = 6시간 (`HEALTH_FAILURE_WINDOW`) |
| 마지막 성공 이후, 6시간 안에 연속 실패 | 장애 | 3건 (`HEALTH_FAILURE_STREAK_FAIL`) |
| 마지막 성공 이후 Drive 인증 오류(`invalid_grant` 등) | 장애. 기간과 관계없이 고칠 때까지 유지 | — |
| 위 오류 뒤 `token.json`이 새로 바뀜 | 주의: "token renewed, waiting for next upload" | — |
| 마지막 성공 이후 Drive 용량 초과 (`storageQuotaExceeded`) | 장애 | — |
| `token.json` 없음 | 장애 | — |

실패가 오래되면(6시간) 자동으로 주의에서 빠진다. 단, Drive 인증·용량 오류는 사람이 고치기 전까지 모든 업로드가 실패하므로, 다음 업로드가 성공할 때까지 장애로 남는다.

### 저장장치

| 상황 | 판정 | 기본값 |
|---|---|---|
| 다운로드 폴더가 있는 디스크의 여유 공간 | 주의 / 장애 | 20 / 5 GB (`HEALTH_DISK_FREE_WARN_GB` / `_FAIL_GB`) |
| 디스크 사용률 | 주의 | 90% (`HEALTH_DISK_USAGE_WARN_PCT`) |
| 처리 중인 작업이 없는데 다운로드 폴더에 파일이 있음 | 주의 (파일 수·크기 표시) | — |

병합할 때 최종 파일의 약 2배를 쓴다(4K 30분 영상 7 GB에 13.4 GB, [LINUX_RESOURCES.md](../LINUX_RESOURCES.md)).
20 GB면 4K 장편 1편은 처리할 수 있다. 헬스체커는 **파일을 지우지 않는다.** 정리는 직접 한다.

### 웹 대시보드

| 응답 | 판정 |
|---|---|
| 401 (로그인 요구) | 정상 |
| 연결 안 됨·시간 초과 (5초, `HEALTH_WEB_TIMEOUT`) | 주의 |
| 503 (`WEB_PASSWORD` 비어 있음) | 주의 |
| 200 (로그인 없이 열림) | 주의 |

웹만 문제면 다운로드·업로드는 되므로 장애가 아닌 주의로 본다.
헬스체크용 엔드포인트를 따로 만들지 않았고, 웹 인증 설정도 바꾸지 않았다.

## Uptime Kuma 연동

### 1. Push 모니터 만들기

1. Uptime Kuma(`http://127.0.0.1:3001`, nuc7에서는 SSH 터널 등으로 접속)에서 **새 모니터 추가**.
2. 모니터 유형 **Push**, 이름 `YTVault Health`.
3. **하트비트 간격 180초**. 헬스체커가 60초마다 보내므로, 3번 연속 신호가 없을 때만 down이 된다.
   헬스체커나 타이머가 멈춰도 이 규칙으로 Kuma가 자동으로 down 처리한다.
4. 재시도 횟수는 0~1을 권장한다. 헬스체커가 이미 지속 시간을 따져 판정하므로 여러 번 겹칠 필요가 없다.
5. 알림(텔레그램 등)을 연결하고 저장한다.
6. 화면에 나온 **Push URL**을 복사한다. 예: `http://127.0.0.1:3001/api/push/<토큰>?status=up&msg=OK&ping=`

### 2. `.env`에 Push URL 넣기

```bash
# nuc7의 ~/ytvault/.env
YTVAULT_KUMA_PUSH_URL=http://127.0.0.1:3001/api/push/<토큰>?status=up&msg=OK&ping=
```

- **호스트는 `http://127.0.0.1:3001`로 쓴다.** Kuma 설정의 기본 URL(Primary Base URL)을 Tailscale 주소 등으로 정해 두었으면 Push URL도 그 주소로 나오는데, 헬스체커 서비스는 localhost로만 연결할 수 있어 시간 초과로 실패한다. `/api/push/` 뒤의 토큰은 그대로 두고 앞부분만 바꾼다.
- 뒤의 `?status=...` 부분은 있어도 없어도 된다. 헬스체커가 매번 새로 붙인다.
- **토큰은 비밀번호와 같다.** 누구든 이 URL로 가짜 상태를 보낼 수 있으므로 `.env`에만 두고 소스코드·문서·채팅에 붙이지 않는다. 헬스체커도 로그에는 호스트만 남긴다.
- `.env`는 권한 600으로 둔다.

### 3. 손으로 한 번 실행해 보기

```bash
cd ~/ytvault
.venv/bin/python healthcheck.py --no-push   # 판정만 출력
.venv/bin/python healthcheck.py --json --no-push  # 판정에 쓴 값까지
.venv/bin/python healthcheck.py             # 실제로 Kuma에 보냄
```

종료 코드: `0` 정상 전송, `1` 전송 실패, `2` Push URL 없음. 판정 결과(up/down)와는 관계없다.

### 4. systemd 타이머 설치

`ytvault.service`와 같은 사용자로 실행한다. `<user>`를 서버 사용자 이름으로 바꿔 복사한다.

```bash
cd ~/ytvault
sed "s/<user>/$USER/g" deploy/ytvault-healthcheck.service | sudo tee /etc/systemd/system/ytvault-healthcheck.service >/dev/null
sudo cp deploy/ytvault-healthcheck.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start ytvault-healthcheck.service      # 한 번 실행해 결과 확인
journalctl -u ytvault-healthcheck -n 5 --no-pager
sudo systemctl enable --now ytvault-healthcheck.timer
systemctl list-timers ytvault-healthcheck.timer
```

**순서:** 먼저 이 기능이 들어간 ytvault로 재시작해 `health_state.json`이 생긴 것을 확인한 뒤 타이머를 켠다.
상태 파일이 없으면 "no health state"로 down이 보고된다.

### 서비스의 권한 제한

- ytvault와 같은 사용자, 루트 아님. `NoNewPrivileges`.
- 파일 시스템은 읽기 전용(`ProtectSystem=strict`, `ProtectHome=read-only`). DB도 SQLite 읽기 전용 모드로 연다.
- 네트워크는 localhost만 허용(`IPAddressAllow=localhost`). Kuma가 다른 기기에 있으면 서비스 파일에서 `IPAddressDeny`/`IPAddressAllow` 두 줄을 지운다.
- `CPUQuota=20%`, `MemoryMax=128M`, `Nice=10`, 실행 제한 45초.
- nuc7 실측: 한 번에 **약 0.25초, 메모리 30 MB**. 하루 1440번 실행해도 실행 시간을 다 합쳐 약 6분이다.

## 운영 가이드

| 할 일 | 명령 |
|---|---|
| 지금 상태 보기 | `cd ~/ytvault && .venv/bin/python healthcheck.py --no-push` |
| 최근 판정 기록 | `journalctl -u ytvault-healthcheck -n 20 --no-pager` |
| 타이머 확인 | `systemctl list-timers ytvault-healthcheck.timer` |
| 잠시 끄기 (점검 중) | `sudo systemctl stop ytvault-healthcheck.timer` (Kuma에서도 모니터 일시정지) |
| 다시 켜기 | `sudo systemctl start ytvault-healthcheck.timer` |
| 앱 쪽 기록 보기 | `cat ~/ytvault/health_state.json` |

- **ytvault를 재시작하면** 시작 후 2분 동안은 봇·워커의 첫 기록을 기다려 준다. 재시작 시간이 길면 Kuma에 잠깐 down이 찍힐 수 있다.
- **헬스체커 코드나 기준을 바꾼 뒤**에는 재시작할 것이 없다. 타이머가 매번 새로 실행한다. `.env`의 기준 값도 다음 실행부터 반영된다.
- **ytvault 쪽 계측(`health_state.py`, 워커, 봇)을 바꾼 뒤**에는 `ytvault` 서비스를 재시작해야 한다.

## 문제 해결

| Kuma 메시지 | 원인과 조치 |
|---|---|
| `no health state (missing)` | ytvault가 이 기능이 없는 버전이거나 한 번도 시작하지 않음. 최신 코드로 재시작 |
| `ytvault not responding … (stopped or hung)` | 프로세스가 멈추거나 꺼짐. `systemctl status ytvault`, `journalctl -u ytvault -n 50`. 멈춰 있으면 재시작 |
| `worker stalled` | 워커 스레드만 멈춤(봇은 정상). 로그에서 원인 확인 후 재시작 |
| `worker error xN: …` | 대기열 확인(DB 접근)이 계속 실패. 디스크·DB 파일 권한 확인. 회복되면 자동으로 정상 |
| `bot polling down … (another instance is polling)` | 같은 봇 토큰으로 다른 곳(예: Windows PC)에서도 실행 중. 한 곳만 남긴다 |
| `bot polling down …` | 네트워크·텔레그램 장애. 회복되면 자동으로 정상 |
| `bot token rejected by Telegram` | `TELEGRAM_BOT_TOKEN`이 잘못되었거나 BotFather에서 재발급됨. `.env` 수정 후 재시작 |
| `job #N no progress … / stalled …` | 다운로드·업로드가 멈춤. 웹 UI에서 단계 확인, 로그 확인. 계속 멈춰 있으면 재시작(작업은 대기열로 돌아가 처음부터 다시) |
| `queue not processed` | 워커는 도는데 대기 작업을 안 가져감. 로그 확인 후 재시작 |
| `Drive authentication error (job #N)` | Drive 토큰 만료·취소. [SETUP.md](../SETUP.md#4-google-drive-인증)대로 PC에서 `token.json`을 다시 만들어 복사한 뒤, 웹 UI에서 실패한 작업을 재시도 |
| `…; token renewed, waiting for next upload` | 토큰은 바뀌었고 다음 업로드 성공을 기다리는 중. 실패 작업을 재시도하면 확인된다 |
| `Drive storage full` | Google Drive 용량 부족. 정리하거나 용량 추가 후 재시도 |
| `N jobs failed in a row` | 연속 실패. 웹 UI에서 오류 메시지 확인(yt-dlp 업데이트가 필요한 경우가 많다: `.venv/bin/pip install -U yt-dlp`) |
| `N failed in 6h` | 일시적 실패. 웹 UI에서 재시도. 6시간이 지나면 자동으로 빠진다 |
| `disk low` / `disk … free` | 여유 공간 부족. 다른 서비스(Docker 등)의 사용량도 확인 |
| `leftover files N (…)` | 중단된 작업이 남긴 파일. 처리 중인 작업이 없을 때 `~/ytvault/downloads/`를 확인하고 지운다 |
| `web UI not responding` | 웹 스레드 문제. 다운로드는 계속되지만 재시작 권장 |
| `database unreadable` | DB 파일이 없거나 권한 문제 |
| `healthcheck error: …` | 헬스체커 자체 오류. `healthcheck.py --no-push --json`으로 확인 |
| Kuma에 아무 신호도 없음 (down) | 타이머 확인(`systemctl list-timers`), `journalctl -u ytvault-healthcheck`에서 `Push failed: …` 확인. `.env`의 URL, Kuma 컨테이너 상태(`docker ps`) 확인 |
| `Push failed: Uptime Kuma rejected the push: Monitor not found or not active.` | Push URL 토큰이 틀렸거나 모니터가 일시정지됨 |
| `Push failed: <호스트> unreachable: timed out` (손으로 실행하면 성공) | Push URL이 localhost가 아닌 주소(Tailscale 이름 등). 서비스는 localhost만 허용하므로 `.env`에서 호스트를 `http://127.0.0.1:3001`로 바꾼다 |

전송이 실패해도 헬스체커는 다시 시도하지 않고 끝난다(1분 뒤 다음 실행). ytvault 본체에는 영향이 없다.

## 한계

- 다운로드 중 403 재시도 대기처럼 메모리 안에서만 일어나는 일은 따로 보이지 않는다. 진행이 멈춘 시간으로만 드러난다.
- Drive 연결 상태는 지난 업로드 결과로만 판단한다(시험 업로드·인증 요청을 하지 않는다). 그래서 토큰이 만료된 뒤 첫 업로드가 실패하기 전까지는 알 수 없다.
- 실패 원인 분류는 오류 메시지 문자열에 기대므로, 라이브러리가 메시지를 바꾸면 일반 실패로 분류될 수 있다.

## 테스트

```bash
python -m unittest tests.test_healthcheck tests.test_health_state
```

임시 DB와 임시 폴더, 모의 웹·디스크, 로컬 테스트용 HTTP 서버(가짜 Kuma)만 쓴다. YouTube·Drive·텔레그램·운영 DB에는 접근하지 않는다.

| 시나리오 | 테스트 |
|---|---|
| 정상 대기 | `test_idle` |
| 텔레그램 봇 정상 (메시지 없음, 일시 오류 후 회복) | `test_bot_without_messages_is_ok`, `test_successful_poll_without_messages` |
| 새 다운로드 작업 생성 | `test_new_job_waiting` |
| 다운로드 정상 진행 | `test_download_in_progress` |
| 다운로드 오류 | `test_single_download_error_is_degraded`, `test_repeated_failures_are_unhealthy`, `test_download_stalled` |
| 업로드 정상 진행 | `test_upload_in_progress` |
| 업로드 인증 실패 | `test_drive_auth_failure_is_unhealthy`, `test_drive_auth_failure_stays_until_fixed`, `test_renewed_token_downgrades_auth_failure` |
| 작업 큐 정체 | `test_queue_not_processed`, `test_long_queue_behind_busy_worker_is_only_degraded` |
| Worker 중단 | `test_worker_stopped`, `test_worker_loop_errors`, `test_process_not_running`, `test_db_error_does_not_kill_worker` |
| Uptime Kuma 연결 중단 | `test_kuma_unreachable`, `test_kuma_rejects_push` |
| 장애 복구 후 정상 복귀 | `test_back_to_normal_after_recovery`, `test_tick_after_error_resets_count` |

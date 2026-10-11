# YTVault

텔레그램으로 YouTube 링크를 보내면 자동으로 다운로드 → AI 요약 → Google Drive 업로드 → Notion 기록까지 처리하는 개인용 아카이빙 봇입니다.

## 동작 흐름

1. 텔레그램 봇에 YouTube URL 전송
2. `yt-dlp`로 영상 다운로드 (+ 가능하면 한국어/영어 자막 다운로드)
3. 자막(또는 영상 설명)을 Gemini에 보내 요약 · 태그 · 카테고리 생성
4. 영상 + 요약 정보를 Google Drive 폴더에 업로드
5. (설정 시) Notion 데이터베이스에 기록
6. 텔레그램으로 완료 알림 (Drive/Notion 링크 포함)

작업 상태는 SQLite에 저장되며, 웹 UI(`http://localhost:8000`)에서 전체 작업 목록과 진행 상태를 확인할 수 있습니다.

## 주요 기능

- 텔레그램 채팅으로 YouTube 링크 전송 → 자동 처리 (`watch`, `shorts`, `live`, `youtu.be` 링크 지원)
- 이미 처리된 링크는 중복 다운로드 없이 기존 Drive/Notion 링크 안내 (Drive에서 파일이 삭제된 경우 자동 재다운로드)
- 실패한 작업은 링크를 다시 보내면 재시도
- 1080p보다 높은 해상도가 있는 영상은 텔레그램 버튼으로 저장할 해상도를 선택 (예상 용량 표시, 10분 안에 고르지 않으면 1080p)
- Gemini 기반 요약 / 태그 / 카테고리 자동 생성 (`GEMINI_API_KEY` 설정 시)
- Notion 데이터베이스 연동 (`NOTION_DATABASE_ID` 설정 시, 선택 사항)
- 작업 현황을 볼 수 있는 웹 대시보드 (FastAPI) — 진행 중인 작업의 현재 단계, 진행률, 속도, 남은 시간 표시

## 요구 사항

- Python 3.11+ (yt-dlp가 3.10 지원을 중단 예정)
- JS 런타임 [deno](https://deno.com/) (YouTube 다운로드에 필요, PATH에 있어야 함)
- [ffmpeg](https://ffmpeg.org/) (영상/오디오 병합용, PATH에 있거나 `tools/ffmpeg/ffmpeg.exe`)
- Telegram Bot Token, Google Drive OAuth 클라이언트, (선택) Gemini API Key, (선택) Notion Integration Token

## 설치 및 실행

```bash
pip install -r requirements.txt
cp .env.example .env   # 값 채우기
python main.py
```

- 웹 UI: http://localhost:8000
- 최초 실행 시 Google Drive 인증을 위한 브라우저 창이 열립니다.

환경변수 설정, Google Drive 인증 절차 등 상세 가이드는 [SETUP.md](./SETUP.md)를 참고하세요.

## 서버 배포 (nuc7, Linux)

운영 서버는 `nuc7`(Ubuntu, Tailscale로 접속)이며, systemd 서비스 `ytvault`로 상시 실행합니다.
서버의 `~/ytvault`는 이 저장소를 clone한 것이고, GitHub `master`를 `git pull`해서 갱신합니다.
리소스 사용량 실측은 [LINUX_RESOURCES.md](./LINUX_RESOURCES.md)를 참고하세요.

| 항목 | 값 |
|---|---|
| 코드 / 데이터 | `~/ytvault` (`.env`, `token.json`, `client_secret.json`, `ytvault.db`는 git 밖, 권한 600) |
| 서비스 | `/etc/systemd/system/ytvault.service` |
| 런타임 | Python 3.11 이상(현재 3.14), deno(`~/.deno/bin`), ffmpeg(apt) |
| 웹 UI | `http://nuc7:8000` (`WEB_USERNAME` / `WEB_PASSWORD`로 로그인) |

### 코드 업데이트 배포

1. PC에서 테스트 후 `master`에 push합니다.
   ```bash
   python -m unittest discover -s tests
   git push origin master
   ```
2. 서버에서 받고 테스트합니다.
   ```bash
   ssh nuc7
   cd ~/ytvault
   git pull --ff-only origin master
   .venv/bin/python -m unittest discover -s tests
   ```
   `requirements.txt`가 바뀌었으면 `.venv/bin/pip install -r requirements.txt`도 실행합니다.
3. 처리 중인 작업이 없는지 확인하고 재시작합니다. 웹 UI 상단의 「진행 중」 카드가 0이면 됩니다.
   ```bash
   sudo systemctl restart ytvault
   ```
   - 처리 중에 재시작해도 그 작업은 시작할 때 대기열로 돌아가지만, 처음 단계부터 다시 처리합니다.
   - `web/templates/`만 바뀐 경우에는 재시작하지 않아도 바로 반영됩니다.
4. 정상 기동을 확인합니다.
   ```bash
   systemctl status ytvault
   journalctl -u ytvault -n 20 --no-pager
   ```
   `DB initialized` → `Worker started` → `Telegram bot started` 순서로 찍히고 경고가 없으면 정상입니다.

### 새 서버에 처음 설치

```bash
# 1) 패키지와 deno
sudo apt install -y git python3-venv ffmpeg
curl -fsSL https://deno.land/install.sh | sh -s -- -y --no-modify-path

# 2) 코드와 가상환경
git clone https://github.com/gnghkim/ytvault.git ~/ytvault
cd ~/ytvault
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

3. 비밀 파일을 PC에서 복사합니다: `.env`, `client_secret.json`, `token.json`. 작업 기록을 이어 가려면 `ytvault.db`도 복사합니다.
   - 복사한 뒤 `chmod 600 .env client_secret.json token.json`
   - `.env`에 `WEB_PASSWORD`를 꼭 넣습니다. 비어 있으면 웹 UI가 모든 요청에 503을 돌려줍니다.
   - **`token.json`은 브라우저가 있는 PC에서 만듭니다.** 서버에서는 Google 로그인 창을 띄울 수 없어서, 토큰이 없으면 작업이 오류로 끝납니다.
   - Google Cloud의 OAuth 동의 화면이 "테스트" 상태면 토큰이 7일마다 만료되므로 "프로덕션"으로 바꿔 둡니다.
4. 서비스 파일을 `/etc/systemd/system/ytvault.service`로 만듭니다. `<user>`는 서버 사용자 이름입니다.
   ```ini
   [Unit]
   Description=ytvault YouTube archiver (Telegram bot, worker, web UI)
   Wants=network-online.target
   After=network-online.target
   StartLimitIntervalSec=600
   StartLimitBurst=5

   [Service]
   Type=simple
   User=<user>
   WorkingDirectory=/home/<user>/ytvault
   Environment=PYTHONUNBUFFERED=1
   # yt-dlp는 JS 런타임(deno)과 ffmpeg가 PATH에 있어야 한다
   Environment=PATH=/home/<user>/.deno/bin:/usr/local/bin:/usr/bin:/bin
   ExecStart=/home/<user>/ytvault/.venv/bin/python main.py
   KillSignal=SIGTERM
   TimeoutStopSec=60
   Restart=on-failure
   RestartSec=30

   [Install]
   WantedBy=multi-user.target
   ```
5. **다른 곳(예: Windows PC)에서 돌던 ytvault를 먼저 끕니다.** 같은 텔레그램 봇을 두 곳에서 동시에 폴링하면 충돌합니다.
   그다음 서비스를 켭니다.
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now ytvault
   ```

### 자주 쓰는 명령

| 할 일 | 명령 |
|---|---|
| 상태 확인 | `systemctl status ytvault` |
| 실시간 로그 | `journalctl -u ytvault -f` |
| 최근 경고·오류 | `journalctl -u ytvault -p warning --since today` |
| 재시작 / 중지 | `sudo systemctl restart ytvault` / `sudo systemctl stop ytvault` |

### 헬스 모니터링 (Uptime Kuma)

`ytvault-healthcheck.timer`가 1분마다 봇 폴링, 워커, 작업 큐, Drive 인증 오류, 디스크, 웹 응답을 판정해 Uptime Kuma Push 모니터로 보냅니다.
판정 기준, 설치, 문제 해결은 [docs/HEALTH_MONITORING.md](./docs/HEALTH_MONITORING.md)를 참고하세요.

| 할 일 | 명령 |
|---|---|
| 지금 상태 보기 | `.venv/bin/python healthcheck.py --no-push` |
| 최근 판정 기록 | `journalctl -u ytvault-healthcheck -n 20 --no-pager` |

재시작을 비밀번호 없이 하려면 `sudo visudo -f /etc/sudoers.d/ytvault`로 아래 한 줄을 추가합니다. 이 서비스 재시작만 허용됩니다.

```
<user> ALL=(root) NOPASSWD: /usr/bin/systemctl restart ytvault
```

## 환경변수

| 변수 | 설명 | 필수 |
|------|------|------|
| `TELEGRAM_BOT_TOKEN` | BotFather에서 발급한 봇 토큰 | ✅ |
| `TELEGRAM_ALLOWED_CHAT_IDS` | 사용 허용 chat ID (쉼표 구분, 비우면 전체 허용) | - |
| `GDRIVE_FOLDER_ID` | 업로드 대상 Google Drive 폴더 ID | ✅ |
| `GEMINI_API_KEY` | Gemini API Key (요약/태그/카테고리 생성용) | - |
| `GEMINI_MODEL` | 사용할 Gemini 모델 (기본값 `gemini-2.5-flash`) | - |
| `NOTION_TOKEN` / `NOTION_DATABASE_ID` | Notion 연동 (선택) | - |
| `DOWNLOAD_DIR` | 임시 다운로드 경로 (기본값 `./downloads`) | - |
| `DB_PATH` | SQLite DB 경로 (기본값 `./ytvault.db`) | - |
| `WEB_HOST` / `WEB_PORT` | 웹 대시보드 바인딩 주소/포트 | - |
| `WEB_USERNAME` / `WEB_PASSWORD` | 웹 대시보드 로그인 (사용자 이름 기본값 `ytvault`). 비밀번호가 비어 있으면 웹 UI가 닫힘 | ✅ |
| `YTVAULT_KUMA_PUSH_URL` | 헬스체커가 결과를 보낼 Uptime Kuma Push URL. 판정 기준(`HEALTH_*`)은 [docs/HEALTH_MONITORING.md](./docs/HEALTH_MONITORING.md) | - |

Google Drive 접근에는 `client_secret.json`(OAuth 클라이언트)이 별도로 필요합니다. 자세한 발급 방법은 SETUP.md 참고.

## 프로젝트 구조

```
main.py             # 진입점 — 웹서버 + 워커 + 텔레그램 봇 기동
bot/                 # 텔레그램 봇 (링크 수신, 상태 안내)
worker/              # 다운로드 → 요약 → 업로드 파이프라인
ai/                  # Gemini 요약/태그/카테고리
uploader/            # Google Drive 업로드
notifier/            # Notion 기록
db/                  # SQLite job 상태 관리
web/                 # 작업 현황 대시보드 (FastAPI)
health_state.py      # 워커·봇 활동을 health_state.json에 기록
healthcheck.py       # 헬스체커 (별도 프로세스, Uptime Kuma로 전송)
deploy/              # 헬스체커 systemd 서비스·타이머
```

## 주의사항

- `downloads/`에는 처리 중 임시로 영상 파일이 저장되며, 업로드 완료 후 자동 삭제됩니다. 다운로드된 영상은 저작권이 있는 콘텐츠일 수 있으므로 개인 아카이빙 용도로만 사용하세요.
- `.env`, `client_secret.json`, `token.json`, `ytvault.db`에는 민감 정보가 포함되므로 git에 커밋하지 않습니다 (`.gitignore` 처리됨).

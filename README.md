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
- Gemini 기반 요약 / 태그 / 카테고리 자동 생성 (`GEMINI_API_KEY` 설정 시)
- Notion 데이터베이스 연동 (`NOTION_DATABASE_ID` 설정 시, 선택 사항)
- 작업 현황을 볼 수 있는 웹 대시보드 (FastAPI)

## 요구 사항

- Python 3.10+
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
```

## 주의사항

- `downloads/`에는 처리 중 임시로 영상 파일이 저장되며, 업로드 완료 후 자동 삭제됩니다. 다운로드된 영상은 저작권이 있는 콘텐츠일 수 있으므로 개인 아카이빙 용도로만 사용하세요.
- `.env`, `client_secret.json`, `token.json`, `ytvault.db`에는 민감 정보가 포함되므로 git에 커밋하지 않습니다 (`.gitignore` 처리됨).

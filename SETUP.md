# YTVault 설정 가이드

처음 설치하고 설정하는 방법을 정리한 문서입니다.
리눅스 서버(nuc7)에 상시 실행하는 방법은 [README의 "서버 배포"](./README.md#서버-배포-nuc7-linux)를 참고하세요.

## 1. 준비물

| 항목 | 용도 | Windows | Linux (Ubuntu) |
|---|---|---|---|
| Python 3.11 이상 | 실행 | python.org 설치 파일 | `sudo apt install python3-venv` |
| ffmpeg | 영상·음성 병합 | `python install_ffmpeg.py` (→ `tools/ffmpeg/`) | `sudo apt install ffmpeg` |
| deno | YouTube 다운로드에 필요한 JS 런타임 | `winget install DenoLand.Deno` | `curl -fsSL https://deno.land/install.sh \| sh` |

- ffmpeg는 `tools/ffmpeg/ffmpeg.exe`를 먼저 찾고, 없으면 PATH에서 찾습니다.
- deno는 PATH에 있어야 합니다. node도 쓸 수는 있지만 yt-dlp가 요구하는 버전보다 낮으면 무시되므로 deno를 권장합니다.

## 2. 설치

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   /   Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## 3. 환경변수 (`.env`)

| 변수 | 설명 | 필수 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | 텔레그램 봇 토큰 | ✅ |
| `TELEGRAM_ALLOWED_CHAT_IDS` | 봇을 쓸 수 있는 chat ID (쉼표 구분) | 권장 |
| `GDRIVE_FOLDER_ID` | 업로드할 Google Drive 폴더 ID. 비우면 내 드라이브 최상위 | 권장 |
| `GDRIVE_CLIENT_SECRET_PATH` | OAuth 클라이언트 파일 (기본값 `client_secret.json`) | - |
| `GDRIVE_TOKEN_PATH` | 발급된 토큰 파일 (기본값 `token.json`) | - |
| `GEMINI_API_KEY` | 요약·태그·카테고리 생성. 비우면 요약 없이 저장 | - |
| `GEMINI_MODEL` | Gemini 모델 (기본값 `gemini-2.5-flash`) | - |
| `NOTION_TOKEN` / `NOTION_DATABASE_ID` | Notion 기록. 비우면 건너뜀 | - |
| `WEB_USERNAME` / `WEB_PASSWORD` | 웹 대시보드 로그인 (사용자 이름 기본값 `ytvault`) | ✅ |
| `WEB_HOST` / `WEB_PORT` | 웹 대시보드 주소 (기본값 `0.0.0.0` / `8000`) | - |
| `DOWNLOAD_DIR` / `DB_PATH` | 임시 다운로드 폴더 / SQLite DB (기본값 `downloads` / `ytvault.db`) | - |

경로는 실행 위치와 관계없이 **프로젝트 폴더 기준**입니다. 절대 경로도 쓸 수 있습니다.

### 텔레그램

1. 텔레그램에서 [@BotFather](https://t.me/BotFather)에게 `/newbot`을 보내 봇을 만들고, 받은 토큰을 `TELEGRAM_BOT_TOKEN`에 넣습니다.
2. 만든 봇에게 아무 메시지나 보낸 뒤, 브라우저에서 `https://api.telegram.org/bot<토큰>/getUpdates`를 열면 `"chat":{"id":...}`에서 chat ID를 찾을 수 있습니다.
3. 그 ID를 `TELEGRAM_ALLOWED_CHAT_IDS`에 넣습니다.
   **비워 두면 봇을 찾은 누구나 다운로드를 시킬 수 있으니** 반드시 설정하세요.

### Google Drive 폴더

업로드할 폴더를 Drive에서 열었을 때 주소 `https://drive.google.com/drive/folders/<폴더ID>`의 마지막 부분이 폴더 ID입니다.
그 안에 `YoutubeVault Archive / YYYY-MM / YYYYMMDD_제목 /` 구조로 영상과 `info.md`가 저장됩니다.

### Gemini (선택)

[Google AI Studio](https://aistudio.google.com/apikey)에서 API 키를 만들어 `GEMINI_API_KEY`에 넣습니다.
자막(없으면 영상 설명)을 보내 3~5문장 요약, 태그, 카테고리(Technology / Business / Science / Life / Creative)를 받습니다.

### 웹 대시보드 비밀번호

`WEB_PASSWORD`를 정해 넣습니다. **비어 있으면 웹 UI가 모든 요청에 503을 돌려줍니다**(실수로 열려 있지 않도록 닫힌 상태가 기본).
브라우저에서 접속하면 `WEB_USERNAME` / `WEB_PASSWORD`를 묻습니다.

## 4. Google Drive 인증

### OAuth 클라이언트 만들기 (처음 한 번)

1. [Google Cloud Console](https://console.cloud.google.com)에서 프로젝트를 만들고 **Google Drive API**를 사용 설정합니다.
2. **OAuth 동의 화면**을 구성합니다. 사용자 유형은 "외부"로 하고, 내 Google 계정을 테스트 사용자로 추가합니다.
3. **사용자 인증 정보 → OAuth 클라이언트 ID 만들기**에서 애플리케이션 유형을 **데스크톱 앱**으로 고릅니다.
4. JSON을 내려받아 프로젝트 폴더에 **`client_secret.json`** 이름으로 저장합니다.
5. 동의 화면의 게시 상태를 **"프로덕션"으로 바꿔 둡니다.**
   "테스트" 상태로 두면 토큰이 7일마다 만료되어 업로드가 `invalid_grant` 오류로 실패합니다.
   개인용이라 Google 검토를 받지 않아도 되며, 로그인할 때 "확인되지 않은 앱" 경고가 나오면 계속 진행하면 됩니다.

### 토큰 발급

브라우저가 있는 PC에서 `python main.py`로 처음 실행하고 첫 업로드를 할 때 로그인 창이 열립니다.
허용하면 **`token.json`**이 만들어지고(권한 600), 이후에는 자동으로 갱신됩니다.

- 권한 범위는 `drive.file`입니다. 이 앱이 만든 파일과 폴더만 보고 수정할 수 있고, 다른 Drive 파일에는 접근하지 않습니다.
- **서버처럼 화면이 없는 곳에서는 로그인 창을 띄울 수 없습니다.**
  토큰이 없으면 작업이 "Google Drive 인증 토큰이 없거나 갱신할 수 없습니다" 오류로 끝나니, PC에서 만든 `token.json`을 복사해 쓰세요.

## 5. Notion 연동 (선택)

1. [Notion 통합](https://www.notion.so/profile/integrations)에서 내부 통합을 만들고 토큰을 `NOTION_TOKEN`에 넣습니다.
2. 기록할 데이터베이스 페이지에서 **연결 → 방금 만든 통합**을 추가합니다.
3. 데이터베이스 주소 `https://www.notion.so/<DB ID>?v=...`에서 `?v=` 앞 32자리를 `NOTION_DATABASE_ID`에 넣습니다.
4. 데이터베이스에 아래 속성을 이름과 유형 그대로 만듭니다. 이름이 다르면 기록이 실패합니다(작업 자체는 성공으로 처리).

| 속성 이름 | 유형 | 들어가는 값 |
|---|---|---|
| 제목 | 제목 | 영상 제목 |
| URL | URL | YouTube 링크 |
| 아카이브 링크 | URL | Drive 폴더 링크 |
| 요약 | 텍스트 | Gemini 요약 (최대 2000자) |
| 태그 | 다중 선택 | Gemini 태그 |
| 카테고리 | 선택 | Gemini 카테고리 (없으면 `YouTube`) |
| 출처 유형 | 선택 | `YouTube` |
| 상태 | 상태 | `Inbox` (이 이름의 상태 옵션이 있어야 함) |
| 중요도 | 선택 | `1` |
| 옵시디언 ID | 텍스트 | `ytvault-<작업 번호>` (같은 작업을 다시 처리하면 이 값으로 기존 페이지를 갱신) |

페이지 본문에는 YouTube 영상이 임베드됩니다.

## 6. 실행

```bash
python main.py
```

- 웹 대시보드: `http://localhost:8000`
  - 진행 단계·진행률·남은 시간 표시
  - 상태 카드로 거르기, 검색, 페이지 나눔
  - 실패한 작업 재시도
- 텔레그램 봇에 YouTube 링크(`watch`, `shorts`, `live`, `youtu.be`)를 보내면 처리가 시작됩니다.
  - **1080p보다 높은 해상도가 있는 영상**은 해상도 버튼(예상 용량 포함)이 옵니다. 10분 안에 고르지 않으면 1080p로 저장합니다.
  - 완료되면 Drive·Notion 링크가 텔레그램으로 옵니다.
  - 이미 저장한 링크를 다시 보내면 기존 링크를 알려 주고, 실패했던 링크면 재시도합니다.

테스트는 다음으로 실행합니다.

```bash
python -m unittest discover -s tests
```

## 7. 문제 해결

| 증상 | 원인과 해결 |
|---|---|
| 로그에 `No supported JavaScript runtime could be found` | deno가 없거나 PATH에 없음. 1번 준비물 참고. systemd에서는 서비스 파일의 `Environment=PATH=`에 `~/.deno/bin`을 넣어야 함 |
| `You have requested merging of multiple formats but ffmpeg is not installed` | ffmpeg 설치 필요 |
| 업로드가 `invalid_grant` 오류로 실패 | 토큰 만료·취소. OAuth 동의 화면을 "프로덕션"으로 바꾸고 `token.json`을 지운 뒤 PC에서 다시 발급 |
| "Google Drive 인증 토큰이 없거나 갱신할 수 없습니다" | 화면 없는 환경에서 토큰이 없음. PC에서 만든 `token.json` 복사 |
| 웹 UI가 503 | `WEB_PASSWORD`가 비어 있음 |
| 텔레그램 `Conflict: terminated by other getUpdates request` | 같은 봇 토큰으로 다른 곳에서도 실행 중. 한 곳만 남기고 끄기 |
| YouTube `HTTP Error 403` | 일시적이면 잠시 쉬었다가 자동으로 다시 시도(최대 3번). 계속되면 yt-dlp 업데이트(`pip install -U yt-dlp`). 데이터센터(VPS) IP는 차단이 잦음 |
| 실패한 작업 | 웹 UI의 「↻ 재시도」 버튼, 또는 텔레그램에 같은 링크를 다시 보내기 |

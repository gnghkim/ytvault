# YTVault 설정 가이드

## 1. 패키지 설치

```bash
pip install -r requirements.txt
```

## 2. 환경변수 설정

`.env.example`을 `.env`로 복사 후 편집:

```bash
cp .env.example .env
```

### 필수 항목

| 변수 | 설명 |
|------|------|
| `TELEGRAM_BOT_TOKEN` | BotFather에서 발급한 토큰 |
| `TELEGRAM_ALLOWED_CHAT_IDS` | 허용할 chat ID (쉼표 구분). 비워두면 전체 허용 |
| `GDRIVE_FOLDER_ID` | 업로드 대상 Google Drive 폴더 ID |

### chat ID 확인 방법
봇에게 메시지 보낸 후: `https://api.telegram.org/bot<TOKEN>/getUpdates`

### Google Drive 폴더 ID 확인
Drive 폴더 URL에서: `https://drive.google.com/drive/folders/<폴더ID>`

## 3. Google Drive 인증

1. [Google Cloud Console](https://console.cloud.google.com) → 새 프로젝트 생성
2. Google Drive API 활성화
3. OAuth 2.0 클라이언트 ID 생성 (데스크톱 앱)
4. `credentials.json` 다운로드 → 프로젝트 루트에 저장
5. 첫 실행 시 브라우저 인증 창이 열림 → 인증 완료 후 `token.pickle` 자동 저장

## 4. 실행

```bash
python main.py
```

- 웹 UI: http://localhost:8000
- Telegram 봇에 YouTube URL 전송 → 자동 다운로드 → Drive 업로드

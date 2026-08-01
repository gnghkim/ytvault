"""ffmpeg 포터블 버전을 tools/ffmpeg/ 에 다운로드/설치"""
import io
import os
import urllib.request
import zipfile

URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
DEST = os.path.join(os.path.dirname(__file__), "tools", "ffmpeg")

def download():
    print(f"다운로드 중: {URL}")
    with urllib.request.urlopen(URL, timeout=120) as r:
        total = int(r.headers.get("Content-Length", 0))
        data = bytearray()
        chunk = 1024 * 256
        while True:
            buf = r.read(chunk)
            if not buf:
                break
            data.extend(buf)
            if total:
                print(f"\r  {len(data)/1024/1024:.1f} / {total/1024/1024:.1f} MB", end="", flush=True)
    print()
    return bytes(data)

def extract(data: bytes):
    os.makedirs(DEST, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for member in z.namelist():
            if "/bin/ffmpeg.exe" in member or "/bin/ffprobe.exe" in member:
                filename = os.path.basename(member)
                target = os.path.join(DEST, filename)
                with z.open(member) as src, open(target, "wb") as dst:
                    dst.write(src.read())
                print(f"  추출: {target}")

def verify():
    ffmpeg = os.path.join(DEST, "ffmpeg.exe")
    if os.path.exists(ffmpeg):
        print(f"\n✅ 설치 완료: {ffmpeg}")
    else:
        print("\n❌ ffmpeg.exe 를 찾지 못했습니다.")

if __name__ == "__main__":
    data = download()
    print("압축 해제 중...")
    extract(data)
    verify()

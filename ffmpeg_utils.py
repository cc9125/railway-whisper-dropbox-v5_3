
import os, subprocess, math, tempfile, shutil, uuid, requests

def _run(cmd: list):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{p.stderr}")
    return p.stdout

def download_to_temp(url: str) -> str:
    import re

    # 1) 標準化 Dropbox 分享連結 → 直連主機，避免拿到 HTML 預覽頁
    if "dropbox.com" in url:
        url = re.sub(r"^https?://(www\.)?dropbox\.com", "https://dl.dropboxusercontent.com", url)
        # 直連主機不需要 ?dl=0/1，去掉 query 以免干擾
        url = url.split("?")[0]

    r = requests.get(url, stream=True, timeout=600, allow_redirects=True)
    r.raise_for_status()

    # 2) 檢查 Content-Type：若是 HTML/純文字，直接報錯（不要讓 ffprobe 去撞）
    ctype = (r.headers.get("Content-Type") or "").lower()
    if ("text/html" in ctype) or ("text/plain" in ctype):
        raise RuntimeError(f"URL did not return audio content (Content-Type={ctype}). "
                           f"Use a direct file URL or pass a Dropbox 'path' so server can fetch a temporary link.")

    # 3) 萃取副檔名並存檔
    filename = url.split("/")[-1]
    ext = filename.split(".")[-1] if "." in filename else "wav"
    suffix = f".{ext}"
    fd, path = tempfile.mkstemp(prefix="aud_", suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        for chunk in r.iter_content(1024 * 1024):
            if chunk:
                f.write(chunk)

    # 4) 追加安全檢查：太小很可能是錯誤頁或壞檔
    try:
        sz = os.path.getsize(path)
        if sz < 1024:
            raise RuntimeError(f"Downloaded file too small ({sz} bytes). Not a valid audio.")
    except Exception as e:
        # 清掉檔案避免留垃圾
        try:
            os.remove(path)
        except Exception:
            pass
        raise

    return path

def probe_duration(path: str) -> float:
    cmd = ["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1", path]
    out = _run(cmd).strip()
    return float(out)

def split_with_overlap(input_path: str, out_dir: str, base: str, segment_sec: int, overlap_sec: int, fmt: str = "wav") -> list:
    os.makedirs(out_dir, exist_ok=True)
    dur = probe_duration(input_path)
    step = max(1, segment_sec - max(0, overlap_sec))
    idx = 1
    outputs = []
    start = 0.0
    while start < dur - 0.1:
        t = segment_sec if start + segment_sec <= dur else max(0.1, dur - start)
        name = f"{base}-{idx:03d}.{fmt}"
        out_path = os.path.join(out_dir, name)
        # re-encode to stable mono 16k for whisper friendliness
        cmd = ["ffmpeg","-y","-ss", f"{start:.3f}","-t", f"{t:.3f}","-i", input_path,"-ac","1","-ar","16000", out_path]
        _run(cmd)
        outputs.append(out_path)
        idx += 1
        start += step
    return outputs

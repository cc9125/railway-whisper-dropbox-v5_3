
import os, subprocess, math, tempfile, shutil, uuid, requests

def _run(cmd: list):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{p.stderr}")
    return p.stdout

def download_to_temp(url: str) -> str:
    # normalize Dropbox share ?dl=1
    if "dropbox.com" in url and "dl=" not in url:
        url = url + ("&dl=1" if "?" in url else "?dl=1")
    elif "dropbox.com" in url:
        url = url.replace("dl=0","dl=1")
    r = requests.get(url, stream=True, timeout=600)
    r.raise_for_status()
    suffix = "." + (url.split("?")[0].split("/")[-1].split(".")[-1] or "wav")
    fd, path = tempfile.mkstemp(prefix="aud_", suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        for chunk in r.iter_content(1024*1024):
            if chunk:
                f.write(chunk)
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


from flask import Flask, request, jsonify
import os, json, threading, tempfile
from dropbox_utils import list_changes_safe, get_temporary_link, upload_to_dropbox, ensure_folder, list_folder
from dropbox_utils import api_call, get_access_token, get_temporary_link as get_tmp, list_changes_safe as lcs
from ffmpeg_utils import download_to_temp, split_with_overlap

app = Flask(__name__)

CURSOR_FILE = os.environ.get("CURSOR_FILE", "/app/cursor.json")
LOCK = threading.Lock()

def _read_cursor(key: str):
    try:
        with LOCK:
            if not os.path.exists(CURSOR_FILE):
                return None
            with open(CURSOR_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get(key)
    except Exception:
        return None

def _write_cursor(key: str, value):
    with LOCK:
        data = {}
        if os.path.exists(CURSOR_FILE):
            try:
                with open(CURSOR_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
        with open(CURSOR_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

@app.get("/")
def root():
    return jsonify(ok=True), 200

@app.get("/health")
def health():
    return jsonify(status="ok"), 200

@app.get("/diag")
def diag():
    keys = ["DBX_REFRESH_TOKEN", "DBX_APP_KEY", "DBX_APP_SECRET", "DBX_APP_FOLDER_NAME"]
    present = {k: bool(os.getenv(k)) for k in keys}
    return jsonify(ok=True, env_present=present), 200

@app.post("/list-changes")
def api_list_changes():
    data = request.get_json(force=True) or {}
    path = data.get("path", "")
    recursive = bool(data.get("recursive", True))
    cursor = data.get("cursor")
    limit = int(data.get("limit", 2000))
    try:
        res = list_changes_safe(path, recursive, cursor, limit)
        return jsonify(res), 200
    except Exception as e:
        return jsonify(error="list_changes_failed", detail=str(e)), 502

@app.post("/shared-link")
def api_shared_link():
    data = request.get_json(force=True) or {}
    path = data.get("path")
    prefer_tmp = bool(data.get("temporary", True))
    if not path or not isinstance(path, str):
        return jsonify(error="Missing 'path'"), 400
    try:
        # Prefer temporary link for direct download
        tmp = api_call("files/get_temporary_link", {"path": path})
        link = tmp.get("link")
        if link:
            return jsonify({"url": link, "kind": "temporary"}), 200
    except Exception as e:
        pass
    # fallback: shared link (may require sharing scopes)
    try:
        res = api_call("sharing/list_shared_links", {"path": path, "direct_only": True})
        links = res.get("links") or []
        if links:
            url = links[0].get("url","")
            if "dl=0" in url: url = url.replace("dl=0","dl=1")
            elif "dl=1" not in url: url = url + ("&dl=1" if "?" in url else "?dl=1")
            return jsonify({"url": url, "kind": "shared_existing"}), 200
    except Exception as e:
        pass
    try:
        res = api_call("sharing/create_shared_link_with_settings", {"path": path, "settings": {"audience":"public","access":"viewer","allow_download": True}})
        url = res.get("url","")
        if "dl=0" in url: url = url.replace("dl=0","dl=1")
        elif "dl=1" not in url: url = url + ("&dl=1" if "?" in url else "?dl=1")
        return jsonify({"url": url, "kind": "shared_created"}), 200
    except Exception as e:
        return jsonify(error="shared_link_failed", detail=str(e)), 502

@app.post("/cursor/get")
def cursor_get():
    data = request.get_json(silent=True) or {}
    key = data.get("key", "default")
    val = _read_cursor(key)
    return jsonify({"cursor": val}), 200

@app.post("/cursor/set")
def cursor_set():
    data = request.get_json(force=True) or {}
    key = data.get("key", "default")
    val = data.get("cursor")
    _write_cursor(key, val)
    return jsonify({"ok": True}), 200

# -------- FFmpeg split + Dropbox upload helpers --------

def _pick_dir(dest_root: str, max_dirs: int, max_files_per_dir: int):
    """
    Ensure subfolders 01..NN exist. Return the first dir with < max_files_per_dir files.
    Create next dir up to max_dirs if all are full.
    """
    for i in range(1, max_dirs+1):
        sub = f"{i:02d}"
        folder = f"{dest_root.rstrip('/')}/{sub}"
        ensure_folder(folder)
        entries = list_folder(folder)
        file_count = sum(1 for e in entries if e.get('.tag') == 'file')
        if file_count < max_files_per_dir:
            return folder
    # if all full, use the last one (rolling)
    return f"{dest_root.rstrip('/')}/{max_dirs:02d}"

@app.post("/split-audio-upload")
def split_audio_upload():
    """
    Download an audio by URL, split with overlap, upload pieces to Dropbox
    distributed into dest_root/01..NN with cap per dir.
    Body:
    {
      "url": "...",
      "segment_time": 400,
      "overlap_seconds": 10,
      "format": "wav",
      "dest_root": "/音檔",
      "group_prefix": "meetingA",
      "max_dirs": 5,
      "max_files_per_dir": 5
    }
    """
    data = request.get_json(force=True) or {}
    url = data.get("url")
    segment_time = int(data.get("segment_time", 400))
    overlap = int(data.get("overlap_seconds", 10))
    fmt = data.get("format","wav").lower()
    dest_root = data.get("dest_root","/音檔")
    group_prefix = data.get("group_prefix","meeting")
    max_dirs = int(data.get("max_dirs",5))
    max_files = int(data.get("max_files_per_dir",5))
    path = data.get("path")

    if not url and path:
        # 以 Dropbox 路徑換真正可下載的臨時連結（最穩）
        try:
            tmp_link = api_call("files/get_temporary_link", {"path": path})
            url = tmp_link.get("link")
        except Exception as e:
            return jsonify(error="cannot_get_temporary_link", detail=str(e)), 502

    if not url:
        return jsonify(error="Missing 'url' or 'path'"), 400
    
    tmp = None
    tmpdir = tempfile.mkdtemp(prefix="split_")
    try:
        tmp = download_to_temp(url)
        pieces = split_with_overlap(tmp, tmpdir, group_prefix, segment_time, overlap, fmt)
        ensure_folder(dest_root)
        uploaded = []
        for p in pieces:
            subdir = _pick_dir(dest_root, max_dirs, max_files)
            dbx_path = f"{subdir}/{os.path.basename(p)}"
            upload_to_dropbox(p, dbx_path)
            uploaded.append(dbx_path)
        return jsonify({"uploaded": uploaded, "count": len(uploaded)}), 200
    except Exception as e:
        return jsonify(error="split_audio_upload_failed", detail=str(e)), 500
    finally:
        try:
            if tmp and os.path.exists(tmp): os.remove(tmp)
            if os.path.exists(tmpdir):
                for f in os.listdir(tmpdir):
                    try: os.remove(os.path.join(tmpdir,f))
                    except: pass
                os.rmdir(tmpdir)
        except Exception:
            pass

@app.post("/ensure-slices")
def ensure_slices():
    """
    If /01..NN already contain files for the given group_prefix, skip.
    Otherwise, split from source URL and upload as /{dest_root}/01..NN/group_prefix-###.ext
    Same body as /split-audio-upload.
    """
    data = request.get_json(force=True) or {}
    url = data.get("url")
    dest_root = data.get("dest_root","/音檔")
    group_prefix = data.get("group_prefix","meeting")
    max_dirs = int(data.get("max_dirs",5))
    max_files = int(data.get("max_files_per_dir",5))
    segment_time = int(data.get("segment_time", 400))
    overlap = int(data.get("overlap_seconds", 10))
    fmt = data.get("format","wav").lower()
    path = data.get("path")
    if not url and path:
        # 以 Dropbox 路徑換真正可下載的臨時連結（最穩）
        try:
            tmp_link = api_call("files/get_temporary_link", {"path": path})
            url = tmp_link.get("link")
        except Exception as e:
            return jsonify(error="cannot_get_temporary_link", detail=str(e)), 502

    if not url:
        return jsonify(error="Missing 'url' or 'path'"), 400
    
    # check if already has this group's files anywhere in 01..NN
    try:
        ensure_folder(dest_root)
        found = False
        for i in range(1, max_dirs+1):
            folder = f"{dest_root.rstrip('/')}/{i:02d}"
            ensure_folder(folder)
            entries = list_folder(folder)
            for e in entries:
                if e.get('.tag') == 'file' and e.get('name','').startswith(group_prefix + "-"):
                    found = True
                    break
            if found: break
        if found:
            return jsonify({"skipped": True, "reason": "group_exists"}), 200
    except Exception as e:
        # proceed anyway
        pass

    # otherwise, split and upload
    tmp = None
    tmpdir = tempfile.mkdtemp(prefix="split_")
    try:
        tmp = download_to_temp(url)
        pieces = split_with_overlap(tmp, tmpdir, group_prefix, segment_time, overlap, fmt)
        uploaded = []
        for p in pieces:
            subdir = _pick_dir(dest_root, max_dirs, max_files)
            dbx_path = f"{subdir}/{os.path.basename(p)}"
            upload_to_dropbox(p, dbx_path)
            uploaded.append(dbx_path)
        return jsonify({"uploaded": uploaded, "count": len(uploaded)}), 200
    except Exception as e:
        return jsonify(error="ensure_slices_failed", detail=str(e)), 500
    finally:
        try:
            if tmp and os.path.exists(tmp): os.remove(tmp)
            if os.path.exists(tmpdir):
                for f in os.listdir(tmpdir):
                    try: os.remove(os.path.join(tmpdir,f))
                    except: pass
                os.rmdir(tmpdir)
        except Exception:
            pass

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

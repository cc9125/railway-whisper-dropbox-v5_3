
import os, json, requests, time
from typing import Optional

DBX_API_BASE = "https://api.dropboxapi.com/2"
DBX_CONTENT_BASE = "https://content.dropboxapi.com/2"

class DropboxConfigError(Exception):
    pass

def _env(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise DropboxConfigError(f"Missing environment variable: {name}")
    return v

def get_access_token() -> str:
    app_key = _env("DBX_APP_KEY")
    app_secret = _env("DBX_APP_SECRET")
    refresh_token = _env("DBX_REFRESH_TOKEN")
    r = requests.post(
        "https://api.dropbox.com/oauth2/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        auth=(app_key, app_secret),
        timeout=30,
    )
    if r.status_code != 200:
        raise requests.HTTPError(f"{r.status_code} {r.reason}: {r.text}")
    return r.json()["access_token"]

def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=True)

def api_call(endpoint: str, payload: dict, use_content: bool = False) -> dict:
    token = get_access_token()
    base = DBX_CONTENT_BASE if use_content else DBX_API_BASE
    url = f"{base}/{endpoint.lstrip('/')}"
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=_json(payload),
        timeout=60,
    )
    if r.status_code != 200:
        raise requests.HTTPError(f"{r.status_code} {r.reason}: {r.text}")
    return r.json()

def upload_to_dropbox(local_path: str, dropbox_path: str, retries: int = 3, backoff: float = 1.0):
    """
    Uploads a file to Dropbox using /files/upload (binary). Overwrites if exists.
    """
    last_err = None
    for attempt in range(1, retries+1):
        token = get_access_token()
        with open(local_path, "rb") as f:
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/octet-stream",
                "Dropbox-API-Arg": _json({"path": dropbox_path, "mode": "overwrite", "autorename": False, "mute": True})
            }
            r = requests.post(f"{DBX_CONTENT_BASE}/files/upload", headers=headers, data=f, timeout=600)
        if r.status_code == 200:
            return
        last_err = f"{r.status_code} {r.reason}: {r.text}"
        # retry on transient
        if r.status_code in (429,500,502,503,504) and attempt < retries:
            time.sleep(backoff * attempt)
            continue
        raise requests.HTTPError(last_err)

def ensure_folder(path: str):
    try:
        api_call("files/create_folder_v2", {"path": path, "autorename": False})
    except requests.HTTPError as e:
        # ignore already_exists
        if "path/conflict/folder" in str(e) or "conflict" in str(e) or "already_exists" in str(e):
            return
        # if not_found on parents, try to create parents recursively
        if "path/not_found" in str(e):
            parent = path.rsplit("/",1)[0]
            if parent and parent != "":
                ensure_folder(parent)
                api_call("files/create_folder_v2", {"path": path, "autorename": False})
                return
        raise

def list_folder(path: str):
    res = api_call("files/list_folder", {"path": path, "recursive": False, "limit": 2000})
    entries = res.get("entries", [])
    return entries

def get_temporary_link(path: str) -> Optional[str]:
    try:
        r = api_call("files/get_temporary_link", {"path": path})
        return r.get("link")
    except Exception:
        return None

def list_changes_safe(path: str, recursive: bool, cursor: Optional[str], limit: int) -> dict:
    app_name = os.getenv("DBX_APP_FOLDER_NAME", "").strip()
    if cursor:
        res = api_call("files/list_folder/continue", {"cursor": cursor})
        res["normalized_path"] = None
        res["mode"] = "continue"
        return res
    # try as-is
    try:
        res = api_call("files/list_folder", {"path": path, "recursive": recursive, "limit": limit})
        res["normalized_path"] = path
        res["mode"] = "app_folder"
        return res
    except requests.HTTPError as e:
        if "path/not_found" in str(e) and app_name:
            alt = path
            if not alt.startswith("/Apps/"):
                alt = f"/Apps/{app_name}{('/' + path.lstrip('/')) if path else ''}"
            res = api_call("files/list_folder", {"path": alt, "recursive": recursive, "limit": limit})
            res["normalized_path"] = alt
            res["mode"] = "full_dropbox"
            return res
        raise

# Railway Dropbox Helper API v5.3 (Unicode-safe, FFmpeg split)

## New endpoints
- `POST /split-audio-upload`
  ```json
  {
    "url": "https://www.dropbox.com/scl/fi/.../meeting.WAV?dl=0",
    "segment_time": 400,
    "overlap_seconds": 10,
    "format": "wav",
    "dest_root": "/音檔",
    "group_prefix": "meetingA",
    "max_dirs": 5,
    "max_files_per_dir": 5
  }
  ```
  - 會下載原始音檔 → 以 `segment_time` 切片並套 `overlap_seconds` 重疊 → 轉成 mono 16k →
    上傳到 `/音檔/01..05`，每個子目錄最多 `max_files_per_dir` 個檔。

- `POST /ensure-slices`（若 01..NN 已有 `group_prefix-###.*` 就 **skip**）
  - Body 同上；用於你在 Make 端要「先檢查、再切」的情境。

## 既有端點
- `GET /health`、`GET /diag`
- `POST /list-changes`（自動判斷 App-folder vs Full Dropbox 路徑）
- `POST /shared-link`（臨時連結優先）
- `POST /cursor/get`、`POST /cursor/set`

## Env
- `DBX_APP_KEY`, `DBX_APP_SECRET`, `DBX_REFRESH_TOKEN`
- `DBX_APP_FOLDER_NAME`（App-folder 型才需要，用於 fallback）
- `CURSOR_FILE`（可選，預設 `/app/cursor.json`）

## Deploy
- Healthcheck Path 設 `/health`
- 使用 Dockerfile 自帶的 gunicorn 指令即可

# Kta3Box Creator Studio

## Run locally
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```
Open `http://127.0.0.1:8080`.

## Railway
- Push the project files to GitHub and connect the repository to Railway.
- Set the start command to `gunicorn --workers 1 --threads 8 --timeout 1800 -b 0.0.0.0:$PORT app:app`.
- Add a Railway Volume mounted at `/app/data` so project history and generated videos persist across redeployments.
- Set `KTA3BOX_DATA=/app/data`.
- Set `SECRET_KEY` to a long random value so anonymous browser workspaces remain valid across restarts.


FFmpeg is supplied by the `imageio-ffmpeg` Python package; no system-level FFmpeg installation or `nixpacks.toml` is required. `yt-dlp` uses that bundled executable to merge streams and render vertical clips.

## Notes
- Login and registration have been removed. The app creates an anonymous browser workspace automatically, so each browser has its own project history without entering a username or password.
- The project detects candidate moments using audio intensity and scene-change density. This is heuristic highlight selection, not a guarantee of virality.
- Suggested posting windows are generic starting points, not live audience analytics. The app does not publish automatically.
- Only download and reuse content you own or have permission to use.
- Railway's default filesystem is ephemeral. A persistent volume is required to retain videos and database records across deployments.


## Video source notes

You can paste a supported public video URL or upload a local video file (MP4, MOV, MKV, WEBM, M4V, AVI; up to 600 MB). Direct upload is the recommended fallback when a hosting provider blocks automated requests. YouTube may block requests from Railway IP addresses with an anti-bot challenge; this cannot be reliably fixed by changing the UI or retrying the same request. The app reports this clearly and lets you upload a video you own or have permission to use.

The workspace does not require an account. An anonymous browser session keeps each visitor’s project history separate without asking for login details.

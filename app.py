import os
import re
import json
import time
import uuid
import math
import sqlite3
import hashlib
import secrets
import threading
import subprocess
import struct
import shutil
from pathlib import Path
from datetime import datetime, timezone
from datetime import timedelta
from werkzeug.utils import secure_filename

import yt_dlp
import imageio_ffmpeg
from flask import Flask, request, jsonify, session, send_file, send_from_directory

BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("KTA3BOX_DATA", BASE / "data"))
DOWNLOADS = DATA / "downloads"
UPLOADS = DATA / "uploads"
DB = DATA / "kta3box.db"
DATA.mkdir(parents=True, exist_ok=True)
DOWNLOADS.mkdir(parents=True, exist_ok=True)
UPLOADS.mkdir(parents=True, exist_ok=True)
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
JOBS = {}
LOCK = threading.RLock()

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.permanent_session_lifetime = timedelta(days=180)
app.config["MAX_CONTENT_LENGTH"] = 600 * 1024 * 1024


def db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            title TEXT,
            thumbnail TEXT,
            platform TEXT,
            status TEXT NOT NULL,
            clips_json TEXT NOT NULL DEFAULT '[]',
            hashtags_json TEXT NOT NULL DEFAULT '[]',
            schedule_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )""")


init_db()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def current_workspace_id():
    workspace_id = session.get("workspace_id")
    if workspace_id:
        with db() as con:
            exists = con.execute("SELECT id FROM users WHERE id=?", (workspace_id,)).fetchone()
        if exists:
            return workspace_id
    username = "guest-" + uuid.uuid4().hex
    with db() as con:
        cur = con.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                          (username, "anonymous-session", now_iso()))
        workspace_id = cur.lastrowid
    session["workspace_id"] = workspace_id
    session.permanent = True
    return workspace_id


def save_job(job):
    with db() as con:
        con.execute("""INSERT OR REPLACE INTO jobs
            (id,user_id,url,title,thumbnail,platform,status,clips_json,hashtags_json,schedule_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (job["id"], job["user_id"], job["url"], job.get("title", ""),
             job.get("thumbnail", ""), job.get("platform", "youtube"),
             job.get("status", "processing"), json.dumps(job.get("clips", [])),
             json.dumps(job.get("hashtags", [])), json.dumps(job.get("schedule", {})),
             job.get("created_at", now_iso())))


def ffmpeg(args, timeout=900):
    return subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y"] + args,
                          capture_output=True, text=True, timeout=timeout)


def safe_url(url):
    return bool(re.match(r"^https?://", url, re.I)) and any(
        host in url.lower() for host in ("youtube.com", "youtu.be", "tiktok.com", "instagram.com"))


def get_info(url):
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 30, "retries": 3}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def download_video(job, folder):
    info = get_info(job["url"])
    job["title"] = info.get("title") or "Untitled video"
    job["thumbnail"] = info.get("thumbnail") or ""
    job["duration"] = float(info.get("duration") or 0)
    job["uploader"] = info.get("uploader") or ""
    job["source_tags"] = info.get("tags") or []
    if job["duration"] < 12:
        raise RuntimeError("The source video is too short to create useful highlights.")
    job["stage"] = "download"
    job["message"] = "Downloading source video"
    job["progress"] = 8
    fmt = "best[ext=mp4]/best"
    opts = {
        "format": fmt,
        "outtmpl": str(folder / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 45,
        "retries": 3,
        "ffmpeg_location": FFMPEG,
        "progress_hooks": [lambda d: job.update(progress=8 + int(22 * ((d.get("downloaded_bytes") or 0) / max(d.get("total_bytes") or d.get("total_bytes_estimate") or 1, 1)))) if d.get("status") == "downloading" else None]
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([job["url"]])
    files = list(folder.glob("source.*"))
    files = [p for p in files if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".m4v")]
    if not files:
        raise RuntimeError("The video download did not produce a supported video file.")
    return max(files, key=lambda p: p.stat().st_size)


def audio_energy(video_path):
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(video_path),
           "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"]
    try:
        raw = subprocess.run(cmd, capture_output=True, timeout=180).stdout
        values = []
        step = 8000 * 2
        for pos in range(0, len(raw) - step, step):
            samples = struct.unpack("<" + "h" * 8000, raw[pos:pos + step])
            rms = math.sqrt(sum(x * x for x in samples) / len(samples))
            values.append(rms / 32768.0)
        return values
    except Exception:
        return []


def scene_times(video_path):
    cmd = [FFMPEG, "-hide_banner", "-i", str(video_path), "-vf",
           "select='gt(scene,0.28)',showinfo", "-an", "-f", "null", "-"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        return [float(x) for x in re.findall(r"pts_time:([0-9.]+)", result.stderr)]
    except Exception:
        return []


def choose_highlights(video_path, duration, count, target, job):
    job["stage"] = "analyze"
    job["message"] = "Analyzing audio peaks and visual scene changes"
    job["progress"] = 34
    energy = audio_energy(video_path)
    scenes = scene_times(video_path)
    max_start = max(0, duration - target)
    candidates = []
    stride = max(2, min(5, target // 6))
    for start in range(0, int(max_start) + 1, stride):
        end = min(duration, start + target)
        a = max(0, int(start))
        b = min(len(energy), int(end))
        section = energy[a:b]
        avg = sum(section) / len(section) if section else 0
        peak = max(section) if section else 0
        cuts = sum(1 for t in scenes if start <= t <= end)
        score = avg * 1.8 + peak * 1.2 + min(cuts, 8) * 0.045
        candidates.append({"start": float(start), "end": float(end), "score": score})
    candidates.sort(key=lambda x: x["score"], reverse=True)
    selected = []
    gap = max(5, target * 0.35)
    for item in candidates:
        if all(item["end"] + gap < old["start"] or item["start"] > old["end"] + gap for old in selected):
            selected.append(item)
        if len(selected) >= count:
            break
    if len(selected) < count:
        for i in range(count):
            start = max(0, min(max_start, (duration - target) * (i + 1) / (count + 1)))
            if all(abs(start - x["start"]) > target * 0.6 for x in selected):
                selected.append({"start": start, "end": min(duration, start + target), "score": 0})
            if len(selected) >= count:
                break
    selected.sort(key=lambda x: x["start"])
    return selected[:count]


def make_sound(folder):
    sound = folder / "intro-impact.wav"
    if sound.exists():
        return sound
    args = ["-f", "lavfi", "-i", "anoisesrc=color=pink:amplitude=0.32:duration=0.65:sample_rate=44100",
            "-f", "lavfi", "-i", "sine=frequency=72:duration=0.65:sample_rate=44100",
            "-filter_complex",
            "[0:a]highpass=f=500,lowpass=f=6500,afade=t=in:st=0:d=0.03,afade=t=out:st=0.16:d=0.48[n];"
            "[1:a]lowpass=f=150,afade=t=in:st=0:d=0.02,afade=t=out:st=0.08:d=0.55[b];"
            "[n][b]amix=inputs=2:duration=first:normalize=0,volume=1.5[out]",
            "-map", "[out]", "-ar", "44100", "-ac", "2", str(sound)]
    ffmpeg(args, timeout=60)
    return sound if sound.exists() else None


def render_clip(source, segment, output, sound, job):
    start = segment["start"]
    duration = segment["end"] - segment["start"]
    width, height = (1080, 1920)
    video_filter = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
    args = ["-ss", f"{start:.3f}", "-i", str(source)]
    if sound:
        args += ["-i", str(sound)]
        if job.get("has_audio", True):
            audio_filter = "[0:a]volume=0.88[orig];[1:a]adelay=0|0,volume=0.38[sfx];[orig][sfx]amix=inputs=2:duration=first:dropout_transition=0,loudnorm=I=-14:TP=-1.5:LRA=11[a]"
        else:
            audio_filter = "[1:a]volume=0.55,loudnorm=I=-14:TP=-1.5:LRA=11[a]"
        maps = ["-filter_complex", f"[0:v]{video_filter}[v];{audio_filter}", "-map", "[v]", "-map", "[a]"]
    else:
        maps = ["-vf", video_filter, "-map", "0:v:0", "-map", "0:a?"]
    args += maps + ["-t", f"{duration:.3f}", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "22", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                    "-movflags", "+faststart", str(output)]
    result = ffmpeg(args, timeout=1200)
    if result.returncode != 0 or not output.exists() or output.stat().st_size < 12000:
        raise RuntimeError("Clip rendering failed: " + (result.stderr[-700:] if result.stderr else "unknown error"))
    return output


def build_hashtags(title, uploader, tags, platform):
    stop = {"the", "and", "for", "with", "from", "this", "that", "your", "you", "are", "was", "have", "has"}
    words = re.findall(r"[\w]{3,}", " ".join([title or "", uploader or ""] + list(tags or [])).lower())
    ranked = []
    for word in words:
        if word not in stop and not word.isdigit() and word not in ranked:
            ranked.append(word)
    platform_tags = {
        "youtube": ["shorts", "youtubeshorts", "shortsfeed"],
        "tiktok": ["fyp", "foryou", "tiktok"],
        "instagram": ["reels", "instagramreels", "reelsvideo"]
    }.get(platform, ["shorts"])
    return ["#" + x for x in (ranked[:10] + platform_tags)][:14]


def schedule_suggestions(platform):
    slots = {
        "youtube": [("Tuesday", "17:00–19:00"), ("Thursday", "17:00–20:00"), ("Saturday", "10:00–12:00")],
        "tiktok": [("Tuesday", "18:00–21:00"), ("Thursday", "18:00–21:00"), ("Sunday", "16:00–19:00")],
        "instagram": [("Wednesday", "11:00–13:00"), ("Friday", "16:00–19:00"), ("Sunday", "18:00–20:00")]
    }
    return {"timezone": "Use your audience's local timezone", "slots": slots.get(platform, slots["youtube"]),
            "note": "Starting-point recommendations only. Refine them using your own audience analytics."}


def prepare_uploaded_video(job, folder):
    original = Path(job["upload_path"])
    if not original.is_file():
        raise RuntimeError("The uploaded video is no longer available. Please upload it again.")
    suffix = original.suffix.lower()
    if suffix not in (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"):
        raise RuntimeError("Unsupported video format. Use MP4, MOV, MKV, WEBM or M4V.")
    source = folder / ("source" + suffix)
    shutil.move(str(original), str(source))
    probe = subprocess.run([FFMPEG, "-hide_banner", "-i", str(source)], capture_output=True, text=True, timeout=45)
    raw = (probe.stderr or "")
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", raw)
    if not match:
        raise RuntimeError("Could not read this video. Try exporting it as MP4 (H.264).")
    duration = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
    if duration < 12:
        raise RuntimeError("The source video is too short to create useful highlights.")
    job["duration"] = duration
    job["title"] = Path(job.get("original_filename") or "Uploaded video").stem[:120]
    job["thumbnail"] = ""
    job["uploader"] = ""
    job["source_tags"] = []
    return source


def friendly_error(exc):
    message = str(exc)
    lowered = message.lower()
    if "sign in to confirm you're not a bot" in lowered or "confirm you're not a bot" in lowered:
        return ("YouTube is blocking requests from the hosting server (Railway). This is not a problem with your video. "
                "Use the Upload video option to process a video file directly, or try a supported public link from another platform.")
    if "http error 403" in lowered or "status code 403" in lowered:
        return "The video provider refused the hosting server's request. Upload the video file directly or try another supported source."
    return message[:500] or "The video could not be processed."

def process_job(job):
    folder = DOWNLOADS / job["id"]
    folder.mkdir(parents=True, exist_ok=True)
    try:
        source = prepare_uploaded_video(job, folder) if job.get("upload_path") else download_video(job, folder)
        probe = subprocess.run([FFMPEG, "-hide_banner", "-i", str(source)], capture_output=True, text=True, timeout=45)
        job["has_audio"] = "Audio:" in (probe.stderr or "")
        duration = job.get("duration") or 0
        target = job["target"]
        if duration and duration < target:
            target = max(8, int(duration * 0.7))
        segments = choose_highlights(source, duration, job["count"], target, job)
        job["stage"] = "render"
        job["message"] = "Creating vertical highlights and adding an opening impact sound"
        job["progress"] = 58
        sound = make_sound(folder) if job["sound"] else None
        clips = []
        for index, segment in enumerate(segments, 1):
            output = folder / f"highlight-{index:02d}.mp4"
            render_clip(source, segment, output, sound, job)
            clips.append({
                "name": output.name,
                "url": f"/api/media/{job['id']}/{output.name}",
                "download": f"/api/media/{job['id']}/{output.name}?download=1",
                "start": round(segment["start"]),
                "end": round(segment["end"]),
                "score": round(segment.get("score", 0), 3)
            })
            job["progress"] = 58 + int(38 * index / max(len(segments), 1))
        job["clips"] = clips
        job["hashtags"] = build_hashtags(job.get("title"), job.get("uploader"), job.get("source_tags"), job["platform"])
        job["schedule"] = schedule_suggestions(job["platform"])
        job["stage"] = "done"
        job["status"] = "done"
        job["message"] = "Your highlights are ready"
        job["progress"] = 100
        save_job(job)
    except Exception as exc:
        job["stage"] = "error"
        job["status"] = "error"
        job["error"] = friendly_error(exc)
        job["message"] = "The job could not be completed"
        save_job(job)
    finally:
        job["finished_at"] = now_iso()


@app.get("/")
def index():
    return send_from_directory(BASE, "index.html")


@app.post("/api/create")
def create():
    upload = request.files.get("video")
    if upload and upload.filename:
        original_filename = secure_filename(upload.filename)
        extension = Path(original_filename).suffix.lower()
        if extension not in (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"):
            return jsonify({"error": "Unsupported video format. Use MP4, MOV, MKV, WEBM or M4V."}), 400
        platform = request.form.get("platform", "youtube")
        raw_count = request.form.get("count", 3)
        raw_duration = request.form.get("duration", 30)
        sound = request.form.get("sound", "true").lower() == "true"
        jid = uuid.uuid4().hex[:12]
        upload_path = UPLOADS / f"{jid}{extension}"
        upload.save(upload_path)
        url = "uploaded://" + original_filename
    else:
        data = request.get_json(silent=True) or {}
        url = (data.get("url") or "").strip()
        if not safe_url(url):
            return jsonify({"error": "Paste a supported YouTube, TikTok or Instagram URL, or upload a video file."}), 400
        platform = data.get("platform", "youtube")
        raw_count = data.get("count", 3)
        raw_duration = data.get("duration", 30)
        sound = bool(data.get("sound", True))
        jid = uuid.uuid4().hex[:12]
        upload_path = None
        original_filename = ""
    if platform not in ("youtube", "tiktok", "instagram"):
        platform = "youtube"
    try:
        count = max(1, min(6, int(raw_count)))
        target = max(12, min(60, int(raw_duration)))
    except (TypeError, ValueError):
        if upload_path:
            upload_path.unlink(missing_ok=True)
        return jsonify({"error": "Invalid clip settings."}), 400
    workspace_id = current_workspace_id()
    job = {"id": jid, "user_id": workspace_id, "url": url, "platform": platform,
           "count": count, "target": target, "sound": sound,
           "stage": "queued", "status": "processing", "progress": 2,
           "message": "Added to the processing queue", "clips": [], "hashtags": [],
           "schedule": {}, "created_at": now_iso()}
    if upload_path:
        job["upload_path"] = str(upload_path)
        job["original_filename"] = original_filename
    with LOCK:
        JOBS[jid] = job
    threading.Thread(target=process_job, args=(job,), daemon=True).start()
    return jsonify({"id": jid})


@app.get("/api/status/<jid>")
def status(jid):
    with LOCK:
        job = JOBS.get(jid)
    workspace_id = current_workspace_id()
    if job and job["user_id"] == workspace_id:
        return jsonify({k: job.get(k) for k in ("id", "stage", "status", "progress", "message", "error",
                                                   "title", "thumbnail", "clips", "hashtags", "schedule")})
    with db() as con:
        row = con.execute("SELECT * FROM jobs WHERE id=? AND user_id=?", (jid, workspace_id)).fetchone()
    if not row:
        return jsonify({"error": "Job not found."}), 404
    return jsonify({"id": row["id"], "stage": row["status"], "status": row["status"], "progress": 100 if row["status"] == "done" else 0,
                    "message": row["title"], "title": row["title"], "thumbnail": row["thumbnail"],
                    "clips": json.loads(row["clips_json"]), "hashtags": json.loads(row["hashtags_json"]),
                    "schedule": json.loads(row["schedule_json"])})


@app.get("/api/history")
def history():
    workspace_id = current_workspace_id()
    with db() as con:
        rows = con.execute("SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 100",
                           (workspace_id,)).fetchall()
    items = []
    for row in rows:
        items.append({"id": row["id"], "title": row["title"], "thumbnail": row["thumbnail"],
                      "platform": row["platform"], "status": row["status"],
                      "created_at": row["created_at"], "clips": json.loads(row["clips_json"]),
                      "hashtags": json.loads(row["hashtags_json"]), "schedule": json.loads(row["schedule_json"])})
    return jsonify({"items": items})


@app.get("/api/media/<jid>/<path:name>")
def media(jid, name):
    workspace_id = current_workspace_id()
    with db() as con:
        row = con.execute("SELECT id FROM jobs WHERE id=? AND user_id=?", (jid, workspace_id)).fetchone()
    folder = DOWNLOADS / jid
    target = (folder / name).resolve()
    if not row or folder.resolve() not in target.parents or not target.is_file():
        return jsonify({"error": "File not found."}), 404
    return send_file(target, as_attachment=request.args.get("download") == "1")


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": "This upload is too large. Choose a video under 600 MB."}), 413


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "ffmpeg": bool(FFMPEG), "storage": str(DATA)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), threaded=True)

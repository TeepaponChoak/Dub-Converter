"""Run:  pip install -r requirements.txt   (ffmpeg must be on PATH)
        python app.py   ->  open http://127.0.0.1:5000
Only convert content you own or have permission to download."""
import re, threading, time, uuid
from pathlib import Path
from urllib.parse import urlparse

import shutil
import yt_dlp
import hmac
from flask import Flask, Response, jsonify, request, send_file, send_from_directory

app = Flask(__name__, static_folder=None)
BASE = Path(__file__).parent
OUT = BASE / "downloads"
OUT.mkdir(exist_ok=True)
JOBS = {}

# ffmpeg lookup: 1) FFMPEG_LOCATION env var  2) ./ffmpeg/bin or ./ffmpeg next to app.py  3) PATH
import os, shutil as _sh
FFMPEG_DIR = None
for _c in (os.environ.get("FFMPEG_LOCATION"), BASE / "ffmpeg" / "bin", BASE / "ffmpeg"):
    if _c and (Path(_c) / "ffmpeg.exe").exists() or _c and (Path(_c) / "ffmpeg").exists():
        FFMPEG_DIR = str(_c)
        break
HAS_FFMPEG = bool(FFMPEG_DIR or _sh.which("ffmpeg"))

# Optional: route YouTube traffic through a proxy and/or use cookies (for cloud hosts like Render).
#   YTDLP_PROXY   e.g. http://user:pass@host:port   (a residential proxy works best)
#   cookies.txt   as a Render Secret File (/etc/secrets/cookies.txt) or path in COOKIES_FILE
COOKIES_PATH = None
_src = Path(os.environ.get("COOKIES_FILE", "/etc/secrets/cookies.txt"))
if _src.exists():
    COOKIES_PATH = "/tmp/cookies.txt"  # yt-dlp needs a writable copy
    _sh.copyfile(_src, COOKIES_PATH)


def common_opts():
    o = {}
    if os.environ.get("YTDLP_PROXY"):
        o["proxy"] = os.environ["YTDLP_PROXY"]
    if COOKIES_PATH:
        o["cookiefile"] = COOKIES_PATH
    return o


MAX_JOBS = int(os.environ.get("MAX_JOBS", "3"))  # concurrent conversions
KEEP_SECONDS = 1800                               # delete files/jobs after 30 min


def cleanup():
    while True:
        time.sleep(300)
        now = time.time()
        for f in OUT.iterdir():
            try:
                if now - f.stat().st_mtime > KEEP_SECONDS:
                    f.unlink()
            except OSError:
                pass
        for jid, j in list(JOBS.items()):
            if now - j["t"] > KEEP_SECONDS:
                JOBS.pop(jid, None)


threading.Thread(target=cleanup, daemon=True).start()
HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}


def friendly(e):
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).split("\n")[0]
    low = msg.lower()
    if "sign in" in low or "not a bot" in low or "cookies" in low:
        return ("YouTube is blocking this server (it asks to confirm you’re not a bot). "
                "Set a YTDLP_PROXY (residential proxy) or cookies on the server, or run the app on your own computer.")
    if "private" in low or "unavailable" in low or "age" in low:
        return "That video is private, unavailable or age-restricted."
    if "ffmpeg" in low:
        return "ffmpeg isn’t installed or isn’t on your PATH. Install it, then restart this app."
    return msg[:200] or "Couldn’t read that video."


def valid(url):
    try:
        return urlparse(url).hostname in HOSTS
    except Exception:
        return False


def run_job(jid, url, mode, quality):
    job = JOBS[jid]
    streams = 2 if mode == "mp4" else 1
    state = {"done": 0}

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            pct = d["downloaded_bytes"] / total if total else 0
            job["progress"] = round((state["done"] + pct) / streams * 95, 1)
        elif d["status"] == "finished":
            state["done"] += 1
            if state["done"] >= streams:
                job.update(progress=96, stage="Converting")

    opts = {"outtmpl": str(OUT / f"{jid}.%(ext)s"), "noplaylist": True,
            "quiet": True, "progress_hooks": [hook], **common_opts()}
    if mode == "mp3":
        opts.update(format="bestaudio/best", postprocessors=[
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": quality}])
    else:
        opts.update(merge_output_format="mp4", format=(
            f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/"
            f"best[height<={quality}][ext=mp4]/best[height<={quality}]"))
    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        name = re.sub(r'[\\/:*?"<>|]', "", info.get("title", "download"))[:120]
        job.update(status="done", progress=100, stage="Ready",
                   file=str(OUT / f"{jid}.{mode}"), name=f"{name}.{mode}")
    except Exception as e:
        print("JOB ERROR:", e, flush=True)
        job.update(status="error", error=friendly(e))


@app.before_request
def password_guard():
    """If APP_PASSWORD is set, ask for it (any username). Protects your tunnel link."""
    pw = os.environ.get("APP_PASSWORD")
    if not pw:
        return None
    auth = request.authorization
    if not auth or not hmac.compare_digest((auth.password or "").encode(), pw.encode()):
        return Response("Password required", 401, {"WWW-Authenticate": 'Basic realm="Dub"'})
    return None


@app.errorhandler(Exception)
def on_error(e):
    code = getattr(e, "code", 500)
    return jsonify(error=f"Server error: {getattr(e, 'description', str(e))}"), code


@app.get("/static/<path:name>")
def static_files(name):
    return send_from_directory(BASE / "static", name)


@app.get("/")
def index():
    return send_from_directory(BASE, "index.html")


@app.post("/api/info")
def info():
    url = (request.json or {}).get("url", "")
    if not valid(url):
        return jsonify(error="That doesn’t look like a YouTube link."), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True, **common_opts()}) as ydl:
            i = ydl.extract_info(url, download=False)
        return jsonify(title=i.get("title"), channel=i.get("uploader"),
                       thumbnail=i.get("thumbnail"), duration=i.get("duration"))
    except Exception as e:
        print("INFO ERROR:", e, flush=True)
        return jsonify(error=friendly(e)), 400


@app.post("/api/convert")
def convert():
    d = request.json or {}
    url, mode, q = d.get("url", ""), d.get("mode"), str(d.get("quality", ""))
    if not valid(url) or mode not in ("mp3", "mp4") or not q.isdigit():
        return jsonify(error="Invalid request."), 400
    if sum(j["status"] == "running" for j in JOBS.values()) >= MAX_JOBS:
        return jsonify(error="Server is busy. Try again in a minute."), 429
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {"status": "running", "progress": 0, "stage": "Downloading", "t": time.time()}
    threading.Thread(target=run_job, args=(jid, url, mode, q), daemon=True).start()
    return jsonify(id=jid)


@app.get("/api/job/<jid>")
def job(jid):
    j = JOBS.get(jid)
    if not j:
        return jsonify(error="Unknown job."), 404
    return jsonify({k: v for k, v in j.items() if k not in ("file", "t")})


@app.get("/download/<jid>")
def download(jid):
    j = JOBS.get(jid)
    if not j or j.get("status") != "done":
        return "Not ready", 404
    return send_file(j["file"], as_attachment=True, download_name=j["name"])


if __name__ == "__main__":
    if not HAS_FFMPEG:
        print("WARNING: ffmpeg not found on PATH. Conversions will fail until it is installed.")
    # On Render (PORT is set) listen on all interfaces; locally stay on 127.0.0.1.
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    app.run(debug=False, host=host, port=int(os.environ.get("PORT", 5000)))

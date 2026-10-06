"""
BiliGrab - ONE FILE: website (HTML + CSS + JS) + video downloader server.

Chalane ka tareeqa:
    python app.py
Phir browser mein kholein:  http://localhost:8000

Zaroori packages (flask, yt-dlp, imageio-ffmpeg, gunicorn) pehli dafa khud install ho jate hain.

Endpoints
  GET /                               -> website
  GET /api/health                     -> {"ok": true}
  GET /api/info?url=...               -> title, thumbnail, duration
  GET /api/download?url=...&q=1080    -> the file (MP4 / MP3)   q = 2160, 1080, 720, 480, mp3
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlparse, quote

# ---- Auto-install missing packages ----
def _need(mod, pkg):
    try:
        __import__(mod)
    except ImportError:
        print("Installing", pkg, "...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", pkg])

for _m, _p in (("flask", "flask"), ("yt_dlp", "yt-dlp"), ("imageio_ffmpeg", "imageio-ffmpeg")):
    _need(_m, _p)

from flask import Flask, Response, jsonify, request
import yt_dlp

# ffmpeg is needed to join video + audio. Use the system one, or the bundled one.
FFMPEG = shutil.which("ffmpeg")
if not FFMPEG:
    try:
        import imageio_ffmpeg
        FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        FFMPEG = None

app = Flask(__name__)

# ---- Settings (change with environment variables) ----
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")          # e.g. https://yoursite.com
COOKIES_FILE = os.environ.get("COOKIES_FILE")                   # optional: your own cookies.txt for 1080p+
MAX_DURATION = int(os.environ.get("MAX_DURATION", 60 * 60))     # seconds (default 1 hour)
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", 2))           # downloads at the same time

ALLOWED_HOSTS = ("bilibili.com", "b23.tv", "bilibili.tv")
QUALITY = {
    "2160": "bv*[height<=2160]+ba/b[height<=2160]/b",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]/b",
    "720":  "bv*[height<=720]+ba/b[height<=720]/b",
    "480":  "bv*[height<=480]+ba/b[height<=480]/b",
}
slots = threading.BoundedSemaphore(MAX_PARALLEL)


@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
    resp.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
    return resp


def check_url(url):
    """Only accept real Bilibili links (stops people using your server for other sites)."""
    if not url:
        return None
    if not re.match(r"^https?://", url):
        url = "https://" + url
    host = (urlparse(url).hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        return url
    return None


def base_opts():
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "socket_timeout": 30, "retries": 3}
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    if FFMPEG:
        opts["ffmpeg_location"] = FFMPEG
    return opts


def err(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/info")
def info():
    url = check_url(request.args.get("url", "").strip())
    if not url:
        return err("Please paste a valid Bilibili video link.")
    try:
        with yt_dlp.YoutubeDL(base_opts()) as ydl:
            data = ydl.extract_info(url, download=False)
    except Exception as e:
        return err("Could not read this video: " + str(e)[:200], 422)
    return jsonify({
        "ok": True,
        "title": data.get("title"),
        "thumbnail": data.get("thumbnail"),
        "duration": data.get("duration"),
        "uploader": data.get("uploader"),
    })


@app.get("/api/download")
def download():
    url = check_url(request.args.get("url", "").strip())
    q = request.args.get("q", "1080").lower()
    if not url:
        return err("Please paste a valid Bilibili video link.")
    if q != "mp3" and q not in QUALITY:
        return err("Unknown quality.")
    if not slots.acquire(blocking=False):
        return err("Server is busy, please try again in a minute.", 429)

    tmp = tempfile.mkdtemp(prefix="biligrab_")
    try:
        opts = base_opts()
        opts["outtmpl"] = os.path.join(tmp, "%(title).80s [%(id)s].%(ext)s")
        opts["match_filter"] = yt_dlp.utils.match_filter_func(f"duration <= {MAX_DURATION}")
        if q == "mp3":
            opts["format"] = "ba/b"
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                       "preferredcodec": "mp3", "preferredquality": "320"}]
        else:
            opts["format"] = QUALITY[q]
            opts["merge_output_format"] = "mp4"

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        files = [f for f in os.listdir(tmp) if not f.endswith((".part", ".ytdl"))]
        if not files:
            raise RuntimeError("Video is too long or could not be downloaded.")
        path = os.path.join(tmp, max(files, key=lambda f: os.path.getsize(os.path.join(tmp, f))))
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        slots.release()
        return err("Download failed: " + str(e)[:200], 422)

    name = os.path.basename(path)
    size = os.path.getsize(path)

    def stream():
        # send the file in pieces, then delete it and free the slot
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 512)
                    if not chunk:
                        break
                    yield chunk
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            slots.release()

    mime = "audio/mpeg" if name.endswith(".mp3") else "video/mp4"
    safe = re.sub(r'[^A-Za-z0-9 ._\-\[\]()]', "_", name)
    headers = {
        "Content-Length": str(size),
        "Content-Disposition": f"attachment; filename=\"{safe}\"; filename*=UTF-8''{quote(name)}",
    }
    return Response(stream(), mimetype=mime, headers=headers)


@app.get("/")
def home():
    return Response(PAGE, mimetype="text/html")


# =====================  WEBSITE (HTML + CSS + JS)  =====================
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>BiliGrab – Video Downloader</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700;800&display=swap" rel="stylesheet" />
  <style>
/* ===========================
   BiliGrab – Video Downloader
   Palette: pink #FB7299 · blue #00AEEC
   =========================== */

:root {
  --pink: #fb7299;
  --pink-dark: #e85a85;
  --blue: #00aeec;
  --blue-dark: #0094c9;
  --ink: #18191c;
  --muted: #61666d;
  --bg: #f6f7fb;
  --card: #ffffff;
  --line: #e3e5e7;
  --grad: linear-gradient(135deg, var(--pink), var(--blue));
  --shadow: 0 10px 30px rgba(0, 174, 236, .12), 0 4px 12px rgba(251, 114, 153, .10);
  --radius: 18px;
}

@media (prefers-color-scheme: dark) {
  :root {
    --ink: #f1f2f3;
    --muted: #a2a7ae;
    --bg: #101114;
    --card: #1a1c21;
    --line: #2b2e35;
    --shadow: 0 10px 30px rgba(0, 0, 0, .45);
  }
}

* { margin: 0; padding: 0; box-sizing: border-box; }

/* Smooth scrolling */
html { scroll-behavior: smooth; scroll-padding-top: 80px; }

body {
  font-family: "Poppins", system-ui, sans-serif;
  color: var(--ink);
  background: var(--bg);
  line-height: 1.6;
  overflow-x: hidden;
}

a { color: inherit; text-decoration: none; }

.grad {
  background: var(--grad);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}

/* ---------- Background blobs ---------- */
.bg { position: fixed; inset: 0; z-index: -1; overflow: hidden; pointer-events: none; }
.blob {
  position: absolute; border-radius: 50%;
  filter: blur(80px); opacity: .35;
  animation: drift 18s ease-in-out infinite alternate;
}
.b1 { width: 420px; height: 420px; background: var(--pink); top: -120px; left: -100px; }
.b2 { width: 480px; height: 480px; background: var(--blue); top: 30%; right: -160px; animation-delay: -6s; }
.b3 { width: 360px; height: 360px; background: var(--pink); bottom: -140px; left: 30%; animation-delay: -12s; }
@keyframes drift {
  to { transform: translate(60px, 40px) scale(1.15); }
}

/* ---------- Navbar ---------- */
.nav {
  position: sticky; top: 0; z-index: 50;
  display: flex; align-items: center; justify-content: space-between;
  gap: 16px; padding: 14px 6vw;
  background: color-mix(in srgb, var(--bg) 70%, transparent);
  backdrop-filter: blur(14px);
  transition: box-shadow .3s;
}
.nav.scrolled { box-shadow: 0 4px 20px rgba(0, 0, 0, .08); }

.brand { display: flex; align-items: center; gap: 10px; font-weight: 700; font-size: 1.35rem; }
.brand b { color: var(--pink); }

.links { display: flex; gap: 28px; }
.links a { font-weight: 500; color: var(--muted); position: relative; transition: color .2s; }
.links a::after {
  content: ""; position: absolute; left: 0; bottom: -4px;
  width: 0; height: 2px; background: var(--grad); transition: width .3s;
}
.links a:hover { color: var(--ink); }
.links a:hover::after { width: 100%; }

/* mini spinning 3D cube (logo in nav/footer) */
.mini-cube {
  width: 26px; height: 26px; position: relative;
  transform-style: preserve-3d;
  animation: spin 6s linear infinite;
}
.mini-cube i {
  position: absolute; inset: 0; border-radius: 6px;
  background: var(--grad); opacity: .9;
}
.mini-cube i:nth-child(1) { transform: translateZ(13px); }
.mini-cube i:nth-child(2) { transform: rotateY(90deg) translateZ(13px); background: var(--blue); }
.mini-cube i:nth-child(3) { transform: rotateX(90deg) translateZ(13px); background: var(--pink); }

/* ---------- Buttons ---------- */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  padding: 14px 26px; border: 0; border-radius: 14px; cursor: pointer;
  font: 600 1rem "Poppins", sans-serif; color: #fff;
  background: var(--grad); background-size: 200% 200%;
  box-shadow: 0 8px 22px rgba(251, 114, 153, .35);
  transition: transform .2s, box-shadow .2s, background-position .5s;
}
.btn:hover { transform: translateY(-2px); background-position: 100% 0; box-shadow: 0 12px 28px rgba(0, 174, 236, .35); }
.btn:active { transform: translateY(0); }
.btn-small { padding: 10px 18px; font-size: .9rem; }

.ic { width: 20px; height: 20px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; flex-shrink: 0; }

/* ---------- Hero ---------- */
.hero {
  min-height: calc(100vh - 70px);
  display: grid; grid-template-columns: 1.1fr .9fr; align-items: center;
  gap: 40px; padding: 40px 6vw 80px; position: relative;
}
.pill {
  display: inline-block; padding: 6px 14px; border-radius: 99px;
  background: color-mix(in srgb, var(--pink) 15%, transparent);
  color: var(--pink-dark); font-weight: 600; font-size: .85rem; margin-bottom: 18px;
}
.hero h1 { font-size: clamp(2.2rem, 5vw, 3.8rem); line-height: 1.12; font-weight: 800; margin-bottom: 18px; }
.hero p { color: var(--muted); font-size: 1.1rem; max-width: 560px; margin-bottom: 30px; }

/* Download box */
.dl-box {
  display: flex; gap: 10px; flex-wrap: nowrap;
  background: var(--card); padding: 10px; border-radius: 20px;
  box-shadow: var(--shadow); border: 1px solid var(--line);
}
.input-wrap {
  flex: 1 1 240px; display: flex; align-items: center; gap: 10px;
  padding: 0 8px 0 14px; border-radius: 14px; background: var(--bg);
  border: 2px solid transparent; transition: border-color .2s;
}
.input-wrap:focus-within { border-color: var(--blue); }
.input-wrap .ic { color: var(--muted); }
.input-wrap input {
  flex: 1; min-width: 0; border: 0; outline: 0; background: transparent;
  padding: 14px 0; font: 400 .95rem "Poppins", sans-serif; color: var(--ink);
}
.paste {
  border: 0; cursor: pointer; padding: 8px 12px; border-radius: 10px;
  background: color-mix(in srgb, var(--blue) 15%, transparent); color: var(--blue-dark);
  font: 600 .85rem "Poppins", sans-serif;
}
.paste:hover { background: color-mix(in srgb, var(--blue) 25%, transparent); }
select {
  padding: 0 14px; border-radius: 14px; border: 1px solid var(--line);
  background: var(--bg); color: var(--ink); font: 500 .95rem "Poppins", sans-serif; cursor: pointer;
  min-height: 50px;
}

/* Progress */
.progress { max-height: 0; overflow: hidden; opacity: 0; transition: all .4s ease; }
.progress.show { max-height: 80px; opacity: 1; margin-top: 16px; }
.bar { height: 10px; border-radius: 99px; background: var(--line); overflow: hidden; }
.bar span { display: block; height: 100%; width: 0; background: var(--grad); border-radius: 99px; transition: width .2s; }
.progress small { display: block; margin-top: 6px; color: var(--muted); }

.stats { display: flex; gap: 36px; margin-top: 34px; }
.stats strong { display: block; font-size: 1.6rem; font-weight: 800; color: var(--pink); }
.stats div:nth-child(2) strong { color: var(--blue); }
.stats span { color: var(--muted); font-size: .9rem; }

/* ---------- 3D animated logo ---------- */
.scene {
  --size: 180px;
  position: relative; height: 460px;
  display: grid; place-items: center;
  perspective: 1000px;
}
.float { animation: float 4s ease-in-out infinite; transform-style: preserve-3d; }
.cube {
  width: var(--size); height: var(--size); position: relative;
  transform-style: preserve-3d;
  animation: spin 12s linear infinite;
}
.face {
  position: absolute; inset: 0; border-radius: 28px;
  display: grid; place-items: center;
  border: 2px solid rgba(255, 255, 255, .5);
  box-shadow: inset 0 0 40px rgba(255, 255, 255, .25);
  backface-visibility: visible;
}
.face svg { width: 62%; height: 62%; }
.face path { fill: #fff; }
.face path.stroke { fill: none; stroke: #fff; stroke-width: 9; stroke-linecap: round; stroke-linejoin: round; }
.face text { fill: #fff; font: 800 32px "Poppins", sans-serif; }

.front  { background: linear-gradient(135deg, var(--pink), var(--pink-dark)); transform: translateZ(calc(var(--size) / 2)); }
.back   { background: linear-gradient(135deg, var(--blue), var(--blue-dark)); transform: rotateY(180deg) translateZ(calc(var(--size) / 2)); }
.right  { background: linear-gradient(135deg, var(--blue), var(--pink)); transform: rotateY(90deg) translateZ(calc(var(--size) / 2)); }
.left   { background: linear-gradient(135deg, var(--pink), var(--blue)); transform: rotateY(-90deg) translateZ(calc(var(--size) / 2)); }
.top    { background: linear-gradient(135deg, var(--pink-dark), var(--blue)); transform: rotateX(90deg) translateZ(calc(var(--size) / 2)); }
.bottom { background: linear-gradient(135deg, var(--blue-dark), var(--pink)); transform: rotateX(-90deg) translateZ(calc(var(--size) / 2)); }

@keyframes spin {
  0%   { transform: rotateX(-20deg) rotateY(0deg); }
  50%  { transform: rotateX(20deg)  rotateY(180deg); }
  100% { transform: rotateX(-20deg) rotateY(360deg); }
}
@keyframes float {
  0%, 100% { transform: translateY(-14px); }
  50%      { transform: translateY(14px); }
}

.shadow {
  position: absolute; bottom: 50px; width: 160px; height: 24px; border-radius: 50%;
  background: radial-gradient(rgba(0, 0, 0, .25), transparent 70%);
  animation: shadow 4s ease-in-out infinite;
}
@keyframes shadow { 50% { transform: scale(.75); opacity: .5; } }

/* orbit rings */
.orbit {
  position: absolute; width: 340px; height: 340px; border-radius: 50%;
  border: 2px dashed color-mix(in srgb, var(--blue) 50%, transparent);
  transform-style: preserve-3d;
}
.orbit span {
  position: absolute; top: -8px; left: calc(50% - 8px);
  width: 16px; height: 16px; border-radius: 50%; background: var(--pink);
  box-shadow: 0 0 18px var(--pink);
}
.o1 { animation: orbit1 8s linear infinite; }
.o2 { width: 400px; height: 400px; border-color: color-mix(in srgb, var(--pink) 45%, transparent); animation: orbit2 11s linear infinite; }
.o2 span { background: var(--blue); box-shadow: 0 0 18px var(--blue); }
.o3 { width: 280px; height: 280px; animation: orbit3 14s linear infinite reverse; }
@keyframes orbit1 { from { transform: rotateX(70deg) rotateZ(0); }   to { transform: rotateX(70deg) rotateZ(360deg); } }
@keyframes orbit2 { from { transform: rotateY(65deg) rotateZ(0); }   to { transform: rotateY(65deg) rotateZ(360deg); } }
@keyframes orbit3 { from { transform: rotateX(-50deg) rotateY(30deg) rotateZ(0); } to { transform: rotateX(-50deg) rotateY(30deg) rotateZ(360deg); } }

/* floating chips */
.chip {
  position: absolute; padding: 8px 16px; border-radius: 12px;
  background: var(--card); box-shadow: var(--shadow); font-weight: 700;
  animation: bob 5s ease-in-out infinite;
}
.c1 { top: 60px; left: 6%; color: var(--pink); }
.c2 { top: 120px; right: 4%; color: var(--blue); animation-delay: -1.5s; }
.c3 { bottom: 80px; left: 14%; color: var(--blue); animation-delay: -3s; }
@keyframes bob { 50% { transform: translateY(-16px) rotate(-4deg); } }

/* scroll hint */
.scroll-hint {
  position: absolute; bottom: 20px; left: 50%; transform: translateX(-50%);
  width: 28px; height: 46px; border: 2px solid var(--muted); border-radius: 20px;
}
.scroll-hint span {
  position: absolute; top: 8px; left: 50%; width: 4px; height: 10px; margin-left: -2px;
  border-radius: 4px; background: var(--pink); animation: wheel 1.6s ease-in-out infinite;
}
@keyframes wheel { to { transform: translateY(16px); opacity: 0; } }

/* ---------- Sections ---------- */
.section { padding: 100px 6vw; text-align: center; }
.section.alt { background: color-mix(in srgb, var(--card) 60%, transparent); }
.section h2 { font-size: clamp(1.8rem, 4vw, 2.6rem); font-weight: 800; }
.sub { color: var(--muted); margin: 8px 0 50px; }

.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 24px; max-width: 1100px; margin: 0 auto; }
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: var(--radius);
  padding: 30px 26px; text-align: left; box-shadow: var(--shadow);
  transition: transform .35s, box-shadow .35s;
}
.card:hover { transform: translateY(-8px) rotateX(4deg); box-shadow: 0 20px 40px rgba(251, 114, 153, .2); }
.card h3 { margin: 16px 0 6px; }
.card p { color: var(--muted); font-size: .95rem; }
.icon { width: 54px; height: 54px; border-radius: 14px; display: grid; place-items: center; font-size: 1.5rem; }
.icon.pink { background: color-mix(in srgb, var(--pink) 18%, transparent); }
.icon.blue { background: color-mix(in srgb, var(--blue) 18%, transparent); }

/* steps */
.steps { display: flex; align-items: flex-start; justify-content: center; gap: 10px; max-width: 1000px; margin: 0 auto; }
.step { flex: 1; padding: 10px 16px; }
.num {
  width: 64px; height: 64px; margin: 0 auto 16px; border-radius: 50%;
  display: grid; place-items: center; font-size: 1.5rem; font-weight: 800; color: #fff;
  background: var(--grad); box-shadow: 0 10px 25px rgba(0, 174, 236, .3);
}
.step p { color: var(--muted); font-size: .95rem; }
.line { flex: 0 0 60px; height: 3px; margin-top: 32px; border-radius: 3px; background: var(--grad); opacity: .5; }

/* formats */
.formats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 18px; max-width: 1000px; margin: 0 auto; }
.fmt {
  background: var(--card); border: 2px solid var(--line); border-radius: var(--radius);
  padding: 26px 10px; transition: border-color .3s, transform .3s;
}
.fmt:hover { border-color: var(--pink); transform: scale(1.05); }
.fmt span { display: block; font-size: 1.6rem; font-weight: 800; background: var(--grad); -webkit-background-clip: text; background-clip: text; color: transparent; }
.fmt small { color: var(--muted); }

/* faq */
.faq { max-width: 760px; margin: 40px auto 0; text-align: left; display: grid; gap: 14px; }
details {
  background: var(--card); border: 1px solid var(--line); border-radius: 14px;
  padding: 18px 22px; box-shadow: var(--shadow);
}
summary { cursor: pointer; font-weight: 600; list-style: none; display: flex; justify-content: space-between; }
summary::-webkit-details-marker { display: none; }
summary::after { content: "+"; color: var(--pink); font-size: 1.4rem; line-height: 1; transition: transform .3s; }
details[open] summary::after { transform: rotate(45deg); }
details p { color: var(--muted); margin-top: 10px; }

/* footer */
.footer { padding: 50px 6vw; text-align: center; border-top: 1px solid var(--line); }
.footer .brand { justify-content: center; margin-bottom: 12px; }
.footer p { color: var(--muted); font-size: .9rem; }
.footer small { color: var(--muted); }

/* ---------- Scroll reveal ---------- */
.reveal { opacity: 0; transform: translateY(40px); transition: opacity .8s ease, transform .8s cubic-bezier(.2, .8, .2, 1); }
.reveal.in { opacity: 1; transform: none; }
.grid .reveal:nth-child(2), .formats .reveal:nth-child(2) { transition-delay: .1s; }
.grid .reveal:nth-child(3), .formats .reveal:nth-child(3) { transition-delay: .2s; }
.grid .reveal:nth-child(4), .formats .reveal:nth-child(4) { transition-delay: .3s; }
.grid .reveal:nth-child(5), .formats .reveal:nth-child(5) { transition-delay: .4s; }
.grid .reveal:nth-child(6), .formats .reveal:nth-child(6) { transition-delay: .5s; }

/* ---------- Responsive ---------- */
@media (max-width: 900px) {
  .hero { grid-template-columns: 1fr; text-align: center; padding-top: 20px; }
  .hero p { margin-inline: auto; }
  .stats { justify-content: center; }
  .scene { height: 380px; --size: 140px; }
  .orbit { width: 260px; height: 260px; }
  .o2 { width: 310px; height: 310px; }
  .o3 { width: 220px; height: 220px; }
  .links { display: none; }
  .dl-box { flex-wrap: wrap; }
  .steps { flex-direction: column; align-items: center; }
  .line { width: 3px; height: 40px; flex-basis: 40px; margin: 0; }
}
@media (max-width: 520px) {
  .nav { padding: 12px 16px; }
  .hero, .section { padding-left: 16px; padding-right: 16px; }
  .dl-box { flex-wrap: wrap; }
  .dl-box select, .dl-box .btn { flex: 1 1 100%; }
  .stats { gap: 20px; }
}

@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  *, *::before, *::after { animation: none !important; transition: none !important; }
  .reveal { opacity: 1; transform: none; }
}

</style>
</head>
<body>

  <!-- Animated background blobs -->
  <div class="bg">
    <span class="blob b1"></span>
    <span class="blob b2"></span>
    <span class="blob b3"></span>
  </div>

  <!-- NAV -->
  <header class="nav">
    <a href="#home" class="brand">
      <span class="mini-cube"><i></i><i></i><i></i></span>
      <span>Bili<b>Grab</b></span>
    </a>
    <nav class="links">
      <a href="#home">Home</a>
      <a href="#features">Features</a>
      <a href="#how">How it works</a>
      <a href="#formats">Formats</a>
      <a href="#faq">FAQ</a>
    </nav>
    <a href="#home" class="btn btn-small">Download</a>
  </header>

  <main>
    <!-- HERO -->
    <section id="home" class="hero">
      <div class="hero-text reveal">
        <span class="pill">⚡ Fast · Free · HD</span>
        <h1>Download <span class="grad">Bilibili</span> videos in one click</h1>
        <p>Paste any video link, choose your quality, and save it as MP4, MP3 or with subtitles. Up to 4K, no sign-up needed.</p>

        <form class="dl-box" id="dlForm" onsubmit="return false;">
          <div class="input-wrap">
            <svg viewBox="0 0 24 24" class="ic"><path d="M10 14a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1M14 10a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/></svg>
            <input id="url" type="url" placeholder="Paste video link here… e.g. https://www.bilibili.com/video/BV…" required />
            <button type="button" class="paste" id="pasteBtn">Paste</button>
          </div>
          <select id="quality" aria-label="Quality">
            <option value="1080">1080p MP4</option>
            <option value="2160">4K MP4</option>
            <option value="720">720p MP4</option>
            <option value="480">480p MP4</option>
            <option value="mp3">MP3 Audio</option>
          </select>
          <button type="submit" class="btn" id="goBtn">
            <svg viewBox="0 0 24 24" class="ic"><path d="M12 3v12m0 0-5-5m5 5 5-5M4 19h16"/></svg>
            Download
          </button>
        </form>

        <div class="progress" id="progress">
          <div class="bar"><span id="barFill"></span></div>
          <small id="status">Fetching video…</small>
        </div>

        <div class="stats">
          <div><strong>4K</strong><span>Max quality</span></div>
          <div><strong>0s</strong><span>Sign-up time</span></div>
          <div><strong>100%</strong><span>Free</span></div>
        </div>
      </div>

      <!-- 3D ANIMATED LOGO -->
      <div class="scene reveal">
        <div class="orbit o1"><span></span></div>
        <div class="orbit o2"><span></span></div>
        <div class="orbit o3"><span></span></div>

        <div class="float">
          <div class="cube">
            <div class="face front">
              <svg viewBox="0 0 100 100"><path d="M38 28 L74 50 L38 72 Z" /></svg>
            </div>
            <div class="face back">
              <svg viewBox="0 0 100 100"><path d="M50 22v40m0 0-16-16m16 16 16-16M26 78h48" class="stroke"/></svg>
            </div>
            <div class="face right">
              <svg viewBox="0 0 100 100"><text x="50" y="62" text-anchor="middle">HD</text></svg>
            </div>
            <div class="face left">
              <svg viewBox="0 0 100 100"><text x="50" y="62" text-anchor="middle">4K</text></svg>
            </div>
            <div class="face top">
              <svg viewBox="0 0 100 100"><path d="M38 28 L74 50 L38 72 Z" /></svg>
            </div>
            <div class="face bottom">
              <svg viewBox="0 0 100 100"><path d="M50 22v40m0 0-16-16m16 16 16-16M26 78h48" class="stroke"/></svg>
            </div>
          </div>
        </div>
        <div class="shadow"></div>

        <div class="chip c1">MP4</div>
        <div class="chip c2">MP3</div>
        <div class="chip c3">SRT</div>
      </div>

      <a href="#features" class="scroll-hint" aria-label="Scroll down"><span></span></a>
    </section>

    <!-- FEATURES -->
    <section id="features" class="section">
      <h2 class="reveal">Why <span class="grad">BiliGrab</span>?</h2>
      <p class="sub reveal">Everything you need to save your favourite videos.</p>
      <div class="grid">
        <article class="card reveal">
          <div class="icon pink">⚡</div>
          <h3>Lightning fast</h3>
          <p>Multi-threaded downloads that use your full bandwidth.</p>
        </article>
        <article class="card reveal">
          <div class="icon blue">🎬</div>
          <h3>Up to 4K</h3>
          <p>Choose from 360p all the way to crisp 4K resolution.</p>
        </article>
        <article class="card reveal">
          <div class="icon pink">🎵</div>
          <h3>Audio only</h3>
          <p>Extract the soundtrack as a high-quality MP3 file.</p>
        </article>
        <article class="card reveal">
          <div class="icon blue">💬</div>
          <h3>Subtitles</h3>
          <p>Grab CC subtitles as SRT files in any available language.</p>
        </article>
        <article class="card reveal">
          <div class="icon pink">📱</div>
          <h3>Any device</h3>
          <p>Works in every browser on phone, tablet and desktop.</p>
        </article>
        <article class="card reveal">
          <div class="icon blue">🔒</div>
          <h3>Private</h3>
          <p>No account, no tracking, no stored history.</p>
        </article>
      </div>
    </section>

    <!-- HOW IT WORKS -->
    <section id="how" class="section alt">
      <h2 class="reveal">How it <span class="grad">works</span></h2>
      <p class="sub reveal">Three simple steps.</p>
      <div class="steps">
        <div class="step reveal">
          <span class="num">1</span>
          <h3>Copy the link</h3>
          <p>Open the video and copy its URL from the address bar or share menu.</p>
        </div>
        <div class="line"></div>
        <div class="step reveal">
          <span class="num">2</span>
          <h3>Paste & choose</h3>
          <p>Paste the link above and pick the format and quality you want.</p>
        </div>
        <div class="line"></div>
        <div class="step reveal">
          <span class="num">3</span>
          <h3>Download</h3>
          <p>Hit download and the file is saved straight to your device.</p>
        </div>
      </div>
    </section>

    <!-- FORMATS -->
    <section id="formats" class="section">
      <h2 class="reveal">Supported <span class="grad">formats</span></h2>
      <p class="sub reveal">Pick what fits you best.</p>
      <div class="formats">
        <div class="fmt reveal"><span>4K</span><small>2160p · MP4</small></div>
        <div class="fmt reveal"><span>1080p</span><small>Full HD · MP4</small></div>
        <div class="fmt reveal"><span>720p</span><small>HD · MP4</small></div>
        <div class="fmt reveal"><span>480p</span><small>SD · MP4</small></div>
        <div class="fmt reveal"><span>MP3</span><small>320 kbps</small></div>
        <div class="fmt reveal"><span>SRT</span><small>Subtitles</small></div>
      </div>
    </section>

    <!-- FAQ -->
    <section id="faq" class="section alt">
      <h2 class="reveal">Frequently asked <span class="grad">questions</span></h2>
      <div class="faq">
        <details class="reveal" open>
          <summary>Is BiliGrab free?</summary>
          <p>Yes, completely free with no limits on the number of downloads.</p>
        </details>
        <details class="reveal">
          <summary>Do I need to install anything?</summary>
          <p>No. Everything runs in your browser — just paste and download.</p>
        </details>
        <details class="reveal">
          <summary>Which links are supported?</summary>
          <p>Standard video links (BV / av IDs), short b23.tv links and multi-part videos.</p>
        </details>
        <details class="reveal">
          <summary>Is it legal?</summary>
          <p>Only download videos you own or have permission to save, and respect creators' copyrights.</p>
        </details>
      </div>
    </section>
  </main>

  <footer class="footer">
    <a href="#home" class="brand">
      <span class="mini-cube"><i></i><i></i><i></i></span>
      <span>Bili<b>Grab</b></span>
    </a>
    <p>BiliGrab is an independent tool and is not affiliated with or endorsed by Bilibili.</p>
    <small>© 2026 BiliGrab. All rights reserved.</small>
  </footer>

  <script>
    /* ====== APNE SERVER KA LINK YAHAN LIKHEIN (end mein / na lagayein) ====== */
    const API_URL = '';   // khali = isi server se chalega
    /* ======================================================================== */

    const form = document.getElementById('dlForm');
    const url = document.getElementById('url');
    const quality = document.getElementById('quality');
    const goBtn = document.getElementById('goBtn');
    const prog = document.getElementById('progress');
    const fill = document.getElementById('barFill');
    const status = document.getElementById('status');

    document.getElementById('pasteBtn').addEventListener('click', async () => {
      try { url.value = await navigator.clipboard.readText(); } catch (e) { url.focus(); }
    });

    function setStatus(text, pct) {
      prog.classList.add('show');
      status.textContent = text;
      if (pct !== undefined) fill.style.width = pct + '%';
    }

    async function readError(res) {
      try { return (await res.json()).error || ('Error ' + res.status); }
      catch (e) { return 'Error ' + res.status; }
    }

    form.addEventListener('submit', async () => {
      if (!url.checkValidity()) { url.reportValidity(); return; }
      const link = encodeURIComponent(url.value.trim());
      const q = quality.value;
      goBtn.disabled = true;
      let fake;

      try {
        // 1) Check the link and get the title
        setStatus('Checking link…', 5);
        let res = await fetch(`${API_URL}/api/info?url=${link}`);
        if (!res.ok) throw new Error(await readError(res));
        const info = await res.json();

        // 2) Server downloads the video (can take a while)
        let p = 10;
        setStatus(`Preparing “${info.title}”… please wait`, p);
        fake = setInterval(() => { p = Math.min(p + 1, 60); fill.style.width = p + '%'; }, 1000);
        res = await fetch(`${API_URL}/api/download?url=${link}&q=${q}`);
        clearInterval(fake);
        if (!res.ok) throw new Error(await readError(res));

        // 3) Receive the file with real progress
        const total = +res.headers.get('Content-Length') || 0;
        const reader = res.body.getReader();
        const chunks = []; let got = 0;
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          chunks.push(value); got += value.length;
          const pct = total ? 60 + Math.round(got / total * 40) : 80;
          setStatus(`Downloading… ${(got / 1048576).toFixed(1)} MB`, pct);
        }

        // 4) Save to device
        const cd = res.headers.get('Content-Disposition') || '';
        const m = cd.match(/filename\*=UTF-8''([^;]+)/) || cd.match(/filename="?([^";]+)"?/);
        const name = m ? decodeURIComponent(m[1]) : (q === 'mp3' ? 'audio.mp3' : 'video.mp4');
        const blob = new Blob(chunks);
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = name;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 60000);
        setStatus('✅ Done! Saved as ' + name, 100);
      } catch (e) {
        clearInterval(fake);
        const msg = e.message === 'Failed to fetch'
          ? 'Cannot reach the server. Check your internet or that the server is running.'
          : e.message;
        setStatus('❌ ' + msg, 0);
      } finally {
        goBtn.disabled = false;
      }
    });

    // Scroll reveal
    const io = new IntersectionObserver(entries => {
      entries.forEach(e => { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } });
    }, { threshold: 0.15 });
    document.querySelectorAll('.reveal').forEach(el => io.observe(el));

    // Nav shadow on scroll
    const nav = document.querySelector('.nav');
    addEventListener('scroll', () => nav.classList.toggle('scrolled', scrollY > 20));
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

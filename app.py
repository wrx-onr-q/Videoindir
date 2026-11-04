import os, re, time, threading, subprocess
from urllib.parse import urlparse
import requests
from flask import Flask, jsonify, request, send_file, Response, stream_with_context
import yt_dlp

app = Flask(__name__, static_folder=None)
BASE = os.path.dirname(os.path.abspath(__file__))
ALLOWED = ("tiktok.com", "instagram.com")
CACHE, HITS = {}, {}
LOCK = threading.Lock()
SLOTS = threading.BoundedSemaphore(2)  # aynı anda en fazla 2 ffmpeg işi


def valid_url(url):
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return url.startswith("http") and any(host == d or host.endswith("." + d) for d in ALLOWED)


def limited():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    now = time.time()
    with LOCK:
        if len(HITS) > 5000:
            HITS.clear()
        h = [t for t in HITS.get(ip, []) if now - t < 60]
        if len(h) >= 20:
            HITS[ip] = h
            return True
        h.append(now)
        HITS[ip] = h
    return False


def summarize(d):
    fs = [f for f in d.get("formats") or [] if f.get("url") and f.get("protocol") in ("http", "https")]
    hv = lambda f: f.get("vcodec") not in (None, "none")
    ha = lambda f: f.get("acodec") not in (None, "none")
    q = lambda f: (f.get("height") or 0, f.get("tbr") or 0)
    both = sorted([f for f in fs if hv(f) and ha(f)], key=q)
    vonly = sorted([f for f in fs if hv(f) and not ha(f)], key=q)
    aonly = sorted([f for f in fs if ha(f) and not hv(f)], key=lambda f: f.get("abr") or 0)
    m = lambda f: {"url": f["url"], "h": f.get("http_headers") or d.get("http_headers") or {}}
    if both:
        video = [m(both[-1])]
    elif vonly and aonly:
        video = [m(vonly[-1]), m(aonly[-1])]
    elif d.get("url"):
        video = [{"url": d["url"], "h": d.get("http_headers") or {}}]
    else:
        video = None
    audio = m(aonly[-1]) if aonly else (video[0] if video else None)
    return {
        "title": d.get("title") or "Video",
        "author": d.get("uploader") or "",
        "thumbnail": d.get("thumbnail") or "",
        "video": video,
        "audio": audio,
    }


def get_info(url):
    now = time.time()
    with LOCK:
        c = CACHE.get(url)
        if c and now - c[0] < 600:
            return c[1]
        if len(CACHE) > 300:
            CACHE.clear()
    opts = {"quiet": True, "skip_download": True, "noplaylist": True, "socket_timeout": 15}
    with yt_dlp.YoutubeDL(opts) as ydl:
        data = summarize(ydl.extract_info(url, download=False))
    with LOCK:
        CACHE[url] = (now, data)
    return data


def hdr_arg(h):
    return "".join(f"{k}: {v}\r\n" for k, v in h.items() if k.lower() in ("user-agent", "referer", "cookie", "accept"))


def ffmpeg_stream(inputs, out_args, mime, name):
    if not SLOTS.acquire(timeout=20):
        return jsonify(error="Sunucu yoğun, birkaç saniye sonra tekrar dene."), 503
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    for i in inputs:
        cmd += ["-headers", hdr_arg(i["h"]), "-i", i["url"]]
    cmd += out_args + ["pipe:1"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def gen():
        while True:
            b = p.stdout.read(65536)
            if not b:
                break
            yield b

    resp = Response(stream_with_context(gen()), mimetype=mime,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
    resp.call_on_close(lambda: (p.kill(), p.wait(), SLOTS.release()))
    return resp


def proxy(src, name):
    r = requests.get(src["url"], headers=src["h"], stream=True, timeout=20)
    if r.status_code >= 400:
        r.close()
        return jsonify(error="Kaynak indirmeyi reddetti."), 502
    resp = Response(stream_with_context(r.iter_content(65536)), mimetype="video/mp4",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
    if r.headers.get("Content-Length") and "Content-Encoding" not in r.headers:
        resp.headers["Content-Length"] = r.headers["Content-Length"]
    resp.call_on_close(r.close)
    return resp


@app.get("/")
def home():
    return send_file(os.path.join(BASE, "index.html"))


@app.post("/api/info")
def info():
    if limited():
        return jsonify(error="Çok hızlı istek attın, bir dakika bekle."), 429
    url = (request.json or {}).get("url", "").strip()
    if not valid_url(url):
        return jsonify(error="Lütfen geçerli bir TikTok veya Instagram bağlantısı gir."), 400
    try:
        d = get_info(url)
    except Exception:
        return jsonify(error="Video bulunamadı. Hesap gizli olabilir ya da bağlantı hatalı."), 422
    return jsonify(title=d["title"], author=d["author"], thumbnail=d["thumbnail"])


@app.post("/api/download")
def download():
    if limited():
        return jsonify(error="Çok hızlı istek attın, bir dakika bekle."), 429
    body = request.json or {}
    url, kind = body.get("url", "").strip(), body.get("kind", "mp4")
    if not valid_url(url):
        return jsonify(error="Geçersiz bağlantı."), 400
    try:
        d = get_info(url)
    except Exception:
        return jsonify(error="İndirme başarısız oldu."), 422
    name = re.sub(r"[^\w\-]+", "_", d["title"])[:60] or "video"
    if kind == "mp3":
        if not d["audio"]:
            return jsonify(error="Ses bulunamadı."), 422
        return ffmpeg_stream([d["audio"]], ["-vn", "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3"],
                             "audio/mpeg", name + ".mp3")
    v = d["video"]
    if not v:
        return jsonify(error="Video adresi bulunamadı."), 422
    try:
        if len(v) == 1:
            return proxy(v[0], name + ".mp4")
        return ffmpeg_stream(v, ["-c", "copy", "-movflags", "frag_keyframe+empty_moov", "-f", "mp4"],
                             "video/mp4", name + ".mp4")
    except requests.RequestException:
        return jsonify(error="Kaynağa bağlanılamadı."), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

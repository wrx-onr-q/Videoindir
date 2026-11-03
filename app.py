import os, re, shutil, tempfile
from urllib.parse import urlparse
from flask import Flask, jsonify, request, send_file, after_this_request
import yt_dlp

app = Flask(__name__, static_folder=None)
BASE = os.path.dirname(os.path.abspath(__file__))

ALLOWED = ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com", "instagram.com")


def valid_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return url.startswith("http") and any(host == d or host.endswith("." + d) for d in ALLOWED)


@app.get("/")
def home():
    return send_file(os.path.join(BASE, "index.html"))


@app.post("/api/info")
def info():
    url = (request.json or {}).get("url", "").strip()
    if not valid_url(url):
        return jsonify(error="Lütfen geçerli bir TikTok veya Instagram bağlantısı gir."), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
            d = ydl.extract_info(url, download=False)
    except Exception:
        return jsonify(error="Video bulunamadı. Hesap gizli olabilir ya da bağlantı hatalı."), 422
    return jsonify(
        title=d.get("title") or "Video",
        author=d.get("uploader") or "",
        thumbnail=d.get("thumbnail") or "",
        duration=d.get("duration") or 0,
    )


@app.post("/api/download")
def download():
    body = request.json or {}
    url = body.get("url", "").strip()
    kind = body.get("kind", "mp4")
    if not valid_url(url):
        return jsonify(error="Geçersiz bağlantı."), 400
    tmp = tempfile.mkdtemp()
    opts = {
        "quiet": True,
        "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "max_filesize": 200 * 1024 * 1024,
    }
    if kind == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            d = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(d)
            if kind == "mp3":
                path = os.path.splitext(path)[0] + ".mp3"
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        return jsonify(error="İndirme başarısız oldu."), 422

    @after_this_request
    def cleanup(resp):
        shutil.rmtree(tmp, ignore_errors=True)
        return resp

    name = re.sub(r"[^\w\-]+", "_", d.get("title") or "video")[:60] + (".mp3" if kind == "mp3" else ".mp4")
    return send_file(path, as_attachment=True, download_name=name)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

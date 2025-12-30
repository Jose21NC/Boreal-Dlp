import os
import shutil
import tempfile
import threading
import time
import json
import uuid
from urllib.parse import urlparse
from urllib.request import urlretrieve
from flask import Flask, request, send_file, render_template, jsonify, Response
from yt_dlp import YoutubeDL

app = Flask(__name__)

# Registro simple en memoria de tareas de descarga
TASKS = {}
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")


def parse_formats(info: dict, has_ffmpeg: bool) -> dict:
    formats = info.get("formats") or []
    heights = set()
    progressive_heights = set()
    for f in formats:
        h = f.get("height")
        if isinstance(h, int):
            heights.add(h)
            if (f.get("acodec") and f.get("vcodec")) and f.get("ext") == "mp4":
                progressive_heights.add(h)
    qlist = []
    for h in sorted(heights, reverse=True):
        qlist.append({
            "label": f"{h}p",
            "height": h,
            "progressive": h in progressive_heights,
        })
    recommended = (f"{max(progressive_heights)}p" if progressive_heights else (qlist[0]["label"] if qlist else None))
    return {
        "qualities": qlist,
        "audioOnlyAvailable": True,
        "hasProgressive": len(progressive_heights) > 0,
        "recommended": recommended,
    }


def build_format(quality: str, audio_only: bool, has_ffmpeg: bool) -> str:
    if audio_only:
        # Solo audio: si no hay ffmpeg, intenta m4a para evitar conversión
        return "bestaudio[ext=m4a]/bestaudio/best"
    q = (quality or "best").lower()
    if q in ("best", "auto"):
        return "bestvideo+bestaudio/best"
    mapping = {
        "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]",
    }
    return mapping.get(q, "bestvideo+bestaudio/best")


def start_download_task(url: str, quality: str, audio_only: bool, ig_profile_pic: bool = False) -> str:
    task_id = uuid.uuid4().hex
    tmpdir = tempfile.mkdtemp(prefix="dlp_")
    TASKS[task_id] = {
        "id": task_id,
        "status": "queued",
        "percent": 0.0,
        "eta": None,
        "speed": None,
        "downloaded": 0,
        "total": None,
        "tmpdir": tmpdir,
        "filepath": None,
        "filename": None,
        "error": None,
    }

    def progress_hook(d):
        t = TASKS.get(task_id)
        if not t:
            return
        status = d.get("status")
        if status == "downloading":
            t["status"] = "downloading"
            t["downloaded"] = d.get("downloaded_bytes") or t["downloaded"]
            t["total"] = d.get("total_bytes") or d.get("total_bytes_estimate") or t["total"]
            pstr = d.get("percent_str")
            try:
                if pstr and pstr.endswith("%"):
                    t["percent"] = float(pstr.strip("% "))
            except Exception:
                pass
            t["eta"] = d.get("eta")
            t["speed"] = d.get("speed")
        elif status == "finished":
            # 'filename' suele estar presente cuando finaliza una parte
            t["status"] = "processing"
            t["filepath"] = d.get("filename") or t["filepath"]
        elif status == "error":
            t["status"] = "error"
            t["error"] = d.get("error") or "Error durante la descarga"

    def run():
        outtmpl = os.path.join(tmpdir, "%(title)s.%(ext)s")
        has_ffmpeg = shutil.which("ffmpeg") is not None

        # Pre-extracción para decidir plataforma y formato apropiado
        base_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "http_headers": {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"},
        }
        try:
            with YoutubeDL(base_opts) as ydl_probe:
                info_pre = ydl_probe.extract_info(url, download=False)
        except Exception:
            info_pre = {}

        extractor = (info_pre.get("extractor_key") or info_pre.get("extractor") or "").lower()
        is_instagram = "instagram" in extractor
        is_youtube = "youtube" in extractor
        is_tiktok = "tiktok" in extractor

        # Elegir formatos según plataforma
        if is_instagram:
            # En Instagram, usar 'best' para que fotos (jpg) y videos se manejen correctamente
            fmt = "best"
        else:
            fmt = build_format(quality, audio_only, has_ffmpeg)
            if not has_ffmpeg and not audio_only:
                # Preferir progresivo MP4 (YouTube típicamente hasta 720p) si no hay ffmpeg
                fmt = "best[ext=mp4]/best"

        ydl_opts = {
            "format": fmt,
            "noplaylist": True,
            "outtmpl": outtmpl,
            "restrictfilenames": True,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [progress_hook],
            "http_headers": {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"},
        }

        if has_ffmpeg and not is_instagram:
            ydl_opts["merge_output_format"] = "mp4"
            if audio_only:
                ydl_opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }]

        # Cookies desde archivo local (cookies.txt en raíz) si existe; evita uso de llavero
        if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
            ydl_opts["cookiefile"] = COOKIES_FILE

        TASKS[task_id]["status"] = "downloading"
        try:
            # Descarga / manejo especial de foto de perfil
            if ig_profile_pic:
                # Si tenemos info_pre y contiene miniatura HD, úsala; si no, intenta extraer nuevamente (posiblemente con cookies)
                pic_url = (
                    info_pre.get("profile_pic_url_hd")
                    or info_pre.get("profile_pic_url")
                    or info_pre.get("thumbnail")
                )
                if not pic_url:
                    with YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                        pic_url = (
                            info.get("profile_pic_url_hd")
                            or info.get("profile_pic_url")
                            or info.get("thumbnail")
                        )
                if not pic_url:
                    raise RuntimeError("No se encontró la foto de perfil (puede requerir inicio de sesión).")
                filename = (info_pre.get("uploader") or info_pre.get("channel") or "perfil") + ".jpg"
                dest = os.path.join(tmpdir, filename)
                urlretrieve(pic_url, dest)
                TASKS[task_id]["filepath"] = dest
                TASKS[task_id]["filename"] = filename
            else:
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    # Intentar resolver la ruta final
                    filepath = None
                    if isinstance(info, dict) and info.get("requested_downloads"):
                        rd = info["requested_downloads"][0]
                        filepath = rd.get("filepath") or rd.get("filename")
                    if not filepath:
                        filepath = ydl.prepare_filename(info)
                    # Fallback: primer archivo en la carpeta temporal
                    if not filepath or not os.path.isfile(filepath):
                        files = [
                            os.path.join(tmpdir, f)
                            for f in os.listdir(tmpdir)
                            if os.path.isfile(os.path.join(tmpdir, f))
                        ]
                        filepath = files[0] if files else None

                    if not filepath or not os.path.isfile(filepath):
                        raise RuntimeError("No se pudo determinar el archivo descargado.")

                    TASKS[task_id]["filepath"] = filepath
                    TASKS[task_id]["filename"] = os.path.basename(filepath)

            TASKS[task_id]["status"] = "done"
            TASKS[task_id]["percent"] = 100.0
        except Exception as e:
            # Mensajes más claros para Instagram con historias/perfil
            emsg = str(e)
            if is_instagram and ("log in" in emsg.lower() or "login" in emsg.lower()):
                TASKS[task_id]["error"] = "Instagram requiere inicio de sesión. Exporta cookies o usa cookies del navegador."
            else:
                TASKS[task_id]["error"] = emsg
            TASKS[task_id]["status"] = "error"

    threading.Thread(target=run, daemon=True).start()
    return task_id


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/probe", methods=["POST"])
def probe():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Falta la URL"}), 400

    has_ffmpeg = shutil.which("ffmpeg") is not None
    try:
        ydl_opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
        # Usa cookiefile local si existe
        if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
            ydl_opts["cookiefile"] = COOKIES_FILE
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        # Heurística para identificar perfiles de Instagram incluso si falla la extracción
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        path = (parsed.path or "/").strip("/")
        if "instagram.com" in host:
            # Si la ruta no contiene /p/, /stories/, /reel/ asumimos perfil
            lower = path.lower()
            if lower and not any(seg in lower for seg in ("/p/", "/stories/", "/reel/", "/tv/", "p/", "stories/", "reel/", "tv/")):
                return jsonify({
                    "type": "instagram",
                    "platform": "instagram",
                    "kind": "profile",
                    "isPlaylist": False,
                    "title": path.split("/")[0],
                    "uploader": path.split("/")[0],
                    "duration": None,
                    "thumbnail": None,
                    "igProfilePicUrl": None,
                    "formats": {"qualities": [], "audioOnlyAvailable": True, "hasProgressive": False, "recommended": None},
                    "qualities": [],
                    "supportsAudioOnly": True,
                    "hasFfmpeg": has_ffmpeg,
                    "error": str(e),
                })
        return jsonify({"type": "unknown", "error": str(e)})

    extractor = (info.get("extractor_key") or info.get("extractor") or "").lower()
    platform = "unknown"
    kind_detail = "unknown"
    if "youtube" in extractor:
        platform = "youtube"
        kind_detail = "video" if not info.get("entries") else "playlist"
    elif "tiktok" in extractor:
        platform = "tiktok"
        kind_detail = "video"
    elif "instagram" in extractor:
        platform = "instagram"
        if "user" in extractor:
            kind_detail = "profile"
        elif "story" in extractor:
            kind_detail = "story"
        else:
            kind_detail = "post"

    is_playlist = bool(info.get("entries"))
    title = info.get("title")
    uploader = info.get("uploader") or info.get("uploader_id") or info.get("channel") or info.get("creator")
    duration = info.get("duration")
    thumb = info.get("thumbnail")
    if not thumb and isinstance(info.get("thumbnails"), list):
        cand = [t for t in info["thumbnails"] if t.get("url")]
        thumb = cand[-1]["url"] if cand else None

    ig_profile_pic_url = None
    if platform == "instagram" and kind_detail == "profile":
        ig_profile_pic_url = (
            info.get("profile_pic_url_hd")
            or info.get("profile_pic_url")
            or info.get("thumbnail")
        )

    fmt = parse_formats(info, has_ffmpeg) if platform in ("youtube", "tiktok") else {"qualities": [], "audioOnlyAvailable": True, "hasProgressive": False, "recommended": None}
    # Filtrar solo progresivo para evitar calidades sin audio en el UI
    if isinstance(fmt, dict) and fmt.get("qualities"):
        fmt["qualities"] = [q for q in fmt["qualities"] if q.get("progressive")]
        # Ajustar recomendado si no hay progresivo
        if not fmt["qualities"]:
            fmt["recommended"] = None

    return jsonify({
        "type": platform,
        "platform": platform,
        "kind": kind_detail,
        "isPlaylist": is_playlist,
        "title": title,
        "uploader": uploader,
        "duration": duration,
        "thumbnail": thumb,
        "igProfilePicUrl": ig_profile_pic_url,
        "formats": fmt,
        "qualities": [q.get("label") for q in fmt.get("qualities", [])] if isinstance(fmt, dict) else [],
        "supportsAudioOnly": True,
        "hasFfmpeg": has_ffmpeg,
        "cookiesLoaded": bool(COOKIES_FILE and os.path.isfile(COOKIES_FILE)),
    })


@app.route("/download", methods=["POST"])
def download_init():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    quality = (data.get("quality") or "best").strip()
    audio_only = bool(data.get("audioOnly"))
    ig_profile_pic = bool(data.get("igProfilePic"))

    if not url:
        return jsonify({"error": "Falta la URL"}), 400

    task_id = start_download_task(url, quality, audio_only, ig_profile_pic)
    return jsonify({"id": task_id})


@app.route("/progress/<task_id>")
def progress_stream(task_id):
    def generate():
        # Enviar actualizaciones cada ~0.5s hasta finalizar
        while True:
            t = TASKS.get(task_id)
            if not t:
                yield "event: error\n" + "data: " + json.dumps({"error": "not_found"}) + "\n\n"
                break
            payload = {
                "status": t["status"],
                "percent": t["percent"],
                "eta": t["eta"],
                "speed": t["speed"],
                "downloaded": t["downloaded"],
                "total": t["total"],
            }
            yield "data: " + json.dumps(payload) + "\n\n"
            if t["status"] in ("done", "error"):
                break
            time.sleep(0.5)
    return Response(generate(), mimetype="text/event-stream")


@app.route("/file/<task_id>")
def file_download(task_id):
    t = TASKS.get(task_id)
    if not t:
        return jsonify({"error": "Tarea no encontrada"}), 404
    if t["status"] != "done" or not t.get("filepath"):
        return jsonify({"error": "Archivo no listo"}), 400

    filepath = t["filepath"]
    filename = t.get("filename") or os.path.basename(filepath)
    tmpdir = t["tmpdir"]

    def cleanup():
        try:
            if os.path.isfile(filepath):
                os.remove(filepath)
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
        finally:
            TASKS.pop(task_id, None)

    # Usamos send_file y limpiamos después con called-after envío
    resp = send_file(filepath, as_attachment=True, download_name=filename)
    # Programar limpieza ligera tras una pequeña espera para permitir envío
    threading.Thread(target=lambda: (time.sleep(2), cleanup()), daemon=True).start()
    return resp


@app.route("/cookies", methods=["POST"])
def upload_cookies():
    global COOKIES_FILE
    if "file" not in request.files:
        return jsonify({"error": "No se adjuntó archivo."}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Nombre de archivo inválido."}), 400
    # Guardar en la raíz del proyecto (cookies.txt)
    dest = COOKIES_FILE
    f.save(dest)
    COOKIES_FILE = dest
    return jsonify({"ok": True, "path": dest})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)

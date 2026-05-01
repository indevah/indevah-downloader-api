"""
INDEVAH Downloader — Backend API
Flask + yt-dlp | Deploy on Render.com free tier
"""

import os, json, subprocess, re
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)

# Allow requests from any origin (your InfinityFree frontend)
CORS(app, resources={r"/*": {"origins": "*"}})

# ─── HEALTH CHECK ─────────────────────────────────────────────────────────────
@app.route('/', methods=['GET'])
def health():
    return jsonify({"status": "ok", "service": "INDEVAH Downloader API", "version": "1.0"})

# ─── INFO ENDPOINT — returns media info + format list ─────────────────────────
@app.route('/info', methods=['POST', 'OPTIONS'])
def get_info():
    if request.method == 'OPTIONS':
        return _cors_preflight()

    data = request.get_json(silent=True)
    if not data or not data.get('url'):
        return jsonify({'error': 'Missing URL'}), 400

    url = data['url'].strip()
    if not url.startswith(('http://', 'https://')):
        return jsonify({'error': 'Invalid URL'}), 400

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'noplaylist': True,
        'socket_timeout': 20,
        'extractor_retries': 2,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        # Clean up yt-dlp verbose error messages
        msg = re.sub(r'\[.*?\]\s*', '', msg).strip()
        msg = msg[:200]
        return jsonify({'error': msg or 'Could not extract media info'}), 400
    except Exception as e:
        return jsonify({'error': str(e)[:200]}), 500

    result = build_response(info, url)
    return jsonify(result)

# ─── DOWNLOAD PROXY — streams file to browser ─────────────────────────────────
@app.route('/download', methods=['GET'])
def download_file():
    dl_url  = request.args.get('url', '').strip()
    referer = request.args.get('ref', '').strip()
    fname   = request.args.get('filename', 'download').strip()

    if not dl_url or not dl_url.startswith(('http://', 'https://')):
        return jsonify({'error': 'Invalid download URL'}), 400

    import requests as req
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': '*/*',
    }
    if referer:
        headers['Referer'] = referer

    try:
        r = req.get(dl_url, headers=headers, stream=True, timeout=30, allow_redirects=True)
        r.raise_for_status()

        resp_headers = {
            'Content-Disposition': f'attachment; filename="{fname}"',
            'Access-Control-Allow-Origin': '*',
        }
        if 'Content-Length' in r.headers:
            resp_headers['Content-Length'] = r.headers['Content-Length']
        ct = r.headers.get('Content-Type', 'application/octet-stream')
        resp_headers['Content-Type'] = ct

        def generate():
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        return Response(stream_with_context(generate()), headers=resp_headers, status=200)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── BUILD RESPONSE ────────────────────────────────────────────────────────────
def build_response(info, original_url):
    media_type = detect_type(info)

    resp = {
        'type':      media_type,
        'title':     info.get('title') or 'Untitled',
        'thumbnail': info.get('thumbnail'),
        'duration':  fmt_duration(info.get('duration')),
        'platform':  (info.get('extractor_key') or '').capitalize() or host_from(original_url),
        'uploader':  info.get('uploader') or info.get('channel'),
        'views':     fmt_views(info.get('view_count')),
    }

    resp['video_formats'] = build_video_formats(info, original_url)
    resp['audio_formats'] = build_audio_formats(info, original_url)
    resp['gif_formats']   = build_gif_formats(info, original_url) if media_type == 'gif' else []

    return resp

def detect_type(info):
    ext    = (info.get('ext') or '').lower()
    vcodec = info.get('vcodec') or ''
    acodec = info.get('acodec') or ''
    if ext == 'gif': return 'gif'
    if vcodec == 'none' and acodec not in ('none', ''): return 'audio'
    audio_exts = {'mp3','aac','ogg','flac','wav','m4a','opus','wma'}
    if ext in audio_exts and not info.get('formats'): return 'audio'
    return 'video'

def build_video_formats(info, original_url):
    formats = info.get('formats') or []
    result, seen = [], set()
    labels = {2160:'4K Ultra HD',1440:'2K QHD',1080:'Full HD',720:'HD',480:'Standard',360:'Low Quality',240:'Very Low',144:'Minimal'}

    for f in formats:
        h = f.get('height') or 0
        if h < 144: continue
        vcodec = f.get('vcodec') or 'none'
        if vcodec == 'none': continue

        acodec   = f.get('acodec') or 'none'
        has_audio = acodec != 'none'
        quality   = f'{h}p'
        key       = f'{quality}-{"a" if has_audio else "v"}'
        if key in seen: continue
        seen.add(key)

        codec = clean_codec(vcodec)
        hdr   = 'hdr' in (f.get('format_note') or '').lower() or 'hdr' in (f.get('dynamic_range') or '').lower()

        dl_url = make_proxy_url(f.get('url',''), original_url, f'{quality}{"" if has_audio else "-noaudio"}.mp4')

        result.append({
            'quality':   quality,
            'label':     (labels.get(h) or quality) + ('' if has_audio else ' (No Audio)'),
            'codec':     codec,
            'hdr':       hdr,
            'hasAudio':  has_audio,
            'size':      fmt_size(f.get('filesize') or f.get('filesize_approx')),
            'url':       dl_url,
        })

    result.sort(key=lambda x: (-int(x['quality'][:-1]), -x['hasAudio']))
    return result

def build_audio_formats(info, original_url):
    formats = info.get('formats') or []
    result, seen = [], set()

    for f in formats:
        acodec = f.get('acodec') or 'none'
        vcodec = f.get('vcodec') or 'none'
        if acodec == 'none' or vcodec != 'none': continue

        abr = int(f.get('abr') or 0)
        ext = (f.get('ext') or 'mp3').lower()
        key = f'{abr}-{ext}'
        if key in seen: continue
        seen.add(key)

        lossless = ext in ('flac', 'wav')
        quality  = 'FLAC' if lossless else (f'{abr}kbps' if abr else 'Best')
        dl_url   = make_proxy_url(f.get('url',''), original_url, f'{quality}.{"flac" if lossless else "mp3"}')

        result.append({
            'quality':  quality,
            'label':    'Lossless ' + ext.upper() if lossless else f'{ext.upper()} {abr} kbps' if abr else 'Best Quality',
            'bitrate':  f'{abr} kbps' if abr else 'Variable',
            'lossless': lossless,
            'size':     fmt_size(f.get('filesize') or f.get('filesize_approx')),
            'url':      dl_url,
        })

    if not result and info.get('url'):
        result.append({
            'quality':'Best','label':'Best Available','bitrate':'Variable',
            'lossless':False,'size':None,
            'url': make_proxy_url(info['url'], original_url, 'audio.mp3'),
        })

    result.sort(key=lambda x: -int(x['bitrate'].split()[0]) if x['bitrate'] and x['bitrate'].split()[0].isdigit() else 0)
    return result

def build_gif_formats(info, original_url):
    dl_url = info.get('url') or ''
    return [{
        'quality': 'Original',
        'label':   'Original GIF',
        'fps':     f"{info.get('fps','')}fps" if info.get('fps') else None,
        'size':    fmt_size(info.get('filesize')),
        'url':     make_proxy_url(dl_url, original_url, 'animation.gif'),
    }]

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def make_proxy_url(direct_url, referer, filename):
    if not direct_url: return ''
    from urllib.parse import quote
    base = request.host_url.rstrip('/')
    return f"{base}/download?url={quote(direct_url)}&ref={quote(referer)}&filename={quote(filename)}"

def clean_codec(s):
    s = (s or '').lower()
    if s.startswith('avc') or 'h264' in s: return 'H.264'
    if s.startswith('hev') or 'h265' in s: return 'H.265'
    if s.startswith('vp9') or s == 'vp9': return 'VP9'
    if s.startswith('av0') or s.startswith('av1'): return 'AV1'
    return s.split('.')[0].upper()[:8]

def fmt_duration(secs):
    if not secs: return None
    secs = int(secs)
    h, m, s = secs//3600, (secs%3600)//60, secs%60
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'

def fmt_views(v):
    if not v: return None
    v = int(v)
    if v >= 1_000_000_000: return f'{v/1e9:.1f}B'
    if v >= 1_000_000:     return f'{v/1e6:.1f}M'
    if v >= 1_000:         return f'{v/1e3:.1f}K'
    return str(v)

def fmt_size(b):
    if not b: return None
    b = int(b)
    if b >= 1_073_741_824: return f'~{b/1_073_741_824:.1f} GB'
    if b >= 1_048_576:     return f'~{b/1_048_576:.1f} MB'
    if b >= 1_024:         return f'~{b/1_024:.0f} KB'
    return f'{b} B'

def host_from(url):
    try:
        from urllib.parse import urlparse
        h = urlparse(url).hostname or ''
        return h.replace('www.','').split('.')[0].capitalize()
    except: return 'Unknown'

def _cors_preflight():
    r = Response()
    r.headers['Access-Control-Allow-Origin']  = '*'
    r.headers['Access-Control-Allow-Methods'] = 'POST, GET, OPTIONS'
    r.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return r, 204

# ─── RUN ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

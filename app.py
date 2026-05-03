"""
INDEVAH Downloader — Backend API v5
Flask + yt-dlp

Key fix: YouTube/TikTok CDN URLs are signed & expire quickly, and
require specific user-agents. Instead of storing raw CDN URLs, we
pass the original page URL + format_id to the /download endpoint,
which re-extracts the fresh CDN URL at download time.
"""

import os, re, base64, tempfile
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

_COOKIE_FILE = None

def get_cookie_file():
    global _COOKIE_FILE
    if _COOKIE_FILE and os.path.exists(_COOKIE_FILE):
        return _COOKIE_FILE
    raw = os.environ.get('YOUTUBE_COOKIES', '').strip()
    if not raw:
        return None
    try:
        try:
            txt = base64.b64decode(raw).decode('utf-8')
        except Exception:
            txt = raw
        tmp = tempfile.NamedTemporaryFile(
            mode='w', suffix='.txt', delete=False, prefix='yt_cookies_'
        )
        tmp.write(txt); tmp.flush(); tmp.close()
        _COOKIE_FILE = tmp.name
        print(f'[cookies] Loaded: {_COOKIE_FILE}')
        return _COOKIE_FILE
    except Exception as e:
        print(f'[cookies] Error: {e}')
        return None


# ─── HEALTH ───────────────────────────────────────────────────────────────────
@app.route('/', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok', 'version': '5.0',
        'yt_cookies': 'set' if os.environ.get('YOUTUBE_COOKIES') else 'not set',
    })

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({'status': 'ok'})

@app.route('/info', methods=['GET'])
def info_get():
    return jsonify({'status': 'ok', 'usage': 'POST {"url":"https://..."}'})


# ─── /info — extract metadata & format list ───────────────────────────────────
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

    info, err = do_extract(url)
    if info is None:
        return jsonify({'error': err}), 400
    return jsonify(build_response(info, url))


# ─── /download — re-extract fresh URL for a specific format_id ───────────────
@app.route('/download', methods=['GET'])
def download_file():
    page_url  = request.args.get('page_url', '').strip()
    format_id = request.args.get('format_id', 'best').strip()
    fname     = request.args.get('filename', 'download').strip()
    site      = request.args.get('site', '').strip().lower()

    if not page_url:
        return jsonify({'error': 'Missing page_url'}), 400

    import requests as req

    # ── Re-extract fresh CDN URL for the requested format ────────────────────
    cookie_file = get_cookie_file()
    is_yt = any(x in page_url for x in ['youtube.com', 'youtu.be'])

    ydl_opts = {
        'quiet':         True,
        'no_warnings':   True,
        'noplaylist':    True,
        'format':        format_id,
        'socket_timeout': 20,
        'http_headers': _ua_headers(site or ('youtube' if is_yt else '')),
    }
    if cookie_file and is_yt:
        ydl_opts['cookiefile'] = cookie_file
    if is_yt:
        ydl_opts['extractor_args'] = {'youtube': {'player_client': ['ios']}}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(page_url, download=False)
            cdn_url = info.get('url') or (
                info.get('requested_formats', [{}])[0].get('url') if info.get('requested_formats') else None
            )
        if not cdn_url:
            return jsonify({'error': 'Could not get download URL for this format'}), 400
    except Exception as e:
        return jsonify({'error': f'Re-extraction failed: {str(e)[:200]}'}), 500

    # ── Stream the CDN URL to the browser ────────────────────────────────────
    hdrs = _ua_headers(site or ('youtube' if is_yt else ''))
    if page_url:
        hdrs['Referer'] = page_url

    try:
        r = req.get(cdn_url, headers=hdrs, stream=True, timeout=60, allow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    out_hdrs = {
        'Content-Disposition':         f'attachment; filename="{fname}"',
        'Access-Control-Allow-Origin': '*',
        'Content-Type': r.headers.get('Content-Type', 'application/octet-stream'),
    }
    if 'Content-Length' in r.headers:
        out_hdrs['Content-Length'] = r.headers['Content-Length']

    def generate():
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                yield chunk

    return Response(stream_with_context(generate()), headers=out_hdrs, status=200)


# ─── EXTRACTION ───────────────────────────────────────────────────────────────
def do_extract(url):
    is_yt     = any(x in url for x in ['youtube.com', 'youtu.be'])
    is_tiktok = 'tiktok.com' in url
    cookie_file = get_cookie_file()

    base = {
        'quiet':             True,
        'no_warnings':       True,
        'skip_download':     True,
        'noplaylist':        True,
        'socket_timeout':    30,
        'extractor_retries': 2,
        'http_headers':      _ua_headers('youtube' if is_yt else ('tiktok' if is_tiktok else '')),
    }

    strategies = []

    if is_yt:
        # With cookies — most reliable
        if cookie_file:
            strategies.append({**base, 'cookiefile': cookie_file})
            strategies.append({**base, 'cookiefile': cookie_file,
                                'extractor_args': {'youtube': {'player_client': ['ios']}}})
        # Without cookies — mobile clients
        for client in ['ios', 'android', 'tv_embedded', 'android_vr']:
            opts = {**base, 'extractor_args': {'youtube': {'player_client': [client]}}}
            if client == 'tv_embedded':
                opts['extractor_args']['youtube']['player_skip'] = ['webpage', 'configs']
            strategies.append(opts)
    elif is_tiktok:
        strategies = [{**base, 'http_headers': _ua_headers('tiktok')}, base]
    else:
        strategies = [base]

    BOT_KEYS = ['sign in', 'bot', 'login', 'cookie', 'not a robot',
                'confirm your age', 'age-restricted']
    last_err  = 'Could not extract media info'

    for opts in strategies:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    return info, None
        except yt_dlp.utils.DownloadError as e:
            clean = re.sub(r'ERROR:\s*', '', str(e))
            clean = re.sub(r'\[[\w:]+\]\s+[\w\-]+:\s*', '', clean).strip()
            last_err = clean[:300]
            if not any(k in clean.lower() for k in BOT_KEYS):
                return None, last_err
        except Exception as e:
            last_err = str(e)[:300]

    # Give a helpful final message
    if is_yt and any(k in last_err.lower() for k in ['sign in', 'bot', 'not a robot']):
        if not cookie_file:
            last_err = (
                'YouTube is blocking this server. Add your YouTube cookies: '
                'Render dashboard → Environment → YOUTUBE_COOKIES. See README.'
            )
        else:
            last_err = (
                'YouTube rejected the cookies. '
                'They may have expired — re-export from your browser and update '
                'the YOUTUBE_COOKIES variable on Render.'
            )
    return None, last_err


# ─── BUILD RESPONSE ───────────────────────────────────────────────────────────
def build_response(info, original_url):
    is_tiktok  = 'tiktok.com' in original_url
    media_type = detect_type(info)
    return {
        'type':          media_type,
        'title':         info.get('title') or 'Untitled',
        'thumbnail':     info.get('thumbnail'),
        'duration':      fmt_duration(info.get('duration')),
        'platform':      (info.get('extractor_key') or '').capitalize() or host_from(original_url),
        'uploader':      info.get('uploader') or info.get('channel'),
        'views':         fmt_views(info.get('view_count')),
        'video_formats': build_video_formats(info, original_url, is_tiktok),
        'audio_formats': build_audio_formats(info, original_url),
        'gif_formats':   build_gif_formats(info, original_url) if media_type == 'gif' else [],
    }


def detect_type(info):
    ext    = (info.get('ext') or '').lower()
    vcodec = info.get('vcodec') or ''
    acodec = info.get('acodec') or ''
    if ext == 'gif': return 'gif'
    if vcodec == 'none' and acodec not in ('none', ''): return 'audio'
    if ext in {'mp3','aac','ogg','flac','wav','m4a','opus','wma'} and not info.get('formats'):
        return 'audio'
    return 'video'


def build_video_formats(info, original_url, is_tiktok=False):
    formats   = info.get('formats') or []
    result, seen = [], set()
    site      = 'tiktok' if is_tiktok else ''
    labels    = {
        2160:'4K Ultra HD', 1440:'2K QHD', 1080:'Full HD',
        720:'HD', 480:'Standard', 360:'Low Quality', 240:'Very Low', 144:'Minimal',
    }
    for f in formats:
        h = f.get('height') or 0
        if h < 144: continue
        vcodec = f.get('vcodec') or 'none'
        if vcodec == 'none': continue
        acodec    = f.get('acodec') or 'none'
        has_audio = acodec != 'none'
        quality   = f'{h}p'
        key       = f'{quality}-{"a" if has_audio else "v"}'
        if key in seen: continue
        seen.add(key)
        fid  = f.get('format_id', quality)
        hdr  = ('hdr' in (f.get('format_note') or '').lower() or
                'hdr' in (f.get('dynamic_range') or '').lower())
        fname = f'{quality}{"" if has_audio else "-noaudio"}.mp4'
        result.append({
            'quality':  quality,
            'label':    (labels.get(h) or quality) + ('' if has_audio else ' (No Audio)'),
            'codec':    clean_codec(vcodec),
            'hdr':      hdr,
            'hasAudio': has_audio,
            'size':     fmt_size(f.get('filesize') or f.get('filesize_approx')),
            'url':      make_dl_url(original_url, fid, fname, site),
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
        abr      = int(f.get('abr') or 0)
        ext      = (f.get('ext') or 'mp3').lower()
        key      = f'{abr}-{ext}'
        if key in seen: continue
        seen.add(key)
        lossless = ext in ('flac', 'wav')
        quality  = 'FLAC' if lossless else (f'{abr}kbps' if abr else 'Best')
        fid      = f.get('format_id', quality)
        result.append({
            'quality':  quality,
            'label':    ('Lossless ' + ext.upper()) if lossless else
                        (f'{ext.upper()} {abr} kbps' if abr else 'Best Quality'),
            'bitrate':  f'{abr} kbps' if abr else 'Variable',
            'lossless': lossless,
            'size':     fmt_size(f.get('filesize') or f.get('filesize_approx')),
            'url':      make_dl_url(original_url, fid,
                                    f'{quality}.{"flac" if lossless else "mp3"}'),
        })
    if not result:
        result.append({
            'quality': 'Best', 'label': 'Best Available Audio',
            'bitrate': 'Variable', 'lossless': False, 'size': None,
            'url': make_dl_url(original_url, 'bestaudio', 'audio.mp3'),
        })
    result.sort(key=lambda x: -(
        int(x['bitrate'].split()[0])
        if x.get('bitrate','').split()[:1] and x['bitrate'].split()[0].isdigit() else 0
    ))
    return result


def build_gif_formats(info, original_url):
    fid = (info.get('formats') or [{}])[0].get('format_id', 'best')
    return [{'quality':'Original','label':'Original GIF',
             'fps': f"{info.get('fps','')}fps" if info.get('fps') else None,
             'size': fmt_size(info.get('filesize')),
             'url': make_dl_url(original_url, fid, 'animation.gif')}]


# ─── HELPERS ──────────────────────────────────────────────────────────────────
def make_dl_url(page_url, format_id, filename, site=''):
    from urllib.parse import quote
    base = request.host_url.rstrip('/')
    s    = f'&site={quote(site)}' if site else ''
    return (f"{base}/download"
            f"?page_url={quote(page_url)}"
            f"&format_id={quote(str(format_id))}"
            f"&filename={quote(filename)}{s}")

def _ua_headers(site=''):
    if site == 'tiktok':
        return {
            'User-Agent': (
                'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
                'AppleWebKit/605.1.15 (KHTML, like Gecko) '
                'Version/17.0 Mobile/15E148 Safari/604.1'
            ),
            'Referer': 'https://www.tiktok.com/',
            'Accept-Language': 'en-US,en;q=0.9',
        }
    if site in ('youtube', 'youtu'):
        return {
            'User-Agent': (
                'com.google.android.youtube/17.36.4 '
                '(Linux; U; Android 12; GB) gzip'
            ),
            'Accept-Language': 'en-US,en;q=0.9',
        }
    return {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept-Language': 'en-US,en;q=0.9',
    }

def clean_codec(s):
    s = (s or '').lower()
    if s.startswith('avc') or 'h264' in s: return 'H.264'
    if s.startswith('hev') or 'h265' in s: return 'H.265'
    if s.startswith('vp9') or s == 'vp9':  return 'VP9'
    if s.startswith(('av0','av1')):         return 'AV1'
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

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

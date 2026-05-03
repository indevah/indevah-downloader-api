"""
INDEVAH Downloader — Backend API v4
Flask + yt-dlp

YouTube: Uses cookies (exported from browser) stored as YOUTUBE_COOKIES env var.
         Falls back to ios/android clients if no cookies set.
TikTok:  Uses yt-dlp directly for download URLs with proper headers.
Others:  Standard yt-dlp extraction.
"""

import os, re, base64, tempfile
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ── Cookie file setup ────────────────────────────────────────────────────────
# Store your YouTube cookies.txt content as base64 in YOUTUBE_COOKIES env var
# on Render. See README for how to export cookies.
_COOKIE_FILE = None

def get_cookie_file():
    """Write cookies from env var to a temp file once, reuse path."""
    global _COOKIE_FILE
    if _COOKIE_FILE and os.path.exists(_COOKIE_FILE):
        return _COOKIE_FILE

    cookies_b64 = os.environ.get('YOUTUBE_COOKIES', '').strip()
    if not cookies_b64:
        return None

    try:
        # Support both raw cookies.txt content and base64-encoded
        try:
            cookies_txt = base64.b64decode(cookies_b64).decode('utf-8')
        except Exception:
            cookies_txt = cookies_b64   # already plain text

        tmp = tempfile.NamedTemporaryFile(
            mode='w', suffix='.txt', delete=False, prefix='yt_cookies_'
        )
        tmp.write(cookies_txt)
        tmp.flush()
        tmp.close()
        _COOKIE_FILE = tmp.name
        print(f"[cookies] Loaded cookie file: {_COOKIE_FILE}")
        return _COOKIE_FILE
    except Exception as e:
        print(f"[cookies] Failed to write cookie file: {e}")
        return None


# ─── HEALTH CHECKS ────────────────────────────────────────────────────────────
@app.route('/', methods=['GET'])
def health():
    has_cookies = bool(os.environ.get('YOUTUBE_COOKIES', '').strip())
    return jsonify({
        "status":      "ok",
        "service":     "INDEVAH Downloader API",
        "version":     "4.0",
        "yt_cookies":  "set" if has_cookies else "not set (YouTube may be blocked)",
    })

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "ok"})

@app.route('/info', methods=['GET'])
def info_get():
    return jsonify({"status": "ok", "usage": 'POST {"url":"https://..."} here'})


# ─── INFO ENDPOINT ────────────────────────────────────────────────────────────
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

    info, err = extract(url)
    if info is None:
        return jsonify({'error': err}), 400

    return jsonify(build_response(info, url))


# ─── EXTRACTION ───────────────────────────────────────────────────────────────
def extract(url):
    is_yt     = any(x in url for x in ['youtube.com', 'youtu.be'])
    is_tiktok = 'tiktok.com' in url

    base_opts = {
        'quiet':             True,
        'no_warnings':       True,
        'skip_download':     True,
        'noplaylist':        True,
        'socket_timeout':    30,
        'extractor_retries': 2,
        'http_headers': {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
            'Accept-Language': 'en-US,en;q=0.9',
        },
    }

    cookie_file = get_cookie_file()

    if is_yt:
        strategies = _yt_strategies(base_opts, cookie_file)
    elif is_tiktok:
        strategies = _tiktok_strategies(base_opts)
    else:
        strategies = [base_opts]

    BOT_KEYS = ['sign in', 'bot', 'login', 'cookie', 'confirm your age',
                'not a robot', 'age-restricted']
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
            # Stop retrying if it's not a bot/auth error
            if not any(k in clean.lower() for k in BOT_KEYS):
                return None, last_err
        except Exception as e:
            last_err = str(e)[:300]

    # All strategies exhausted
    if is_yt and any(k in last_err.lower() for k in ['sign in', 'bot', 'not a robot']):
        if not cookie_file:
            last_err = (
                'YouTube is blocking this server\'s IP. '
                'Fix: Add your YouTube cookies to Render. '
                'Go to your Render dashboard → Environment → add variable '
                'YOUTUBE_COOKIES with the content of your cookies.txt file. '
                'See the README for step-by-step instructions.'
            )
        else:
            last_err = (
                'YouTube rejected the cookies. '
                'Your cookies may have expired — please re-export them from your browser '
                'and update the YOUTUBE_COOKIES environment variable on Render.'
            )

    return None, last_err


def _yt_strategies(base_opts, cookie_file):
    """Build YouTube extraction strategies, cookies-first."""
    strategies = []

    # ── With cookies (most reliable if set) ──────────────────────────────────
    if cookie_file:
        strategies.append({
            **base_opts,
            'cookiefile': cookie_file,
            # Default web client with cookies passes bot check
        })
        strategies.append({
            **base_opts,
            'cookiefile': cookie_file,
            'extractor_args': {'youtube': {'player_client': ['ios']}},
        })

    # ── Without cookies: mobile/TV clients ───────────────────────────────────
    strategies += [
        # ios — Apple mobile app client, least bot-checked
        {**base_opts, 'extractor_args': {
            'youtube': {'player_client': ['ios']}
        }},
        # android
        {**base_opts, 'extractor_args': {
            'youtube': {'player_client': ['android']}
        }},
        # tv_embedded — Smart TV, no sign-in enforcement
        {**base_opts, 'extractor_args': {
            'youtube': {
                'player_client': ['tv_embedded'],
                'player_skip':   ['webpage', 'configs'],
            }
        }},
        # android_vr — VR headset client, rarely blocked
        {**base_opts, 'extractor_args': {
            'youtube': {'player_client': ['android_vr']}
        }},
    ]
    return strategies


def _tiktok_strategies(base_opts):
    """TikTok needs a mobile user agent to get working CDN URLs."""
    mobile_opts = {
        **base_opts,
        'http_headers': {
            'User-Agent': (
                'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
                'AppleWebKit/605.1.15 (KHTML, like Gecko) '
                'Version/17.0 Mobile/15E148 Safari/604.1'
            ),
            'Referer':        'https://www.tiktok.com/',
            'Accept-Language': 'en-US,en;q=0.9',
        },
        # Tell yt-dlp to get direct CDN URL without extra processing
        'extractor_args': {
            'tiktok': {'webpage_download': ['0']}
        },
    }
    return [mobile_opts, base_opts]


# ─── DOWNLOAD PROXY ───────────────────────────────────────────────────────────
@app.route('/download', methods=['GET'])
def download_file():
    dl_url  = request.args.get('url', '').strip()
    referer = request.args.get('ref', '').strip()
    fname   = request.args.get('filename', 'download').strip()
    site    = request.args.get('site', '').strip().lower()

    if not dl_url or not dl_url.startswith(('http://', 'https://')):
        return jsonify({'error': 'Invalid download URL'}), 400

    import requests as req

    # Build appropriate headers per platform
    if 'tiktok' in dl_url or site == 'tiktok':
        hdrs = {
            'User-Agent': (
                'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
                'AppleWebKit/605.1.15 (KHTML, like Gecko) '
                'Version/17.0 Mobile/15E148 Safari/604.1'
            ),
            'Referer':         'https://www.tiktok.com/',
            'Accept':          'video/webm,video/mp4,video/*;q=0.9,*/*;q=0.8',
            'Accept-Encoding': 'identity',
            'Range':           'bytes=0-',
        }
    elif 'googlevideo' in dl_url or 'youtube' in (referer or ''):
        hdrs = {
            'User-Agent': (
                'com.google.android.youtube/17.36.4 '
                '(Linux; U; Android 12; GB) gzip'
            ),
            'Accept':          '*/*',
            'Accept-Encoding': 'identity',
        }
    else:
        hdrs = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
            'Accept':          '*/*',
            'Accept-Encoding': 'identity',
        }

    if referer:
        hdrs['Referer'] = referer

    try:
        r = req.get(dl_url, headers=hdrs, stream=True, timeout=60, allow_redirects=True)
        r.raise_for_status()

        out_hdrs = {
            'Content-Disposition':         f'attachment; filename="{fname}"',
            'Access-Control-Allow-Origin': '*',
        }
        if 'Content-Length' in r.headers:
            out_hdrs['Content-Length'] = r.headers['Content-Length']
        out_hdrs['Content-Type'] = r.headers.get('Content-Type', 'application/octet-stream')

        def generate():
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        return Response(stream_with_context(generate()), headers=out_hdrs, status=200)

    except Exception as e:
        return jsonify({'error': str(e)}), 500


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
    formats = info.get('formats') or []
    result, seen = [], set()
    labels = {
        2160:'4K Ultra HD', 1440:'2K QHD', 1080:'Full HD',
        720:'HD', 480:'Standard', 360:'Low Quality', 240:'Very Low', 144:'Minimal',
    }
    site_param = 'tiktok' if is_tiktok else ''

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
        hdr = ('hdr' in (f.get('format_note') or '').lower() or
               'hdr' in (f.get('dynamic_range') or '').lower())
        fname = f'{quality}{"" if has_audio else "-noaudio"}.mp4'
        result.append({
            'quality':  quality,
            'label':    (labels.get(h) or quality) + ('' if has_audio else ' (No Audio)'),
            'codec':    clean_codec(vcodec),
            'hdr':      hdr,
            'hasAudio': has_audio,
            'size':     fmt_size(f.get('filesize') or f.get('filesize_approx')),
            'url':      make_proxy_url(f.get('url',''), original_url, fname, site_param),
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
        result.append({
            'quality':  quality,
            'label':    ('Lossless ' + ext.upper()) if lossless else
                        (f'{ext.upper()} {abr} kbps' if abr else 'Best Quality'),
            'bitrate':  f'{abr} kbps' if abr else 'Variable',
            'lossless': lossless,
            'size':     fmt_size(f.get('filesize') or f.get('filesize_approx')),
            'url':      make_proxy_url(f.get('url',''), original_url,
                                       f'{quality}.{"flac" if lossless else "mp3"}'),
        })
    if not result and info.get('url'):
        result.append({'quality':'Best','label':'Best Available','bitrate':'Variable',
                       'lossless':False,'size':None,
                       'url':make_proxy_url(info['url'], original_url, 'audio.mp3')})
    result.sort(key=lambda x: -(
        int(x['bitrate'].split()[0])
        if x.get('bitrate','').split()[:1] and x['bitrate'].split()[0].isdigit() else 0
    ))
    return result


def build_gif_formats(info, original_url):
    return [{'quality':'Original','label':'Original GIF',
             'fps': f"{info.get('fps','')}fps" if info.get('fps') else None,
             'size': fmt_size(info.get('filesize')),
             'url': make_proxy_url(info.get('url',''), original_url, 'animation.gif')}]


# ─── HELPERS ──────────────────────────────────────────────────────────────────
def make_proxy_url(direct_url, referer, filename, site=''):
    if not direct_url: return ''
    from urllib.parse import quote
    base = request.host_url.rstrip('/')
    s = f'&site={quote(site)}' if site else ''
    return f"{base}/download?url={quote(direct_url)}&ref={quote(referer)}&filename={quote(filename)}{s}"

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


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

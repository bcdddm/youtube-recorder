'''Platform helpers: detect a pasted source URL, build per-platform watch
URLs, and discover items. YouTube unchanged; adds Bilibili (yt-dlp space) and
Podcast (generic RSS enclosure).'''
from __future__ import annotations
import re
import hashlib

YOUTUBE = 'youtube'
BILIBILI = 'bilibili'
PODCAST = 'podcast'

_BILI = re.compile('bilibili[.]com/([0-9]+)')
_ITUNES = 'http://www.itunes.com/dtds/podcast-1.0.dtd'
_UA = {'User-Agent': 'YouTubeRecorder/1.0 (personal use)'}


def detect(url):
    '''Fast, network-free detection. Returns a dict for Bilibili; None for
    YouTube (handled by the existing resolver) and for possible podcasts
    (validated separately via podcast_info).'''
    low = (url or '').strip().lower()
    if not low:
        return None
    if 'bilibili.com' in low:
        m = _BILI.search(low)
        if m:
            uid = m.group(1)
            return {'platform': BILIBILI, 'channel_id': 'bili:' + uid,
                    'url': 'https://space.bilibili.com/' + uid + '/video',
                    'name': None}
    return None


def watch_url(platform, native_id):
    if platform == BILIBILI:
        return 'https://www.bilibili.com/video/' + native_id
    if platform == PODCAST:
        return ''
    return 'https://www.youtube.com/watch?v=' + native_id


# --- Bilibili ---------------------------------------------------------------

def _space_url(channel_id):
    return 'https://space.bilibili.com/' + channel_id.split(':', 1)[-1] + '/video'


def _flat(channel_id, limit):
    try:
        import yt_dlp
    except ImportError:
        return None
    opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True,
            'playlistend': limit, 'skip_download': True}
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            return y.extract_info(_space_url(channel_id), download=False)
    except Exception:
        return None


def fetch_bilibili(channel_id, limit=15):
    info = _flat(channel_id, limit) or {}
    out = []
    for e in info.get('entries') or []:
        vid = e.get('id')
        if vid:
            out.append({'video_id': vid, 'title': e.get('title') or ''})
    return out


def bili_name(channel_id):
    info = _flat(channel_id, 1) or {}
    return info.get('uploader') or info.get('channel') or info.get('title')


# --- Podcast ----------------------------------------------------------------

def _parse_duration(text):
    if not text:
        return 0
    text = text.strip()
    if text.isdigit():
        return int(text)
    try:
        parts = [int(p) for p in text.split(':')]
    except ValueError:
        return 0
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


def _pub_iso(text):
    if not text:
        return None
    try:
        from email.utils import parsedate_to_datetime
        from datetime import timezone
        dt = parsedate_to_datetime(text)
        if dt is None:
            return None
        return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception:
        return None


def _ssl_ctx():
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _feed_root(feed_url):
    import urllib.request
    import xml.etree.ElementTree as ET
    req = urllib.request.Request(feed_url, headers=_UA)
    with urllib.request.urlopen(req, timeout=30, context=_ssl_ctx()) as resp:
        data = resp.read()
    return ET.fromstring(data)


def podcast_info(feed_url):
    '''Return (title, episode_count) if this is a podcast RSS with audio
    enclosures, else None. Used to validate a pasted feed at add time.'''
    try:
        root = _feed_root(feed_url)
    except Exception:
        return None
    chan = root.find('channel')
    if chan is None:
        return None
    items = chan.findall('item')
    if not any(it.find('enclosure') is not None for it in items):
        return None
    return ((chan.findtext('title') or feed_url).strip(), len(items))


def fetch_podcast(feed_url, limit=20):
    try:
        root = _feed_root(feed_url)
    except Exception:
        return []
    chan = root.find('channel')
    if chan is None:
        return []
    out = []
    for it in chan.findall('item')[:limit]:
        enc = it.find('enclosure')
        if enc is None:
            continue
        murl = enc.get('url')
        if not murl:
            continue
        guid = (it.findtext('guid') or murl).strip()
        vid = 'pod' + hashlib.sha1(guid.encode('utf-8')).hexdigest()[:14]
        dur = _parse_duration(it.findtext('{' + _ITUNES + '}duration'))
        out.append({'video_id': vid, 'title': (it.findtext('title') or '').strip(),
                    'published': _pub_iso(it.findtext('pubDate')),
                    'media_url': murl, 'duration_sec': dur})
    return out

import os
import time
import uuid
import threading
import logging
import glob
import base64
from flask import Flask, render_template, request, jsonify, send_file, make_response
from yt_dlp import YoutubeDL
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# --- Configuration ---
DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- COOKIE SETUP ---
COOKIE_FILE = 'cookies.txt'
if os.path.exists(COOKIE_FILE):
    logger.info(f"✅ Found {COOKIE_FILE}.")
elif os.environ.get('YOUTUBE_COOKIES'):
    try:
        with open(COOKIE_FILE, 'wb') as f:
            f.write(base64.b64decode(os.environ.get('YOUTUBE_COOKIES')))
    except Exception: pass

active_downloads = {}

# --- CLEANUP ---
def clean_stale_files():
    try:
        now = time.time()
        for filename in os.listdir(DOWNLOAD_FOLDER):
            filepath = os.path.join(DOWNLOAD_FOLDER, filename)
            if os.path.isfile(filepath):
                if now - os.path.getctime(filepath) > 600: 
                    os.remove(filepath)
    except Exception: pass

scheduler = BackgroundScheduler()
scheduler.add_job(func=clean_stale_files, trigger="interval", minutes=5)
scheduler.start()

# --- PWA BACKGROUND ENGINE (CRITICAL) ---
@app.route('/manifest.json')
def manifest():
    return jsonify({
        "name": "Music Player",
        "short_name": "Music",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#121212",
        "theme_color": "#1db954",
        "icons": [{"src": "https://cdn-icons-png.flaticon.com/512/727/727218.png", "sizes": "512x512", "type": "image/png"}]
    })

@app.route('/sw.js')
def service_worker():
    # This script runs in the background to serve audio from Cache
    js_content = """
    self.addEventListener('install', event => self.skipWaiting());
    self.addEventListener('activate', event => event.waitUntil(clients.claim()));
    
    self.addEventListener('fetch', event => {
        // If the browser asks for the virtual song file...
        if (event.request.url.includes('/virtual-song.mp3')) {
            event.respondWith(
                caches.open('temp-music-cache').then(cache => {
                    return cache.match('/virtual-song.mp3').then(response => {
                        // Serve it from the Internal Cache
                        return response || new Response("Buffering...", {status: 200});
                    });
                })
            );
        }
    });
    """
    response = make_response(js_content)
    response.headers['Content-Type'] = 'application/javascript'
    return response

# --- DOWNLOAD LOGIC ---
def run_download(url, file_id):
    active_downloads[file_id] = 'downloading'
    output_template = os.path.join(DOWNLOAD_FOLDER, f"{file_id}.%(ext)s")
    
    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/best',
        'outtmpl': output_template,
        'noplaylist': True,
        'quiet': True,
        'cookiefile': COOKIE_FILE if os.path.exists(COOKIE_FILE) else None,
        'source_address': '0.0.0.0',
        'socket_timeout': 60,
        'retries': 30,
        'fragment_retries': 30,
        'retry_sleep': 5,
        'http_chunk_size': 1048576, 
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        active_downloads[file_id] = 'completed'
    except Exception:
        active_downloads[file_id] = 'error'

# --- API ROUTES ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/search', methods=['POST'])
def search():
    try:
        data = request.get_json()
        query = data.get('query')
        
        ydl_opts = {
            'noplaylist': True, 'quiet': True, 'default_search': 'ytsearch1',
            'cookiefile': COOKIE_FILE if os.path.exists(COOKIE_FILE) else None,
        }
        
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
            video = info['entries'][0] if 'entries' in info else info
            return jsonify({
                'title': video.get('title'),
                'url': video.get('webpage_url'),
                'thumbnail': video.get('thumbnail'),
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/fetch_song')
def fetch_song():
    video_url = request.args.get('url')
    session_id = request.args.get('session_id')
    file_id = f"{session_id}_{uuid.uuid4()}"
    
    thread = threading.Thread(target=run_download, args=(video_url, file_id))
    thread.start()

    timeout = 0
    filepath = None
    while timeout < 120: 
        if active_downloads.get(file_id) == 'completed':
            matches = glob.glob(os.path.join(DOWNLOAD_FOLDER, f"{file_id}*"))
            if matches:
                filepath = matches[0]
                break
        elif active_downloads.get(file_id) == 'error':
            return "Error", 500
        time.sleep(1)
        timeout += 1

    if filepath:
        return send_file(filepath, as_attachment=True, download_name="song.m4a")
    return "Timeout", 504

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
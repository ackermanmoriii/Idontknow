import os
import time
import uuid
import threading
import logging
import glob
import base64
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from yt_dlp import YoutubeDL
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# --- Configuration ---
DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- COOKIE SETUP (Critical for Auth) ---
COOKIE_FILE = 'cookies.txt'

# 1. Check if cookies exist
if os.path.exists(COOKIE_FILE):
    logger.info(f"✅ Found {COOKIE_FILE}, using it for authentication.")
elif os.environ.get('YOUTUBE_COOKIES'):
    # 2. Check Environment Variable (Secure Fallback)
    try:
        with open(COOKIE_FILE, 'wb') as f:
            f.write(base64.b64decode(os.environ.get('YOUTUBE_COOKIES')))
        logger.info("✅ Created cookies.txt from Env.")
    except Exception as e:
        logger.error(f"❌ Cookie Error: {e}")
else:
    logger.warning("⚠️ WARNING: No cookies found.")

# Track active downloads
active_downloads = {}

# --- RESILIENCE: Background Cleanup ---
# We keep files for 10 minutes max. 
# Since the browser will save the file to RAM, we can clean server files quickly.
def clean_stale_files():
    try:
        now = time.time()
        cutoff = 10 * 60  # 10 minutes
        for filename in os.listdir(DOWNLOAD_FOLDER):
            filepath = os.path.join(DOWNLOAD_FOLDER, filename)
            if os.path.isfile(filepath):
                if now - os.path.getctime(filepath) > cutoff:
                    os.remove(filepath)
                    logger.info(f"🧹 Cleanup: Removed {filename}")
    except Exception as e:
        logger.error(f"Cleanup Error: {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(func=clean_stale_files, trigger="interval", minutes=2)
scheduler.start()

# --- Helper: Delete Session Files ---
def delete_session_files(session_id):
    if not session_id: return
    try:
        pattern = os.path.join(DOWNLOAD_FOLDER, f"{session_id}_*")
        for f in glob.glob(pattern):
            try:
                os.remove(f)
            except OSError: pass
    except Exception: pass

# --- DOWNLOAD LOGIC (VPN OPTIMIZED) ---
def download_video_thread(url, filename):
    active_downloads[filename] = 'downloading'
    output_template = os.path.join(DOWNLOAD_FOLDER, f"{filename}.%(ext)s")
    
    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/best',
        'outtmpl': output_template,
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        
        # Auth
        'cookiefile': COOKIE_FILE if os.path.exists(COOKIE_FILE) else None,
        
        # VPN Resilience
        'source_address': '0.0.0.0',
        'socket_timeout': 60,
        'retries': 30,
        'fragment_retries': 30,
        'retry_sleep': 5,
        
        # Anti-Stall (Micro-chunks)
        'http_chunk_size': 1048576, 
        
        # Speed Cap (Anti-Ban)
        'ratelimit': 10000000, 
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        active_downloads[filename] = 'completed'
        logger.info(f"✅ Downloaded: {filename}")
    except Exception as e:
        logger.error(f"❌ Failed: {e}")
        active_downloads[filename] = 'error'

# --- ROUTES ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/favicon.ico')
def favicon():
    return "", 204

@app.route('/search', methods=['POST'])
def search():
    try:
        data = request.get_json()
        query = data.get('query')
        session_id = request.headers.get('X-Session-ID')

        if not query: return jsonify({'error': 'No query'}), 400
        
        # Clean previous search from server to save space
        if session_id: delete_session_files(session_id)

        ydl_opts = {
            'noplaylist': True, 'quiet': True, 'default_search': 'ytsearch1',
            'no_warnings': True, 'source_address': '0.0.0.0',
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
        logger.exception("Search Failed")
        return jsonify({'error': str(e)}), 500

@app.route('/stream')
def stream():
    video_url = request.args.get('url')
    session_id = request.args.get('session_id')
    if not video_url or not session_id: return "Missing Data", 400

    file_id = f"{session_id}_{uuid.uuid4()}"
    
    # Start download thread
    thread = threading.Thread(target=download_video_thread, args=(video_url, file_id))
    thread.start()

    def generate():
        filepath = None
        timeout = 0
        
        # 1. Wait for file to exist and have headers
        while True:
            matches = glob.glob(os.path.join(DOWNLOAD_FOLDER, f"{file_id}*"))
            if matches:
                temp_path = matches[0]
                if os.path.exists(temp_path) and os.path.getsize(temp_path) > 2048:
                    filepath = temp_path
                    break
            
            if active_downloads.get(file_id) == 'error': return
            if timeout > 120: return 
            time.sleep(0.5)
            timeout += 1

        # 2. Stream to browser
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(1024 * 64)
                if chunk:
                    yield chunk
                else:
                    status = active_downloads.get(file_id)
                    if status in ['completed', 'error']:
                        break
                    time.sleep(0.2)

    return Response(stream_with_context(generate()), mimetype="audio/mp4", headers={
        "Content-Disposition": f"attachment; filename=song.m4a"
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)

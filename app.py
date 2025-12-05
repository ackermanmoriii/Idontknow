import os
import time
import uuid
import logging
import glob
import base64
import threading
from flask import Flask, render_template, request, jsonify, send_file
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

# --- COOKIE SETUP ---
COOKIE_FILE = 'cookies.txt'
if os.path.exists(COOKIE_FILE):
    logger.info(f"✅ Found {COOKIE_FILE}.")
elif os.environ.get('YOUTUBE_COOKIES'):
    try:
        with open(COOKIE_FILE, 'wb') as f:
            f.write(base64.b64decode(os.environ.get('YOUTUBE_COOKIES')))
        logger.info("✅ Created cookies.txt from Env.")
    except Exception as e:
        logger.error(f"❌ Cookie Error: {e}")

# Track active downloads
active_downloads = {}

# --- CLEANUP TASK ---
def clean_stale_files():
    try:
        now = time.time()
        # Keep files for 10 mins (enough time to send to client)
        cutoff = 10 * 60 
        for filename in os.listdir(DOWNLOAD_FOLDER):
            filepath = os.path.join(DOWNLOAD_FOLDER, filename)
            if os.path.isfile(filepath):
                if now - os.path.getctime(filepath) > cutoff:
                    os.remove(filepath)
    except Exception:
        pass

scheduler = BackgroundScheduler()
scheduler.add_job(func=clean_stale_files, trigger="interval", minutes=5)
scheduler.start()

# --- HELPER: Delete Session ---
def delete_session_files(session_id):
    if not session_id: return
    try:
        pattern = os.path.join(DOWNLOAD_FOLDER, f"{session_id}_*")
        for f in glob.glob(pattern):
            try: os.remove(f)
            except OSError: pass
    except Exception: pass

# --- DOWNLOAD LOGIC ---
def run_download(url, file_id):
    active_downloads[file_id] = 'downloading'
    output_template = os.path.join(DOWNLOAD_FOLDER, f"{file_id}.%(ext)s")
    
    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/best',
        'outtmpl': output_template,
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'cookiefile': COOKIE_FILE if os.path.exists(COOKIE_FILE) else None,
        'source_address': '0.0.0.0',
        # Robust Network Settings
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
        logger.info(f"✅ Downloaded: {file_id}")
    except Exception as e:
        logger.error(f"❌ Failed: {e}")
        active_downloads[file_id] = 'error'

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

@app.route('/fetch_song')
def fetch_song():
    video_url = request.args.get('url')
    session_id = request.args.get('session_id')
    if not video_url or not session_id: return "Missing Data", 400

    file_id = f"{session_id}_{uuid.uuid4()}"
    
    # 1. Start Download in Thread
    thread = threading.Thread(target=run_download, args=(video_url, file_id))
    thread.start()

    # 2. WAIT for completion (Block until done)
    # This prevents the "6 second" bug by ensuring file is 100% ready.
    timeout = 0
    filepath = None
    
    while timeout < 120: # Wait up to 120 seconds for slow VPN
        if active_downloads.get(file_id) == 'completed':
            # Find the file
            matches = glob.glob(os.path.join(DOWNLOAD_FOLDER, f"{file_id}*"))
            if matches:
                filepath = matches[0]
                break
        elif active_downloads.get(file_id) == 'error':
            return "Download Error", 500
            
        time.sleep(1) # Check every second
        timeout += 1

    if filepath and os.path.exists(filepath):
        # Send the file with correct length headers
        return send_file(filepath, as_attachment=True, download_name="song.m4a")
    else:
        return "Timeout or File Missing", 504

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)

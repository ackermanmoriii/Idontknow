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

# Logging Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- COOKIE SETUP (Critical for Auth) ---
COOKIE_FILE = 'cookies.txt'

# Logic: Check for file existence first
if os.path.exists(COOKIE_FILE):
    logger.info(f"✅ Found {COOKIE_FILE}, using it for authentication.")
elif os.environ.get('YOUTUBE_COOKIES'):
    # Fallback: Create file from Env Var if file is missing
    try:
        with open(COOKIE_FILE, 'wb') as f:
            f.write(base64.b64decode(os.environ.get('YOUTUBE_COOKIES')))
        logger.info("✅ Created cookies.txt from Environment Variable.")
    except Exception as e:
        logger.error(f"❌ Failed to load cookies from Env Var: {e}")
else:
    logger.warning("⚠️ WARNING: No cookies found. YouTube will likely block this request.")

# --- Track active downloads ---
active_downloads = {}

# --- RESILIENCE: Background Failsafe Cleanup ---
def clean_stale_files():
    try:
        now = time.time()
        cutoff = 20 * 60  # 20 minutes expiration
        for filename in os.listdir(DOWNLOAD_FOLDER):
            filepath = os.path.join(DOWNLOAD_FOLDER, filename)
            if os.path.isfile(filepath):
                if now - os.path.getctime(filepath) > cutoff:
                    os.remove(filepath)
                    logger.info(f"🧹 Failsafe Cleanup: Removed {filename}")
    except Exception as e:
        logger.error(f"Cleanup Error: {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(func=clean_stale_files, trigger="interval", minutes=5)
scheduler.start()

# --- Helper: Precise Session Cleanup ---
def delete_session_files(session_id):
    if not session_id: return
    try:
        pattern = os.path.join(DOWNLOAD_FOLDER, f"{session_id}_*")
        for f in glob.glob(pattern):
            try:
                os.remove(f)
                logger.info(f"🗑️ Session Cleanup: Deleted {os.path.basename(f)}")
            except OSError:
                pass
    except Exception as e:
        logger.error(f"Error in delete_session_files: {e}")

# --- Helper: Download Logic (Optimized for VPN) ---
def download_video_thread(url, filename):
    active_downloads[filename] = 'downloading'
    output_template = os.path.join(DOWNLOAD_FOLDER, f"{filename}.%(ext)s")
    
    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/best',
        'outtmpl': output_template,
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        
        # --- Authentication ---
        'cookiefile': COOKIE_FILE if os.path.exists(COOKIE_FILE) else None,
        
        # --- VPN/Network Resilience Settings ---
        'source_address': '0.0.0.0', # Force IPv4
        'socket_timeout': 60,        # Extended timeout for slow VPN handshake
        'retries': 30,               # Retry aggressively on connection drop
        'fragment_retries': 30,      # Retry individual chunks
        'retry_sleep': 5,            # Wait 5s before retrying to avoid "hammering"
        
        # --- Micro-Chunking (The Fix for "Regressing Progress") ---
        # Downloads in small 1MB pieces. If connection drops, we only lose 1MB.
        'http_chunk_size': 1048576, 
        
        # --- Anti-Throttling ---
        # Cap speed to 10MB/s to avoid looking like a bot/DDoS
        'ratelimit': 10000000, 
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        active_downloads[filename] = 'completed'
        logger.info(f"✅ Download Complete: {filename}")
    except Exception as e:
        logger.error(f"❌ Download Failed: {e}")
        active_downloads[filename] = 'error'

# --- Routes ---

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

        if not query:
            return jsonify({'error': 'Please enter a song name'}), 400

        if session_id:
            delete_session_files(session_id)

        # Search Options
        ydl_opts = {
            'noplaylist': True, 
            'quiet': True, 
            'default_search': 'ytsearch1',
            'no_warnings': True, 
            'source_address': '0.0.0.0',
            'socket_timeout': 15, # Shorter timeout for search
            'cookiefile': COOKIE_FILE if os.path.exists(COOKIE_FILE) else None,
        }
        
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
            video = info['entries'][0] if 'entries' in info else info
            
            return jsonify({
                'title': video.get('title', 'Unknown'),
                'url': video.get('webpage_url'),
                'thumbnail': video.get('thumbnail', ''),
            })

    except Exception as e:
        logger.exception("Search Failed")
        return jsonify({'error': str(e)}), 500

@app.route('/stream')
def stream():
    video_url = request.args.get('url')
    session_id = request.args.get('session_id')
    
    if not video_url or not session_id:
        return "Missing Data", 400

    file_id = f"{session_id}_{uuid.uuid4()}"
    
    thread = threading.Thread(target=download_video_thread, args=(video_url, file_id))
    thread.start()

    def generate():
        filepath = None
        timeout = 0
        
        while True:
            matches = glob.glob(os.path.join(DOWNLOAD_FOLDER, f"{file_id}*"))
            if matches:
                temp_path = matches[0]
                # Wait for 2KB headers
                if os.path.exists(temp_path) and os.path.getsize(temp_path) > 2048:
                    filepath = temp_path
                    break
            
            if active_downloads.get(file_id) == 'error': return
            if timeout > 120: return # Allow 60 seconds (0.5 * 120) for initial connect (slow VPN)
            
            time.sleep(0.5)
            timeout += 1

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

@app.route('/cleanup_session', methods=['POST'])
def cleanup_session():
    try:
        if request.is_json:
            data = request.get_json()
            session_id = data.get('session_id')
        else:
            session_id = request.data.decode('utf-8')

        if session_id:
            delete_session_files(session_id)
            return "OK", 200
        return "No Session ID", 400
    except Exception as e:
        logger.error(f"Error in cleanup endpoint: {e}")
        return "Error", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)

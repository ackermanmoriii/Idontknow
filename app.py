import os
import time
import uuid
import threading
import logging
import glob
import shutil
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from yt_dlp import YoutubeDL
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# --- Configuration ---
# Files are stored on the server to prevent mobile storage clutter
DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

# Logging Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Track active downloads {filename: status}
active_downloads = {}

# --- RESILIENCE: Background Failsafe Cleanup ---
# If a user's VPN drops and they can't send the "Close" signal,
# this background task acts as a safety net to clean files.
def clean_stale_files():
    try:
        now = time.time()
        cutoff = 20 * 60  # 20 minutes expiration
        for filename in os.listdir(DOWNLOAD_FOLDER):
            filepath = os.path.join(DOWNLOAD_FOLDER, filename)
            if os.path.isfile(filepath):
                if now - os.path.getctime(filepath) > cutoff:
                    os.remove(filepath)
                    logger.info(f"Failsafe Cleanup: Removed stale file {filename}")
    except Exception as e:
        logger.error(f"Cleanup Error: {e}")

# Run cleanup every 5 minutes
scheduler = BackgroundScheduler()
scheduler.add_job(func=clean_stale_files, trigger="interval", minutes=5)
scheduler.start()

# --- Helper: Precise Session Cleanup ---
def delete_session_files(session_id):
    """
    Deletes files specific to a user's session.
    Called on new search or app exit.
    """
    if not session_id: return
    
    try:
        # Pattern matches: [session_id]_[uuid].m4a
        pattern = os.path.join(DOWNLOAD_FOLDER, f"{session_id}_*")
        files = glob.glob(pattern)
        
        for f in files:
            try:
                os.remove(f)
                logger.info(f"Session Cleanup: Deleted {os.path.basename(f)}")
            except OSError:
                logger.warning(f"Could not delete {f}, file might be in use.")
    except Exception as e:
        logger.error(f"Error in delete_session_files: {e}")

# --- Helper: Download Logic ---
def download_video_thread(url, filename):
    """
    Downloads audio with settings optimized for unstable/VPN networks.
    """
    active_downloads[filename] = 'downloading'
    
    # Save as [filename].(ext)
    output_template = os.path.join(DOWNLOAD_FOLDER, f"{filename}.%(ext)s")
    
    ydl_opts = {
        # Format: Prefer M4A for compatibility and immediate streaming
        'format': 'bestaudio[ext=m4a]/best',
        'outtmpl': output_template,
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        
        # --- VPN/Network Resilience Settings ---
        'source_address': '0.0.0.0', # Force IPv4
        'socket_timeout': 30,        # Allow 30s for slow handshakes
        'retries': 10,               # Retry 10 times on drop
        'fragment_retries': 10,      # Retry individual chunks
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        active_downloads[filename] = 'completed'
        logger.info(f"Download Complete: {filename}")
    except Exception as e:
        logger.error(f"Download Failed: {e}")
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
    data = request.get_json()
    query = data.get('query')
    # Get the User's Session ID from headers (sent by JS)
    session_id = request.headers.get('X-Session-ID')

    if not query:
        return jsonify({'error': 'Please enter a song name'}), 400

    # TRIGGER 1: New Search -> Clean up OLD files for this user
    # This prevents storage clutter on the server
    if session_id:
        logger.info(f"Search Trigger: Cleaning previous files for session {session_id}")
        delete_session_files(session_id)

    # Search Options
    ydl_opts = {
        'noplaylist': True, 
        'quiet': True, 
        'default_search': 'ytsearch1',
        'no_warnings': True, 
        'source_address': '0.0.0.0',
        'socket_timeout': 15 
    }
    
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
            video = info['entries'][0] if 'entries' in info else info
            
            return jsonify({
                'title': video.get('title', 'Unknown'),
                'url': video.get('webpage_url'),
                'thumbnail': video.get('thumbnail', ''),
            })
    except Exception as e:
        logger.error(f"Search failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/stream')
def stream():
    """
    Streams file to client.
    Filename includes Session ID to ensure isolation.
    """
    video_url = request.args.get('url')
    session_id = request.args.get('session_id')
    
    if not video_url or not session_id:
        return "Missing Data", 400

    # Create a unique filename tied to the Session ID
    # Format: [session_id]_[random_uuid]
    file_id = f"{session_id}_{uuid.uuid4()}"
    
    # Start background download
    thread = threading.Thread(target=download_video_thread, args=(video_url, file_id))
    thread.start()

    def generate():
        filepath = None
        timeout = 0
        
        # 1. Wait for Buffering (Wait for > 2KB of data)
        while True:
            # Look for any file starting with this file_id (ignoring extension)
            matches = glob.glob(os.path.join(DOWNLOAD_FOLDER, f"{file_id}*"))
            
            if matches:
                temp_path = matches[0]
                # CRITICAL BUFFER CHECK:
                # If we stream 0 bytes, browser assumes error. 
                # We wait for 2KB (headers) to exist.
                if os.path.exists(temp_path) and os.path.getsize(temp_path) > 2048:
                    filepath = temp_path
                    break
            
            # Check for download failure or timeout
            if active_downloads.get(file_id) == 'error': return
            if timeout > 60: return # 30s timeout (0.5 * 60) for slow VPNs
            
            time.sleep(0.5)
            timeout += 1

        # 2. Stream Content
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(1024 * 64) # 64KB chunks
                if chunk:
                    yield chunk
                else:
                    # Stop if download is done or failed
                    status = active_downloads.get(file_id)
                    if status in ['completed', 'error']:
                        break
                    time.sleep(0.2) # Wait for more data to arrive

    return Response(stream_with_context(generate()), mimetype="audio/mp4", headers={
        "Content-Disposition": f"attachment; filename=song.m4a"
    })

# TRIGGER 2: Explicit Cleanup Endpoint
# The frontend calls this via navigator.sendBeacon when the tab closes
@app.route('/cleanup_session', methods=['POST'])
def cleanup_session():
    try:
        # Beacon API can send text/plain or JSON
        if request.is_json:
            data = request.get_json()
            session_id = data.get('session_id')
        else:
            # Handle raw text payload
            session_id = request.data.decode('utf-8')

        if session_id:
            logger.info(f"App Close Trigger: Cleaning session {session_id}")
            delete_session_files(session_id)
            return "OK", 200
        return "No Session ID", 400
    except Exception as e:
        logger.error(f"Error in cleanup endpoint: {e}")
        return "Error", 500

if __name__ == '__main__':
    # Threaded=True is mandatory for simultaneous download + playback
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
import os
import json # New import for loading JSON from environment variable
import eventlet # New import for SocketIO async mode. Ensure 'eventlet' is in requirements.txt
eventlet.monkey_patch() # Patch standard library for async I/O
import tempfile

from flask import Flask, request, Response, abort, render_template, send_from_directory, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
import yt_dlp
import logging
import uuid
from urllib import request as url_request
import re
from googleapiclient.discovery import build
import random
import firebase_admin
from firebase_admin import credentials, firestore, auth

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

socketio = SocketIO(app, cors_allowed_origins="*")

# --- CONFIGURATION ---
# IMPORTANT: Retrieve Google Drive API Key from environment variable for security.
# In Vercel, set an environment variable named GOOGLE_DRIVE_API_KEY with your actual key.
GOOGLE_DRIVE_API_KEY = os.environ.get('GOOGLE_DRIVE_API_KEY', 'YOUR_GOOGLE_DRIVE_API_KEY_HERE') # Fallback for local testing

# Define the directory for downloaded audio files
# WARNING: On Vercel, this directory is ephemeral. Files downloaded here will not persist
# between requests or deployments. For persistent storage, use cloud storage (e.g., GCS, S3).
# Initialize Google Drive API service
DRIVE_SERVICE = build('drive', 'v3', developerKey=GOOGLE_DRIVE_API_KEY)

# --- Firebase Admin SDK Initialization (for Firestore) ---
# IMPORTANT: For production, store the content of your firebase_admin_key.json
# file as a JSON string in a Vercel environment variable named 'FIREBASE_ADMIN_CREDENTIALS_JSON'.
# Never commit your .json key file to your repository.
db = None # Initialize db as None
try:
    firebase_credentials_json = os.environ.get('FIREBASE_ADMIN_CREDENTIALS_JSON')

    if firebase_credentials_json:
        # Load credentials from environment variable
        cred_dict = json.loads(firebase_credentials_json)
        cred = credentials.Certificate(cred_dict)
        if not firebase_admin._apps: # Initialize Firebase Admin SDK only once
            firebase_admin.initialize_app(cred)
        db = firestore.client()
        logging.info("Firebase Admin SDK initialized successfully from environment variable.")
    else:
        # Fallback for local development if environment variable is not set
        # This part should ideally be used ONLY for local development
        # and 'firebase_admin_key.json' should be in your .gitignore
        FIREBASE_ADMIN_KEY_FILE_LOCAL = 'firebase_admin_key.json' # Adjust path for local testing
        if os.path.exists(FIREBASE_ADMIN_KEY_FILE_LOCAL):
            if not firebase_admin._apps: # Initialize Firebase Admin SDK only once
                cred = credentials.Certificate(FIREBASE_ADMIN_KEY_FILE_LOCAL)
                firebase_admin.initialize_app(cred)
            db = firestore.client()
            logging.info("Firebase Admin SDK initialized successfully from local file (for development).")
        else:
            logging.error("Firebase Admin SDK credentials not found. Set 'FIREBASE_ADMIN_CREDENTIALS_JSON' "
                          "environment variable on Vercel or provide 'firebase_admin_key.json' for local development.")
            db = None # Ensure db is None if initialization fails
except Exception as e:
    logging.error(f"Error initializing Firebase Admin SDK: {e}")
    db = None

# This dictionary will store active jam sessions primarily for SocketIO tracking.
# The authoritative data will reside in Firestore.
jam_sessions = {}
sids_in_jams = {} # { socket_id: { 'jam_id': '...', 'nickname': '...' } }

# --- Helper for getting base URL ---
def get_base_url():
    # In a Vercel environment, request.base_url or request.host_url
    # should correctly reflect the public URL.
    # For local development, it will be http://127.0.0.1:5000/
    return request.host_url

@app.route('/')
def index():
    # This route serves the main application page.
    # The client-side JavaScript will read the jam_id from the URL.
    # For direct root access, initial_jam_id will be None.
    return render_template('index.html', initial_jam_id='')

# NEW ROUTE: To handle joining a session via a URL (e.g., /join/some_jam_id)
@app.route('/join/<jam_id>')
def join_by_link(jam_id):
    logging.info(f"Received request to join jam via link: {jam_id}")
    # This route simply serves the main application page and passes the jam_id.
    # The client-side JavaScript will then read the jam_id from the URL.
    return render_template('index.html', initial_jam_id=jam_id)


@app.route('/local_audio/<path:filename>')
def serve_local_audio(filename):
    try:
        return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=False)
    except FileNotFoundError:
        abort(404, description=f"File not found: {filename}")

@app.route('/proxy_googledrive_audio/<file_id>')
def proxy_googledrive_audio(file_id):
    logging.info(f"Received request to proxy Google Drive audio for file ID: {file_id}")
    google_drive_url = f"https://docs.google.com/uc?export=download&id={file_id}&key={GOOGLE_DRIVE_API_KEY}"
    range_header = request.headers.get('Range')
    start_byte = 0
    end_byte = None
    total_length = None

    if range_header:
        match = re.search(r'bytes=(\d+)-(\d*)', range_header)
        if match:
            start_byte = int(match.group(1))
            if match.group(2):
                end_byte = int(match.group(2))
        logging.info(f"Parsed Range header: start_byte={start_byte}, end_byte={end_byte}")

    try:
        req = url_request.Request(google_drive_url)
        if range_header:
            req.add_header('Range', range_header)

        logging.info(f"Attempting to fetch Google Drive audio from: {google_drive_url} with Range: {range_header}")
        drive_response = url_request.urlopen(req)

        content_type = drive_response.info().get_content_type()
        content_length = drive_response.info().get('Content-Length')
        content_range = drive_response.info().get('Content-Range')

        if content_length:
            total_length = int(content_length)
            if content_range:
                total_match = re.search(r'bytes \d+-\d+/(\d+)', content_range)
                if total_match:
                    total_length = int(total_match.group(1))

        if total_length is None:
            try:
                head_req = url_request.Request(google_drive_url, method='HEAD')
                head_response = url_request.urlopen(head_req)
                total_length_head = head_response.info().get('Content-Length')
                if total_length_head:
                    total_length = int(total_length_head)
                    logging.info(f"Obtained total length from HEAD request: {total_length}")
            except Exception as e:
                logging.warning(f"Failed to get total length from HEAD request: {e}")

        def generate_audio_stream():
            try:
                chunk_size = 1024 * 64
                while True:
                    chunk = drive_response.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
                logging.info(f"Finished streaming Google Drive audio for {file_id}")
            except Exception as e:
                logging.error(f"Error streaming Google Drive audio: {e}")
                pass

        response = Response(generate_audio_stream(), mimetype=content_type or 'application/octet-stream')
        response.headers['Accept-Ranges'] = 'bytes'

        if range_header and total_length is not None:
            if end_byte is None:
                end_byte = total_length - 1
            
            actual_start = start_byte
            actual_end = end_byte if end_byte is not None else total_length - 1
            
            if content_range:
                range_match = re.search(r'bytes (\d+)-(\d+)/(\d+)', content_range)
                if range_match:
                    actual_start = int(range_match.group(1))
                    actual_end = int(range_match.group(2))
                    total_length = int(range_match.group(3))
            
            response.status_code = 206
            response.headers['Content-Range'] = f"bytes {actual_start}-{actual_end}/{total_length}"
            response.headers['Content-Length'] = actual_end - actual_start + 1
            logging.info(f"Serving partial content: Content-Range: {response.headers['Content-Range']}, Content-Length: {response.headers['Content-Length']}")
        elif total_length is not None:
            response.status_code = 200
            response.headers['Content-Length'] = total_length
            logging.info(f"Serving full content: Content-Length: {total_length}")
        else:
            response.status_code = 200
            logging.warning("Serving full content without Content-Length (unknown total size).")

        return response

    except url_request.URLError as e:
        logging.error(f"URLError when proxying Google Drive audio for {file_id}: {e}")
        return jsonify({"error": f"Failed to access Google Drive file: {e.reason}"}), 500
    except Exception as e:
        logging.error(f"Unexpected error when proxying Google Drive audio for {file_id}: {e}")
        return jsonify({"error": f"Internal server error: {e}"}), 500

@app.route('/search_googledrive_folder/<folder_id>')
def search_googledrive_folder(folder_id):
    query = request.args.get('query', '')
    logging.info(f"Searching Google Drive folder {folder_id} for audio files with query: '{query}'")

    if not GOOGLE_DRIVE_API_KEY or GOOGLE_DRIVE_API_KEY == 'YOUR_GOOGLE_DRIVE_API_KEY_HERE':
        return jsonify({"error": "Google Drive API Key is not configured on the server."}), 500

    try:
        q_param = f"'{folder_id}' in parents and mimeType contains 'audio/' and trashed = false"
        if query:
            q_param += f" and fullText contains '{query}'"

        fields = "files(id, name, mimeType, thumbnailLink, size)"

        results = DRIVE_SERVICE.files().list(
            q=q_param,
            fields=fields,
            pageSize=20
        ).execute()

        items = results.get('files', [])
        songs = []
        for item in items:
            album_art = item.get('thumbnailLink') or "https://placehold.co/128x128/0F9D58/FFFFFF?text=Drive"
            
            songs.append({
                'fileId': item['id'],
                'title': item['name'],
                'artist': 'Google Drive',
                'albumArtSrc': album_art,
                'type': 'googledrive'
            })
        
        logging.info(f"Found {len(songs)} songs in Google Drive folder {folder_id} for query '{query}'")
        return jsonify(songs)

    except Exception as e:
        logging.error(f"Error searching Google Drive folder {folder_id}: {e}")
        if "API key not valid" in str(e) or "API has not been used in project" in str(e) or "Google Drive API is not enabled" in str(e):
             return jsonify({"error": "Google Drive API Key is invalid or API is not enabled. Please check server logs."}), 500
        return jsonify({"error": f"Failed to search Google Drive folder: {e}"}), 500

@app.route('/get_random_googledrive_songs/<folder_id>')
def get_random_googledrive_songs(folder_id):
    logging.info(f"Getting random songs from Google Drive folder: {folder_id}")

    if not GOOGLE_DRIVE_API_KEY or GOOGLE_DRIVE_API_KEY == 'YOUR_GOOGLE_DRIVE_API_KEY_HERE':
        return jsonify({"error": "Google Drive API Key is not configured on the server."}), 500

    try:
        q_param = f"'{folder_id}' in parents and mimeType contains 'audio/' and trashed = false"
        fields = "files(id, name, mimeType, thumbnailLink, size)"
        
        all_items = []
        page_token = None
        while True:
            results = DRIVE_SERVICE.files().list(
                q=q_param,
                fields=fields,
                pageSize=1000,
                pageToken=page_token
            ).execute()
            all_items.extend(results.get('files', []))
            page_token = results.get('nextPageToken')
            if not page_token:
                break

        songs = []
        for item in all_items:
            album_art = item.get('thumbnailLink') or "https://placehold.co/128x128/0F9D58/FFFFFF?text=Drive"
            songs.append({
                'fileId': item['id'],
                'title': item['name'],
                'artist': 'Google Drive',
                'albumArtSrc': album_art,
                'type': 'googledrive'
            })
        
        random.shuffle(songs)
        num_songs_to_return = min(len(songs), 20)
        random_songs = songs[:num_songs_to_return]

        logging.info(f"Returned {len(random_songs)} random songs from Google Drive folder {folder_id}")
        return jsonify(random_songs)

    except Exception as e:
        logging.error(f"Error getting random songs from Google Drive folder {folder_id}: {e}")
        if "API key not valid" in str(e) or "API has not been used in project" in str(e) or "Google Drive API is not enabled" in str(e):
             return jsonify({"error": "Google Drive API Key is invalid or API is not enabled. Please check server logs."}), 500
        return jsonify({"error": f"Failed to get random songs from Google Drive folder: {e}"}), 500


@app.route('/download_youtube_audio/<video_id>', methods=['GET'])
def download_youtube_audio(video_id):
    logging.info(f"Received request to download YouTube audio for video ID: {video_id}")

    unique_filename = f"{video_id}-{uuid.uuid4().hex}.mp3"
    filepath = os.path.join(DOWNLOAD_DIR, unique_filename)

    # Check if file already exists in local temp storage
    if os.path.exists(filepath):
        logging.info(f"Serving existing downloaded audio for {video_id} at {filepath}")
        return jsonify({
            "audio_url": f"/local_audio/{unique_filename}",
            "title": "Existing Download",
            "artist": "Unknown",
            "album_art": "" # This might need to be fetched if not stored
        })

    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': filepath,
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'force_ipv4': True,
        'geo_bypass': True,
        'age_limit': 99,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
            logging.info(f"Audio downloaded for {video_id} to {filepath}")

            return jsonify({
                "audio_url": f"/local_audio/{unique_filename}",
                "title": info.get('title', 'Unknown Title'),
                "artist": info.get('uploader', 'Unknown Artist'),
                "album_art": info.get('thumbnail', '')
            })

    except yt_dlp.utils.DownloadError as e:
        logging.error(f"yt-dlp download error for video ID {video_id}: {e}")
        if "unavailable" in str(e) or "private" in str(e) or "embedding is disabled" in str(e):
            return jsonify({"error": "Video is restricted or unavailable for download."}), 403
        elif "Age-restricted" in str(e):
            return jsonify({"error": "Video is age-restricted and cannot be accessed."}), 403
        else:
            return jsonify({"error": f"Download error: {e}"}), 500
    except Exception as e:
        logging.error(f"Unexpected error for video ID {video_id}: {e}")
        return jsonify({"error": f"Internal server error: {e}"}), 500

@app.route('/youtube_info')
def youtube_info():
    url = request.args.get('url')
    if not url:
        return jsonify({"error": "URL parameter is missing."}), 400

    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'force_ipv4': True,
        'geo_bypass': True,
        'age_limit': 99,
        'logger': logging.getLogger(),
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            video_id = info.get('id')
            title = info.get('title')
            uploader = info.get('uploader')
            thumbnail = info.get('thumbnail')
            duration = info.get('duration')

            if not video_id:
                logging.error(f"Could not extract video ID for URL: {url}")
                return jsonify({"error": "Could not extract video ID from the provided URL."}), 400

            return jsonify({
                "video_id": video_id,
                "title": title,
                "uploader": uploader,
                "thumbnail": thumbnail,
                "duration": duration,
                "type": "youtube"
            })

    except yt_dlp.utils.DownloadError as e:
        logging.error(f"yt-dlp info extraction error for URL {url}: {e}")
        return jsonify({"error": f"Could not get YouTube video information: {e}"}), 500
    except Exception as e:
        logging.error(f"Unexpected error during YouTube info extraction for URL {url}: {e}")
        return jsonify({"error": f"Internal server error: {e}"}), 500


@app.route('/Youtube')
def Youtube():
    query = request.args.get('query')
    if not query:
        return jsonify({"error": "Query parameter is missing."}), 400

    ydl_opts = {
        'default_search': 'ytsearch10',
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'force_ipv4': True,
        'geo_bypass': True,
        'logger': logging.getLogger(),
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
            videos = []
            if 'entries' in info:
                for entry in info['entries']:
                    if entry and entry.get('id') and entry.get('title'):
                        videos.append({
                            'id': entry['id'],
                            'title': entry['title'],
                            'uploader': entry.get('uploader', 'Unknown'),
                            'thumbnail': entry.get('thumbnail', ''),
                            'duration': entry.get('duration'),
                        })
            return jsonify(videos)

    except yt_dlp.utils.DownloadError as e:
        logging.error(f"yt-dlp search error for query '{query}': {e}")
        return jsonify({"error": f"Youtube failed: {e}"}), 500
    except Exception as e:
        logging.error(f"Unexpected error during Youtube for query '{query}': {e}")
        return jsonify({"error": f"Internal server error: {e}"}), 500


# --- SocketIO Event Handlers ---
@socketio.on('connect')
def handle_connect():
    logging.info(f"Client connected: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    logging.info(f"Client disconnected: {request.sid}")
    if request.sid in sids_in_jams:
        jam_id = sids_in_jams[request.sid]['jam_id']
        nickname = sids_in_jams[request.sid]['nickname']

        if db: # Only proceed if Firestore is initialized
            jam_ref = db.collection('jam_sessions').document(jam_id)
            try:
                jam_doc = jam_ref.get()
                if jam_doc.exists:
                    jam_data = jam_doc.to_dict()
                    if jam_data['host_sid'] == request.sid:
                        # Host disconnected, mark session as ended in Firestore
                        logging.info(f"Host {nickname} ({request.sid}) for jam {jam_id} disconnected. Marking session as ended.")
                        jam_ref.update({'is_active': False, 'ended_at': firestore.SERVER_TIMESTAMP})
                        socketio.emit('session_ended', {'jam_id': jam_id, 'message': 'Host disconnected. Session ended.'}, room=jam_id)
                    else:
                        # Participant disconnected, remove from participants list
                        if request.sid in jam_data['participants']:
                            updated_participants = {sid: name for sid, name in jam_data['participants'].items() if sid != request.sid}
                            jam_ref.update({'participants': updated_participants})
                            logging.info(f"Participant {nickname} ({request.sid}) left jam {jam_id}.")
                            socketio.emit('update_participants', {
                                'jam_id': jam_id,
                                'participants': list(updated_participants.values())
                            }, room=jam_id)
                else:
                    logging.warning(f"Disconnected client {request.sid} was in jam {jam_id}, but jam not found in Firestore.")
            except Exception as e:
                logging.error(f"Error handling disconnect for jam {jam_id} in Firestore: {e}")
        
        # Clean up local socketio tracking
        # Note: Local jam_sessions cache will eventually become stale.
        # Firestore is the source of truth.
        if jam_id in jam_sessions and jam_sessions[jam_id]['host_sid'] == request.sid:
             del jam_sessions[jam_id] # Remove local tracking for host-ended session
        elif request.sid in jam_sessions.get(jam_id, {}).get('participants', {}):
             jam_sessions[jam_id]['participants'].pop(request.sid, None)

        if request.sid in sids_in_jams:
            del sids_in_jams[request.sid]
        
        leave_room(jam_id) # Ensure socket leaves the room

@socketio.on('create_session')
def create_session(data):
    if db is None:
        emit('join_failed', {'message': 'Server database not initialized. Cannot create session.'})
        return

    jam_name = data.get('jam_name', 'Unnamed Jam Session')
    nickname = data.get('nickname', 'Host')
    
    try:
        # Create a new document in 'jam_sessions' collection, Firestore generates ID
        new_jam_doc_ref = db.collection('jam_sessions').document() # Create document reference first
        jam_id = new_jam_doc_ref.id # Get the ID

        initial_jam_data = {
            'name': jam_name,
            'host_sid': request.sid,
            'participants': {request.sid: nickname}, # Store SID to nickname mapping
            'playlist': [],
            'playback_state': {
                'current_track_index': 0,
                'current_playback_time': 0,
                'is_playing': False,
                'timestamp': firestore.SERVER_TIMESTAMP # Use server timestamp
            },
            'created_at': firestore.SERVER_TIMESTAMP,
            'is_active': True # Mark session as active
        }
        new_jam_doc_ref.set(initial_jam_data) # Set the document with the initial data


        # Update local cache for quick lookup (for host, participants will rely on Firestore)
        jam_sessions[jam_id] = {
            'name': jam_name,
            'host_sid': request.sid,
            'participants': {request.sid: nickname},
            'playlist': [],
            'playback_state': { # Local cache doesn't need server timestamp here
                'current_track_index': 0,
                'current_playback_time': 0,
                'is_playing': False,
                'timestamp': 0
            }
        }
        sids_in_jams[request.sid] = {'jam_id': jam_id, 'nickname': nickname}

        join_room(jam_id)
        logging.info(f"Jam session '{jam_name}' created with ID: {jam_id} by host {nickname} ({request.sid})")

        # Construct the shareable link using the base URL
        shareable_link = f"{get_base_url()}join/{jam_id}"

        emit('session_created', {
            'jam_id': jam_id,
            'jam_name': jam_name,
            'is_host': True,
            'initial_state': initial_jam_data['playback_state'], # Send initial Firestore state
            'participants': list(initial_jam_data['participants'].values()),
            'shareable_link': shareable_link
        })

    except Exception as e:
        logging.error(f"Error creating jam session in Firestore: {e}")
        emit('join_failed', {'message': f'Error creating session: {e}'})


@socketio.on('join_session')
def join_session(data):
    if db is None:
        emit('join_failed', {'message': 'Server database not initialized. Cannot join session.'})
        return

    jam_id = data.get('jam_id')
    nickname = data.get('nickname', 'Guest')
    
    if not jam_id:
        logging.warning(f"Client {request.sid} attempted to join without jam_id.")
        emit('join_failed', {'message': 'Jam ID is missing.'})
        return

    try:
        jam_doc = db.collection('jam_sessions').document(jam_id).get()
        if not jam_doc.exists or not jam_doc.to_dict().get('is_active', False):
            logging.warning(f"Client {request.sid} attempted to join non-existent or inactive jam {jam_id}")
            emit('join_failed', {'message': 'Jam session not found or has ended.'})
            return

        jam_data = jam_doc.to_dict()
        
        # Check if already a participant (based on current socket ID)
        if request.sid in jam_data.get('participants', {}):
            logging.info(f"Client {request.sid} already in jam {jam_id}")
            # Still send initial state and success to ensure client is synced
            playback_state = jam_data.get('playback_state', {})
            emit('session_join_success', { # Changed event name for clarity
                'jam_id': jam_id,
                'current_track_index': playback_state.get('current_track_index', 0),
                'current_playback_time': playback_state.get('current_playback_time', 0),
                'is_playing': playback_state.get('is_playing', False),
                'playlist': jam_data.get('playlist', []),
                'jam_name': jam_data.get('name', 'Unnamed Jam'),
                'last_synced_at': playback_state.get('timestamp', firestore.SERVER_TIMESTAMP),
                'participants': jam_data.get('participants', {}), # Send dict to distinguish host/nickname
                'nickname_used': nickname # Send back the nickname that was used
            })
            join_room(jam_id) # Ensure the socket is in the room
            return


        # Add participant to Firestore
        updated_participants = jam_data.get('participants', {})
        updated_participants[request.sid] = nickname
        db.collection('jam_sessions').document(jam_id).update({'participants': updated_participants})

        # Update local tracking (optional, but good for quick SID-to-jam mapping)
        # Ensure the jam_id exists in jam_sessions before updating participants
        if jam_id not in jam_sessions:
            # If server restarted or jam wasn't in local cache, populate it.
            jam_sessions[jam_id] = {
                'name': jam_data.get('name', 'Unnamed Jam'),
                'host_sid': jam_data.get('host_sid'),
                'playlist': jam_data.get('playlist', []),
                'playback_state': jam_data.get('playback_state', {})
            }
        jam_sessions[jam_id]['participants'] = updated_participants # Update local cache
        sids_in_jams[request.sid] = {'jam_id': jam_id, 'nickname': nickname}

        join_room(jam_id)
        logging.info(f"Client {nickname} ({request.sid}) joined jam {jam_id}")

        # Send current state to the newly joined participant (from Firestore data)
        playback_state = jam_data.get('playback_state', {})
        emit('session_join_success', { # Changed event name for clarity
            'jam_id': jam_id,
            'current_track_index': playback_state.get('current_track_index', 0),
            'current_playback_time': playback_state.get('current_playback_time', 0),
            'is_playing': playback_state.get('is_playing', False),
            'playlist': jam_data.get('playlist', []),
            'jam_name': jam_data.get('name', 'Unnamed Jam'),
            'last_synced_at': playback_state.get('timestamp', firestore.SERVER_TIMESTAMP), # Use server timestamp
            'participants': updated_participants,
            'nickname_used': nickname
        })

        # Notify all other participants in the room about the new participant
        # Send the updated_participants dict so clients can re-render with nicknames
        emit('update_participants', {
            'jam_id': jam_id,
            'participants': updated_participants
        }, room=jam_id, include_self=False)

    except Exception as e:
        logging.error(f"Error joining jam session {jam_id} in Firestore: {e}")
        emit('join_failed', {'message': f'Error joining session: {e}'})

@socketio.on('sync_playback_state')
def sync_playback_state(data):
    if db is None:
        return # Database not initialized

    jam_id = data.get('jam_id')
    if not jam_id:
        return

    try:
        jam_doc = db.collection('jam_sessions').document(jam_id).get()
        if not jam_doc.exists:
            logging.warning(f"Sync request for non-existent jam {jam_id}")
            return
        
        jam_data = jam_doc.to_dict()
        # Ensure only the designated host can update the state
        if jam_data.get('host_sid') != request.sid:
            logging.warning(f"Non-host {request.sid} attempted to sync state for jam {jam_id}")
            return

        # Update Firestore with the new state
        new_playback_state = {
            'current_track_index': data.get('current_track_index'),
            'current_playback_time': data.get('current_playback_time'),
            'is_playing': data.get('is_playing'),
            'timestamp': firestore.SERVER_TIMESTAMP # Always update with server timestamp
        }
        
        db.collection('jam_sessions').document(jam_id).update({
            'playback_state': new_playback_state,
            'playlist': data.get('playlist', []) # Host sends full playlist
        })

        # No emit from here; client-side will listen to Firestore changes and update.
        # This prevents a loop of SocketIO events when using Firestore as the source of truth.

    except Exception as e:
        logging.error(f"Error syncing playback state for jam {jam_id} to Firestore: {e}")

@socketio.on('add_song_to_jam')
def add_song_to_jam(data):
    if db is None:
        return

    jam_id = data.get('jam_id')
    song = data.get('song')

    if not jam_id or not song:
        logging.warning(f"Invalid add_song_to_jam request from {request.sid} for jam {jam_id}")
        return

    try:
        jam_ref = db.collection('jam_sessions').document(jam_id)
        jam_doc = jam_ref.get()
        if not jam_doc.exists:
            logging.warning(f"Add song request for non-existent jam {jam_id}")
            return

        jam_data = jam_doc.to_dict()
        if jam_data.get('host_sid') != request.sid:
            logging.warning(f"Non-host {request.sid} attempted to add song to jam {jam_id}")
            return

        updated_playlist = jam_data.get('playlist', [])
        updated_playlist.append(song)
        
        jam_ref.update({'playlist': updated_playlist})
        logging.info(f"Song '{song.get('title', 'Unknown')}' added to jam {jam_id} by host {request.sid} via Firestore.")
        # No emit here, Firestore listener on client will handle.

    except Exception as e:
        logging.error(f"Error adding song to jam {jam_id} in Firestore: {e}")

@socketio.on('remove_song_from_jam')
def remove_song_from_jam(data):
    if db is None:
        return

    jam_id = data.get('jam_id')
    index = data.get('index')

    if not jam_id or index is None:
        logging.warning(f"Invalid remove_song_from_jam request from {request.sid} for jam {jam_id}")
        return

    try:
        jam_ref = db.collection('jam_sessions').document(jam_id)
        jam_doc = jam_ref.get()
        if not jam_doc.exists:
            logging.warning(f"Remove song request for non-existent jam {jam_id}")
            return

        jam_data = jam_doc.to_dict()
        if jam_data.get('host_sid') != request.sid:
            logging.warning(f"Non-host {request.sid} attempted to remove song from jam {jam_id}")
            return

        current_playlist = jam_data.get('playlist', [])
        if 0 <= index < len(current_playlist):
            removed_song = current_playlist.pop(index)
            logging.info(f"Song '{removed_song.get('title', 'Unknown')}' removed from jam {jam_id} by host {request.sid} via Firestore.")

            # Adjust current_track_index if the removed song affects it
            current_track_index = jam_data['playback_state'].get('current_track_index', 0)
            if current_track_index == index:
                if not current_playlist:
                    current_track_index = 0
                elif index >= len(current_playlist):
                    current_track_index = 0
            elif current_track_index > index:
                current_track_index -= 1
            
            # Update Firestore
            jam_ref.update({
                'playlist': current_playlist,
                'playback_state.current_track_index': current_track_index,
                'playback_state.current_playback_time': 0, # Reset time for new current track
                'playback_state.is_playing': jam_data['playback_state'].get('is_playing', False) and len(current_playlist) > 0 # Keep playing if playlist not empty
            })
            # No emit here, Firestore listener on client will handle.

    except Exception as e:
        logging.error(f"Error removing song from jam {jam_id} in Firestore: {e}")


# When running on Vercel, the application is served by a WSGI server (e.g., Gunicorn),
# which handles starting the Flask app. The 'socketio.run(app, ...)' call is only for
# local development with Flask's built-in server.
if __name__ == '__main__':
    if GOOGLE_DRIVE_API_KEY == 'YOUR_GOOGLE_DRIVE_API_KEY_HERE':
        logging.warning("Google Drive API Key is not configured in app.py. Google Drive features might not work.")
    if db is None:
        logging.error("Firestore database is not initialized. Jam Session feature will not work.")
    
    # This line is for local development only. Vercel will run `app` directly.
    socketio.run(app, debug=True, port=5000)

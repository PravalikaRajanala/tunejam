import os
import json
import eventlet
eventlet.monkey_patch()
import tempfile
import requests # Used for robust HTTP requests, especially for streaming

from flask import Flask, request, Response, abort, render_template, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
import yt_dlp
import logging
import uuid
import re
from googleapiclient.discovery import build
import random
import firebase_admin
from firebase_admin import credentials, firestore, auth

# Initialize Flask app, telling it to look for templates in the current directory (root)
app = Flask(__name__, template_folder='.') # <--- CHANGE HERE
CORS(app)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

socketio = SocketIO(app, cors_allowed_origins="*")

# --- CONFIGURATION ---
# IMPORTANT: Retrieve Google Drive API Key from environment variable for security.
# In Vercel, set an environment variable named GOOGLE_DRIVE_API_KEY with your actual key.
GOOGLE_DRIVE_API_KEY = os.environ.get('GOOGLE_DRIVE_API_KEY', 'YOUR_GOOGLE_DRIVE_API_KEY_HERE') # Fallback for local testing

# Define the directory for downloaded audio files (now primarily for temporary yt-dlp internal use if needed)
# On Vercel, this directory is ephemeral and writable. Files downloaded here will not persist
# between requests or deployments.
DOWNLOAD_DIR = tempfile.mkdtemp() # Correctly uses a writable temporary directory
logging.info(f"Using temporary directory for downloads: {DOWNLOAD_DIR}")


# Initialize Google Drive API service
# This initialization happens once when the serverless function cold starts
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
        if not firebase_admin._apps: # Initialize Firebase Admin SDK only once per process
            firebase_admin.initialize_app(cred)
        db = firestore.client()
        logging.info("Firebase Admin SDK initialized successfully from environment variable.")
    else:
        # Fallback for local development if environment variable is not set
        # This part should ideally be used ONLY for local development
        # and 'firebase_admin_key.json' should be in your .gitignore
        FIREBASE_ADMIN_KEY_FILE_LOCAL = 'firebase_admin_key.json' # Adjust path for local testing
        if os.path.exists(FIREBASE_ADMIN_KEY_FILE_LOCAL):
            if not firebase_admin._apps: # Initialize Firebase Admin SDK only once per process
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


# The /local_audio route is removed as both Google Drive and YouTube audio are now streamed directly


@app.route('/proxy_googledrive_audio/<file_id>')
def proxy_googledrive_audio(file_id):
    """
    Proxies Google Drive audio files directly to the client with byte-range support.
    Uses 'requests' library for more robust streaming and error handling.
    Adds 'key' parameter directly to the Google Drive URL.
    """
    logging.info(f"Received request to proxy Google Drive audio for file ID: {file_id}")

    # It's crucial that GOOGLE_DRIVE_API_KEY is correctly set in Vercel environment variables
    if not GOOGLE_DRIVE_API_KEY or GOOGLE_DRIVE_API_KEY == 'YOUR_GOOGLE_DRIVE_API_KEY_HERE':
        logging.error("Google Drive API Key is not configured on the server for proxy.")
        return jsonify({"error": "Google Drive API Key is not configured on the server. Please set it as an environment variable."}), 500

    google_drive_url = f"https://docs.google.com/uc?export=download&id={file_id}"
    
    headers_for_drive_request = {}
    range_header = request.headers.get('Range')

    if range_header:
        headers_for_drive_request['Range'] = range_header
        logging.info(f"Proxying with Range header: {range_header}")

    try:
        # Use requests.get with stream=True for efficient streaming
        # allow_redirects=True to follow Google Drive redirects
        # Add developer key directly to the URL parameters
        params = {'key': GOOGLE_DRIVE_API_KEY}

        # Set a timeout for the external request to prevent Vercel function timeouts
        # Adjust timeout as needed, but avoid very long values that exceed Vercel's limits
        response_from_drive = requests.get(
            google_drive_url, 
            headers=headers_for_drive_request, 
            stream=True, 
            params=params, 
            allow_redirects=True, 
            timeout= (30, 60) # (connect timeout, read timeout) in seconds
        )
        response_from_drive.raise_for_status() # Raise an HTTPError for bad responses (4xx or 5xx)

        content_type = response_from_drive.headers.get('Content-Type', 'application/octet-stream')
        content_length = response_from_drive.headers.get('Content-Length')
        content_range = response_from_drive.headers.get('Content-Range')

        # Create a Flask response that streams content from Google Drive
        flask_response = Response(response_from_drive.iter_content(chunk_size=8192), mimetype=content_type)
        flask_response.headers['Accept-Ranges'] = 'bytes'

        if content_length:
            flask_response.headers['Content-Length'] = content_length
        if content_range:
            flask_response.headers['Content-Range'] = content_range
            flask_response.status_code = 206 # Partial Content

        logging.info(f"Successfully proxied Google Drive audio for {file_id}. Status: {flask_response.status_code}")
        return flask_response

    except requests.exceptions.Timeout:
        logging.error(f"Timeout when fetching Google Drive audio for {file_id}. The Google Drive API took too long to respond.")
        return jsonify({"error": "Failed to stream audio: Request to Google Drive timed out. Try a smaller file or faster connection."}), 504
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code
        logging.error(f"HTTPError when proxying Google Drive audio for {file_id}: {e.response.text} (Status: {status_code})")
        # Check for specific API key related errors
        if status_code == 400 and "developerKey" in e.response.text and "invalid" in e.response.text:
            return jsonify({"error": "Google Drive API Key is invalid or missing. Please check your Vercel environment variables."}), 400
        return jsonify({"error": f"Failed to access Google Drive file (HTTP {status_code}): {e.response.text}"}), status_code
    except requests.exceptions.RequestException as e:
        logging.error(f"General request error when proxying Google Drive audio for {file_id}: {e}")
        return jsonify({"error": f"Failed to stream audio due to network or request issue: {e}"}), 500
    except Exception as e:
        logging.error(f"Unexpected error when proxying Google Drive audio for {file_id}: {e}")
        return jsonify({"error": f"Internal server error during proxy: {e}"}), 500


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


@app.route('/proxy_youtube_audio/<video_id>', methods=['GET'])
def proxy_youtube_audio(video_id):
    """
    Proxies YouTube audio directly to the client by first extracting the direct stream URL
    with yt-dlp and then streaming from that URL using requests.
    This avoids saving the audio to temporary disk storage entirely.
    """
    logging.info(f"Received request to proxy YouTube audio for video ID: {video_id}")

    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio', # Prefer m4a for better browser compatibility
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'force_ipv4': True,
        'geo_bypass': True,
        'age_limit': 99,
        'logger': logging.getLogger(),
        'simulate': True, # Only extract info, don't download
        'format_sort': ['res,ext:m4a', 'res,ext:webm'], # Sort by resolution, then prefer m4a/webm
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            
            # Find the best audio stream URL
            audio_url = None
            audio_ext = None
            content_length = None
            
            # Prioritize streams with known content length if available
            for f in info.get('formats', []):
                if f.get('ext') in ['m4a', 'webm', 'mp3', 'ogg', 'opus'] and f.get('url') and f.get('acodec') != 'none':
                    if f.get('filesize') is not None: # Prefer formats where filesize is known
                        audio_url = f['url']
                        audio_ext = f['ext']
                        content_length = f['filesize']
                        break # Found a good format with filesize, use it

            if not audio_url:
                # Fallback to any best audio if no filesize found initially or previous loop skipped
                for f in info.get('formats', []):
                    if f.get('ext') in ['m4a', 'webm', 'mp3', 'ogg', 'opus'] and f.get('url') and f.get('acodec') != 'none':
                        audio_url = f['url']
                        audio_ext = f['ext']
                        content_length = f.get('filesize') # May still be None
                        break # Use the first suitable one

            if not audio_url:
                logging.error(f"No suitable audio stream found for video ID: {video_id}")
                return jsonify({"error": "No suitable audio format found for this YouTube video."}), 404

            logging.info(f"Extracted YouTube audio URL: {audio_url} (Ext: {audio_ext}, Size: {content_length})")

            # Now stream directly from the extracted URL
            headers_for_youtube_request = {}
            range_header = request.headers.get('Range')
            if range_header:
                headers_for_youtube_request['Range'] = range_header
                logging.info(f"Proxying YouTube audio with Range header: {range_header}")

            # Set a timeout for the external request to YouTube
            youtube_stream_response = requests.get(
                audio_url,
                headers=headers_for_youtube_request,
                stream=True,
                allow_redirects=True,
                timeout=(30, 90) # Connect timeout, Read timeout for large files
            )
            youtube_stream_response.raise_for_status() # Raise an HTTPError for bad responses

            # Determine mimetype based on extracted extension or response header
            mimetype = youtube_stream_response.headers.get('Content-Type') or f'audio/{audio_ext}' if audio_ext else 'application/octet-stream'
            
            # Get actual content length from YouTube's response if available, or use yt-dlp's estimate
            actual_content_length = youtube_stream_response.headers.get('Content-Length') or content_length

            flask_response = Response(youtube_stream_response.iter_content(chunk_size=8192), mimetype=mimetype)
            flask_response.headers['Accept-Ranges'] = 'bytes'

            if actual_content_length:
                flask_response.headers['Content-Length'] = actual_content_length
            if range_header and youtube_stream_response.status_code == 206:
                flask_response.status_code = 206 # Partial Content
                flask_response.headers['Content-Range'] = youtube_stream_response.headers.get('Content-Range')

            logging.info(f"Successfully proxied YouTube audio for {video_id}. Status: {flask_response.status_code}")
            return flask_response

    except yt_dlp.utils.DownloadError as e:
        logging.error(f"yt-dlp info extraction/proxy setup error for video ID {video_id}: {e}")
        error_message = str(e).lower()
        if "unavailable" in error_message or "private" in error_message or "embedding is disabled" in error_message:
            return jsonify({"error": "YouTube video is restricted or unavailable."}), 403
        elif "age-restricted" in error_message:
            return jsonify({"error": "YouTube video is age-restricted and cannot be accessed."}), 403
        elif "no appropriate format" in error_message:
            return jsonify({"error": "No suitable audio format found for this YouTube video."}), 404
        elif "read timeout" in error_message or "connection timed out" in error_message:
            return jsonify({"error": "YouTube info extraction timed out. Video might be too large or network slow."}), 504
        else:
            return jsonify({"error": f"YouTube download/proxy error: {e}"}), 500
    except requests.exceptions.Timeout:
        logging.error(f"Timeout when streaming YouTube audio for {video_id}. The external request took too long to respond.")
        return jsonify({"error": "Failed to stream YouTube audio: External request timed out."}), 504
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code
        logging.error(f"HTTPError when streaming YouTube audio for {video_id}: {e.response.text} (Status: {status_code})")
        return jsonify({"error": f"Failed to stream YouTube audio (HTTP {status_code}): {e.response.text}"}), status_code
    except Exception as e:
        logging.error(f"Unexpected error when proxying YouTube audio for {video_id}: {e}")
        return jsonify({"error": f"Internal server error during YouTube proxy: {e}"}), 500


@app.route('/youtube_info')
def youtube_info():
    """
    Extracts basic YouTube video information without downloading.
    """
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
        'external_downloader_args': ['--socket-timeout', '15'] # Add timeout for info extraction
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
        error_message = str(e).lower()
        if "unavailable" in error_message or "private" in error_message or "embedding is disabled" in error_message:
            return jsonify({"error": "Video info restricted or unavailable."}), 403
        elif "age-restricted" in error_message:
            return jsonify({"error": "Video info is age-restricted."}), 403
        elif "read timeout" in error_message or "connection timed out" in error_message:
            return jsonify({"error": "Info extraction timed out. Check URL or network."}), 504
        return jsonify({"error": f"Could not get YouTube video information: {e}"}), 500
    except Exception as e:
        logging.error(f"Unexpected error during YouTube info extraction for URL {url}: {e}")
        return jsonify({"error": f"Internal server error: {e}"}), 500


@app.route('/Youtube')
def Youtube():
    """
    Searches YouTube for videos based on a query.
    """
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
        'external_downloader_args': ['--socket-timeout', '15'] # Add timeout for search
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
        error_message = str(e).lower()
        if "read timeout" in error_message or "connection timed out" in error_message:
            return jsonify({"error": "Search timed out. Try a more specific query."}), 504
        return jsonify({"error": f"Youtube search failed: {e}"}), 500
    except Exception as e:
        logging.error(f"Unexpected error during Youtube search for query '{query}': {e}")
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

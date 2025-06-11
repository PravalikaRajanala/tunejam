import os
import json
import eventlet
eventlet.monkey_patch() # Patch standard library for async operations (e.g., requests)
import tempfile
import requests # Used for robust HTTP requests, especially for streaming

from flask import Flask, request, Response, abort, render_template, jsonify, make_response, redirect, url_for, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
import yt_dlp # For fetching YouTube metadata and streaming URLs
import logging
import uuid
import re # For regex parsing URLs
import random # Kept, potentially useful for future general randomization
# Removed Firebase Admin SDK imports:
# import firebase_admin
# from firebase_admin import credentials, firestore, auth
# from functools import wraps # No longer needed without decorators
# import datetime # No longer strictly needed for session expiration, but useful for timestamps
from werkzeug.exceptions import HTTPException # Import for custom error handling

# Initialize Flask app, telling it to look for templates in the current directory (root)
app = Flask(__name__, template_folder='.')
CORS(app, supports_credentials=True) # Enable CORS and support credentials (for cookies - though less critical without auth)

# --- CONFIGURATION: Flask Secret Key ---
# Still good practice for Flask's internal operations, even without explicit sessions.
if os.environ.get('FLASK_SECRET_KEY'):
    app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY')
    logging.info("Flask SECRET_KEY loaded from environment variable.")
else:
    app.config['SECRET_KEY'] = secrets.token_hex(32)
    logging.warning("FLASK_SECRET_KEY environment variable is NOT set. "
                    "A random key has been generated for this session.")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Removed Flask-Caching as authentication is gone, and it was primarily for cached auth-related calls ---
# app.config["CACHE_TYPE"] = "simple"
# app.config["CACHE_DEFAULT_TIMEOUT"] = 3600
# cache = Cache(app)

# Explicitly pass the Flask app to SocketIO and set async_mode
socketio = SocketIO(app, cors_allowed_origins="*", manage_session=False, async_mode='eventlet')

logging.info("Flask app and SocketIO initialized.")

# --- Ephemeral Directory for Downloads ---
DOWNLOAD_DIR = tempfile.mkdtemp()
logging.info(f"Using temporary directory for downloads: {DOWNLOAD_DIR}")

# --- Initialize Firestore (without Firebase Admin SDK credentials) ---
# NOTE: This setup assumes you will configure Firestore access for anonymous users
# in your Firebase project's Firestore Security Rules for 'jam_sessions' and 'users' collections.
# Without `firebase_admin` credentials, direct Firestore operations from the backend
# will require that the Firebase project is configured for public access or
# that the environment where this code runs has other means of authentication to Firestore.
# For simplicity, assuming you are relying on client-side Firestore for jam management
# or have public read/write rules.
# Since Firebase Admin SDK is removed, direct backend Firestore operations are no longer possible without re-introducing it.
# However, the user implied full removal of auth, so Firestore interactions via backend are removed,
# and client-side Firestore will be the source of truth, if used.
# If you *do* want server-side Firestore operations, you *must* re-add firebase-admin and its initialization.

# For this "no auth" version, we will simplify backend's interaction with Firestore
# and assume client-side Firebase handles direct Firestore writes.
# Backend will primarily proxy YouTube and serve manifest.

# The `db` and `firebase_auth` variables are no longer used here.
# Removed firebase_admin initialization logic.

# --- Hosted MP3 Songs Manifest (for Netlify-hosted songs) ---
HOSTED_SONGS_MANIFEST_FILE = 'hosted_songs_manifest.json'

# --- Helper for getting base URL ---
def get_base_url():
    return request.host_url

# --- Removed Authentication Decorator ---
# No more login_required decorator.

# --- Flask Routes ---

# Default root route - goes directly to the main app page
@app.route('/')
def index():
    # No more authentication, just render the main page
    return render_template('index.html')

# Route to handle joining a session via a URL (e.g., /join/some_jam_id)
@app.route('/join/<jam_id>')
def join_by_link(jam_id):
    logging.info(f"Received request to join jam via link: {jam_id}")
    # Pass the jam_id to the frontend, which will handle joining via Socket.IO
    return render_template('index.html', initial_jam_id=jam_id)

@app.route('/hosted_songs_manifest.json')
# Removed @cache.cached decorator since Flask-Caching is removed
def hosted_songs_manifest_route():
    """
    Serves the hosted_songs_manifest.json file.
    """
    try:
        if os.path.exists(HOSTED_SONGS_MANIFEST_FILE):
            return send_from_directory(os.getcwd(), HOSTED_SONGS_MANIFEST_FILE, mimetype='application/json')
        else:
            logging.error(f"Hosted songs manifest file '{HOSTED_SONGS_MANIFEST_FILE}' not found.")
            return jsonify({"error": "Hosted songs manifest not found on server."}), 404
    except Exception as e:
        logging.error(f"Error serving hosted_songs_manifest.json: {e}")
        return jsonify({"error": f"Internal server error serving manifest: {e}"}), 500

@app.route('/proxy_youtube_audio/<video_id>', methods=['GET'])
def proxy_youtube_audio(video_id):
    """
    Proxies YouTube audio directly to the client.
    """
    logging.info(f"Received request to proxy YouTube audio for video ID: {video_id}")

    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'force_ipv4': True,
        'geo_bypass': True,
        'logger': logging.getLogger(),
        'simulate': True,
        'format_sort': ['res,ext:m4a', 'res,ext:webm'],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            
            audio_url = None
            audio_ext = None
            content_length = None
            
            for f in info.get('formats', []):
                if f.get('ext') in ['m4a', 'webm', 'mp3', 'ogg', 'opus'] and f.get('url') and f.get('acodec') != 'none':
                    if f.get('filesize') is not None:
                        audio_url = f['url']
                        audio_ext = f['ext']
                        content_length = f['filesize']
                        break
            if not audio_url:
                for f in info.get('formats', []):
                    if f.get('ext') in ['m4a', 'webm', 'mp3', 'ogg', 'opus'] and f.get('url') and f.get('acodec') != 'none':
                        audio_url = f['url']
                        audio_ext = f['ext']
                        content_length = f.get('filesize')
                        break

            if not audio_url:
                logging.error(f"No suitable audio stream found for video ID: {video_id}")
                return jsonify({"error": "No suitable audio format found for this YouTube video."}), 404

            logging.info(f"Extracted YouTube audio URL: {audio_url} (Ext: {audio_ext}, Size: {content_length})")

            headers_for_youtube_request = {}
            range_header = request.headers.get('Range')
            if range_header:
                headers_for_youtube_request['Range'] = range_header
                logging.info(f"Proxying YouTube audio with Range header: {range_header}")

            youtube_stream_response = requests.get(
                audio_url,
                headers=headers_for_youtube_request,
                stream=True,
                allow_redirects=True,
                timeout=(30, 90)
            )
            youtube_stream_response.raise_for_status()

            mimetype = youtube_stream_response.headers.get('Content-Type') or f'audio/{audio_ext}' if audio_ext else 'application/octet-stream'
            actual_content_length = youtube_stream_response.headers.get('Content-Length') or content_length

            flask_response = Response(youtube_stream_response.iter_content(chunk_size=8192), mimetype=mimetype)
            flask_response.headers['Accept-Ranges'] = 'bytes'

            if actual_content_length:
                flask_response.headers['Content-Length'] = actual_content_length
            if range_header and youtube_stream_response.status_code == 206:
                flask_response.status_code = 206
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

@app.route('/download_youtube_audio/<video_id>')
def download_youtube_audio(video_id):
    """
    Downloads YouTube audio to a temporary file and returns its local URL.
    NOTE: This route is generally not used now that proxy_youtube_audio streams directly.
    """
    logging.info(f"Received request to download YouTube audio for video ID: {video_id}")
    audio_filename = f"{video_id}.mp3"
    audio_path = os.path.join(DOWNLOAD_DIR, audio_filename)

    if os.path.exists(audio_path):
        logging.info(f"Audio for {video_id} already exists locally. Serving existing file.")
        return jsonify({"audio_url": f"/local_audio/{audio_filename}"})

    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'outtmpl': audio_path,
        'quiet': True,
        'no_warnings': True,
        'force_ipv4': True,
        'geo_bypass': True,
        'age_limit': 99,
        'logger': logging.getLogger()
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
            logging.info(f"Successfully downloaded YouTube audio for video ID: {video_id}")
            return jsonify({"audio_url": f"/local_audio/{audio_filename}"})
    except yt_dlp.utils.DownloadError as e:
        logging.error(f"yt-dlp download error for video ID {video_id}: {e}")
        error_message = str(e).lower()
        if "unavailable" in error_message or "private" in error_message or "embedding is disabled" in error_message:
            return jsonify({"error": "YouTube video is restricted or unavailable."}), 403
        elif "age-restricted" in error_message:
            return jsonify({"error": "YouTube video is age-restricted and cannot be downloaded."}), 403
        return jsonify({"error": f"YouTube download failed: {e}"}), 500
    except Exception as e:
        logging.error(f"Unexpected error during YouTube download for video ID {video_id}: {e}")
        return jsonify({"error": f"Internal server error: {e}"}), 500

# Route to serve locally downloaded audio files (for YouTube downloads)
@app.route('/local_audio/<filename>')
def local_audio(filename):
    file_path = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(file_path):
        abort(404)
    return Response(open(file_path, 'rb').read(), mimetype='audio/mpeg')

@app.route('/youtube_info')
# Removed @cache.cached decorator
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
        'external_downloader_args': ['--socket-timeout', '15']
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
# Removed @cache.cached decorator
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
        'external_downloader_args': ['--socket-timeout', '15']
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


@app.route('/search_hosted_mp3s')
def search_hosted_mp3s():
    """
    Searches the loaded HOSTED_SONGS_DATA manifest for MP3s matching a query.
    NOTE: This route should use the HOSTED_SONGS_DATA loaded by `hosted_songs_manifest_route`.
    """
    manifest_response = hosted_songs_manifest_route()
    if manifest_response.status_code == 200:
        HOSTED_SONGS_DATA = json.loads(manifest_response.data)
    else:
        logging.error(f"Failed to load hosted songs manifest from /hosted_songs_manifest.json: {manifest_response.status_code}")
        return jsonify({"error": "Could not retrieve hosted songs data."}), 500

    query = request.args.get('query', '').lower()
    
    if not HOSTED_SONGS_DATA:
        return jsonify({"error": "Hosted MP3 songs manifest not loaded or is empty on the server. Please ensure 'hosted_songs_manifest.json' is present."}), 500

    filtered_songs = []
    for song in HOSTED_SONGS_DATA:
        if query in song.get('title', '').lower() or query in song.get('artist', '').lower():
            filtered_songs.append(song)
    
    logging.info(f"Found {len(filtered_songs)} hosted MP3s for query '{query}'")
    return jsonify(filtered_songs)

# --- SocketIO Event Handlers ---
# Dictionaries to keep track of active jam sessions and SIDs.
# For a "no-auth" setup, these local dictionaries become more critical
# unless a central shared state (like a public Firestore without auth) is used.
# Since the user wants to remove auth, Firestore integration is also simplified here.
# If you still want persistent jam sessions, you'd need to re-add Firebase Admin SDK for Firestore.
# For now, jam sessions will be purely in-memory on the Vercel instance that created them.
# This means if the Vercel instance restarts or scales, the jam data is lost.
# To keep sessions persistent without auth, you'd need to use Firestore with anonymous/public rules.
# Given the user's explicit request to remove login/auth, I'm assuming ephemeral sessions for now.
jam_sessions = {} # {jam_id: {host_sid: '...', participants: {sid: 'nickname'}, playlist: [], playback_state: {}}}
sids_in_jams = {} # {sid: {jam_id: '...', nickname: '...'}}


@socketio.on('connect')
def handle_connect():
    logging.info(f"Socket.IO Client connected: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    logging.info(f"Socket.IO Client disconnected: {request.sid}")
    if request.sid in sids_in_jams:
        jam_id = sids_in_jams[request.sid]['jam_id']
        nickname = sids_in_jams[request.sid]['nickname']

        if jam_id in jam_sessions:
            jam_data = jam_sessions[jam_id]
            
            if jam_data.get('host_sid') == request.sid:
                # Host disconnected, delete the session
                logging.info(f"Host {nickname} ({request.sid}) for jam {jam_id} disconnected. Deleting session.")
                socketio.emit('session_ended', {'jam_id': jam_id, 'message': 'Host disconnected. Session ended.'}, room=jam_id)
                del jam_sessions[jam_id]
            else:
                # Participant disconnected, remove from participants list
                if request.sid in jam_data.get('participants', {}):
                    updated_participants = {sid: name for sid, name in jam_data['participants'].items() if sid != request.sid}
                    jam_data['participants'] = updated_participants # Update local state
                    logging.info(f"Participant {nickname} ({request.sid}) left jam {jam_id}.")
                    socketio.emit('update_participants', {
                        'jam_id': jam_id,
                        'participants': updated_participants
                    }, room=jam_id)
                    
                    # If this was the last participant and host is already gone, delete session
                    if not updated_participants and jam_id not in jam_sessions: # Check if host already deleted
                        logging.info(f"Last participant left jam {jam_id} which had no host. Deleting session.")
                        # Session might already be gone if host left.
                        # No need to emit session_ended again if host already did.
                        pass # The host's disconnect already handled the deletion.
                
            # Clean up local socketio tracking for the disconnecting SID
            del sids_in_jams[request.sid]
            leave_room(jam_id) # Ensure socket leaves the room
        else:
            logging.warning(f"Disconnected client {request.sid} was in jam {jam_id}, but jam not found in local sessions.")
            del sids_in_jams[request.sid] # Still clean up sids_in_jams
            leave_room(jam_id)

@socketio.on('create_session')
def create_session(data):
    jam_name = data.get('jam_name', 'Unnamed Jam Session')
    nickname = data.get('nickname', 'Host')
    
    # Generate a unique jam ID
    jam_id = str(uuid.uuid4())

    initial_jam_data = {
        'name': jam_name,
        'host_sid': request.sid,
        'participants': {request.sid: nickname}, # Store SID to nickname mapping
        'playlist': [],
        'playback_state': {
            'current_track_index': 0,
            'current_playback_time': 0,
            'is_playing': False,
            'timestamp': 0 # Local timestamp for internal logic
        },
        'created_at': 0, # Placeholder
        'is_active': True # Mark session as active
    }
    
    jam_sessions[jam_id] = initial_jam_data
    sids_in_jams[request.sid] = {'jam_id': jam_id, 'nickname': nickname}

    join_room(jam_id)
    logging.info(f"Jam session '{jam_name}' created with ID: {jam_id} by host {nickname} ({request.sid})")

    shareable_link = f"{get_base_url()}join/{jam_id}"

    emit('session_created', {
        'jam_id': jam_id,
        'jam_name': jam_name,
        'is_host': True,
        'initial_state': initial_jam_data['playback_state'],
        'participants': initial_jam_data['participants'],
        'shareable_link': shareable_link,
        'nickname_used': nickname
    })

@socketio.on('join_session')
def join_session_handler(data):
    jam_id = data.get('jam_id')
    nickname = data.get('nickname', 'Guest')
    
    if not jam_id:
        logging.warning(f"Client {request.sid} attempted to join without jam_id.")
        emit('join_failed', {'message': 'Jam ID is missing.'})
        return

    if jam_id not in jam_sessions or not jam_sessions[jam_id].get('is_active', False):
        logging.warning(f"Client {request.sid} attempted to join non-existent or inactive jam {jam_id}")
        emit('join_failed', {'message': 'Jam session not found or has ended.'})
        return

    jam_data = jam_sessions[jam_id]
    
    # Add participant to local dictionary
    updated_participants = jam_data.get('participants', {})
    updated_participants[request.sid] = nickname
    jam_data['participants'] = updated_participants

    sids_in_jams[request.sid] = {'jam_id': jam_id, 'nickname': nickname}

    join_room(jam_id)
    logging.info(f"Client {nickname} ({request.sid}) joined jam {jam_id}")

    playback_state = jam_data.get('playback_state', {})
    emit('session_join_success', {
        'jam_id': jam_id,
        'current_track_index': playback_state.get('current_track_index', 0),
        'current_playback_time': playback_state.get('current_playback_time', 0),
        'is_playing': playback_state.get('is_playing', False),
        'playlist': jam_data.get('playlist', []),
        'jam_name': jam_data.get('name', 'Unnamed Jam'),
        'last_synced_at': playback_state.get('timestamp', 0),
        'host_sid': jam_data.get('host_sid'), # Send host_sid for client-side role check
        'participants': updated_participants,
        'nickname_used': nickname
    })

    # Notify all other participants in the room about the new participant
    emit('update_participants', {
        'jam_id': jam_id,
        'participants': updated_participants
    }, room=jam_id, include_self=False)

@socketio.on('sync_playback_state')
def sync_playback_state(data):
    jam_id = data.get('jam_id')
    if not jam_id or jam_id not in jam_sessions:
        logging.warning("Received sync_playback_state for non-existent jam or without jam_id.")
        return

    jam_data = jam_sessions[jam_id]
    if jam_data.get('host_sid') != request.sid: # Only host can sync state
        logging.warning(f"Non-host {request.sid} attempted to sync state for jam {jam_id}")
        return

    new_playback_state = {
        'current_track_index': data.get('current_track_index'),
        'current_playback_time': data.get('current_playback_time'),
        'is_playing': data.get('is_playing'),
        'timestamp': datetime.datetime.now().timestamp() # Use server timestamp for sync
    }
    
    jam_data['playback_state'] = new_playback_state
    jam_data['playlist'] = data.get('playlist', []) # Host sends full playlist

    logging.info(f"Host {request.sid} synced playback state for jam {jam_id}.")

    # Broadcast updated state to all other participants in the room
    emit('playback_state_updated', {
        'jam_id': jam_id,
        'playback_state': new_playback_state,
        'playlist': jam_data['playlist']
    }, room=jam_id, include_self=False)


@socketio.on('add_song_to_jam')
def add_song_to_jam(data):
    jam_id = data.get('jam_id')
    song = data.get('song')

    if not jam_id or jam_id not in jam_sessions or not song or not song.get('type'):
        logging.warning(f"Invalid add_song_to_jam request from {request.sid} for jam {jam_id}")
        return
    
    jam_data = jam_sessions[jam_id]
    if jam_data.get('host_sid') != request.sid:
        logging.warning(f"Non-host {request.sid} attempted to add song to jam {jam_id}")
        return

    # Assign a unique ID to the song if it doesn't have one
    if 'id' not in song or song['id'] is None:
        song['id'] = str(uuid.uuid4())

    updated_playlist = jam_data.get('playlist', [])
    updated_playlist.append(song)
    jam_data['playlist'] = updated_playlist # Update local state

    logging.info(f"Song '{song.get('title', 'Unknown')}' (Type: {song.get('type')}) added to jam {jam_id} by host {request.sid}.")

    # Broadcast updated playlist to all participants
    emit('playlist_updated', {'jam_id': jam_id, 'playlist': updated_playlist}, room=jam_id)

@socketio.on('remove_song_from_jam')
def remove_song_from_jam(data):
    jam_id = data.get('jam_id')
    song_id_to_remove = data.get('song_id')

    if not jam_id or jam_id not in jam_sessions or not song_id_to_remove:
        logging.warning(f"Invalid remove_song_from_jam request from {request.sid} for jam {jam_id}")
        return

    jam_data = jam_sessions[jam_id]
    if jam_data.get('host_sid') != request.sid:
        logging.warning(f"Non-host {request.sid} attempted to remove song from jam {jam_id}")
        return

    current_playlist = jam_data.get('playlist', [])
    index_to_remove = -1
    for i, song in enumerate(current_playlist):
        if song.get('id') == song_id_to_remove:
            index_to_remove = i
            break

    if index_to_remove != -1:
        removed_song = current_playlist.pop(index_to_remove)
        logging.info(f"Song '{removed_song.get('title', 'Unknown')}' removed from jam {jam_id} by host {request.sid}.")
        
        # Adjust current_track_index if the removed song affects it
        current_track_index = jam_data['playback_state'].get('current_track_index', 0)
        if current_track_index == index_to_remove:
            if not current_playlist:
                current_track_index = 0
                jam_data['playback_state']['is_playing'] = False # Stop playing if playlist empty
            elif index_to_remove >= len(current_playlist):
                current_track_index = 0 # If last song was removed, go to beginning
        elif current_track_index > index_to_remove:
            current_track_index -= 1
        
        jam_data['playlist'] = current_playlist
        jam_data['playback_state']['current_track_index'] = current_track_index
        jam_data['playback_state']['current_playback_time'] = 0 # Reset time for new current track
        jam_data['playback_state']['timestamp'] = datetime.datetime.now().timestamp() # Update timestamp

        # Broadcast updated playlist and adjusted state
        emit('playlist_updated', {'jam_id': jam_id, 'playlist': current_playlist}, room=jam_id)
        emit('playback_state_updated', {'jam_id': jam_id, 'playback_state': jam_data['playback_state'], 'playlist': current_playlist}, room=jam_id) # Send full state for re-sync

# When running on Vercel, the application is served by a WSGI server (e.g., Gunicorn),
# which handles starting the Flask app. The 'socketio.run(app, ...)' call is only for
# local development with Flask's built-in server.
if __name__ == '__main__':
    # No more Firebase checks here
    # This line is for local development only. Vercel will run `app` directly.
    socketio.run(app, debug=True, port=5000)

# Global error handler to ensure all errors return JSON responses
@app.errorhandler(HTTPException)
def handle_http_exception(e):
    logging.error(f"Global HTTP error handler caught: {e}")
    code = e.code if isinstance(e, HTTPException) else 500
    message = e.description if isinstance(e, HTTPException) and e.description else "An unexpected server error occurred."
    if code == 500:
        message = "An internal server error occurred. Please try again later."
    response = jsonify(error={"code": code, "message": message})
    response.status_code = code
    return response

@app.errorhandler(Exception)
def handle_generic_exception(e):
    logging.error(f"Global generic exception handler caught: {e}", exc_info=True)
    response = jsonify(error={"code": 500, "message": "An unexpected internal server error occurred."})
    response.status_code = 500
    return response

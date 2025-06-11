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
import datetime # Now strictly needed for timestamps for sync
import secrets # Import secrets for generating a secure key for Flask secret key
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

# Explicitly pass the Flask app to SocketIO and set async_mode
socketio = SocketIO(app, cors_allowed_origins="*", manage_session=False, async_mode='eventlet')

logging.info("Flask app and SocketIO initialized.")

# --- Ephemeral Directory for Downloads ---
# This is still here for potential future use or if local caching is desired,
# but proxy_youtube_audio directly streams now.
DOWNLOAD_DIR = tempfile.mkdtemp()
logging.info(f"Using temporary directory for downloads: {DOWNLOAD_DIR}")

# --- Hosted MP3 Songs Manifest (for Netlify-hosted songs) ---
HOSTED_SONGS_MANIFEST_FILE = 'hosted_songs_manifest.json'
HOSTED_SONGS_DATA = [] # Global variable to store loaded manifest data

# Load hosted songs manifest once on startup
try:
    with open(HOSTED_SONGS_MANIFEST_FILE, 'r') as f:
        HOSTED_SONGS_DATA = json.load(f)
    logging.info(f"Successfully loaded {len(HOSTED_SONGS_DATA)} songs from {HOSTED_SONGS_MANIFEST_FILE}.")
except FileNotFoundError:
    logging.error(f"Error: {HOSTED_SONGS_MANIFEST_FILE} not found. Hosted MP3 search will not work.")
except json.JSONDecodeError as e:
    logging.error(f"Error decoding JSON from {HOSTED_SONGS_MANIFEST_FILE}: {e}. Hosted MP3 search may not work.")
except Exception as e:
    logging.error(f"Unexpected error loading {HOSTED_SONGS_MANIFEST_FILE}: {e}.")


# --- Helper for getting base URL ---
def get_base_url():
    """Dynamically determines the base URL of the application."""
    # request.url_root provides the full URL including scheme and host,
    # and ends with a slash.
    return request.url_root

# --- Flask Routes ---

# Default root route - goes directly to the main app page
@app.route('/')
def index():
    """Renders the main application page."""
    return render_template('index.html')

# Route to handle joining a session via a URL (e.g., /join/some_jam_id)
@app.route('/join/<jam_id>')
def join_by_link(jam_id):
    """
    Renders the main application page and provides a jam_id from the URL
    for automatic joining.
    """
    logging.info(f"Received request to join jam via link: {jam_id}")
    # Pass the jam_id to the frontend, which will handle joining via Socket.IO
    # Note: The template doesn't explicitly use initial_jam_id anymore in its script,
    # but the JS will parse window.location.search for jam_id.
    return render_template('index.html', initial_jam_id=jam_id)

@app.route('/hosted_songs_manifest.json')
def hosted_songs_manifest_route():
    """
    Serves the hosted_songs_manifest.json file directly.
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
    Proxies YouTube audio directly to the client, supporting range requests
    for seeking.
    """
    logging.info(f"Received request to proxy YouTube audio for video ID: {video_id}")

    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio', # Prefer m4a, webm
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'force_ipv4': True,
        'geo_bypass': True,
        'logger': logging.getLogger(),
        'simulate': True, # Only extract info, don't download
        'format_sort': ['res,ext:m4a', 'res,ext:webm'],
        'age_limit': 99, # Bypass age restrictions if possible
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            
            audio_url = None
            audio_ext = None
            content_length = None
            
            # Find the best audio format
            for f in info.get('formats', []):
                if f.get('url') and f.get('acodec') != 'none' and f.get('ext') in ['m4a', 'webm', 'mp3', 'ogg', 'opus']:
                    audio_url = f['url']
                    audio_ext = f['ext']
                    content_length = f.get('filesize')
                    break # Take the first suitable one found

            if not audio_url:
                logging.error(f"No suitable audio stream found for video ID: {video_id}")
                return jsonify({"error": "No suitable audio format found for this YouTube video."}), 404

            logging.info(f"Extracted YouTube audio URL: {audio_url} (Ext: {audio_ext}, Size: {content_length})")

            # Handle range requests for streaming
            headers_for_youtube_request = {}
            range_header = request.headers.get('Range')
            if range_header:
                headers_for_youtube_request['Range'] = range_header
                logging.info(f"Proxying YouTube audio with Range header: {range_header}")

            # Stream the audio from YouTube
            youtube_stream_response = requests.get(
                audio_url,
                headers=headers_for_youtube_request,
                stream=True, # Important for streaming large files
                allow_redirects=True,
                timeout=(30, 90) # Connect timeout, Read timeout
            )
            youtube_stream_response.raise_for_status() # Raise an exception for bad status codes

            mimetype = youtube_stream_response.headers.get('Content-Type') or f'audio/{audio_ext}' if audio_ext else 'application/octet-stream'
            actual_content_length = youtube_stream_response.headers.get('Content-Length') or content_length

            # Create a Flask response that streams content
            flask_response = Response(youtube_stream_response.iter_content(chunk_size=8192), mimetype=mimetype)
            flask_response.headers['Accept-Ranges'] = 'bytes'

            if actual_content_length:
                flask_response.headers['Content-Length'] = actual_content_length
            
            # If client requested a range and YouTube returned partial content
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

# Removed '/download_youtube_audio/<video_id>' as direct proxying is now preferred.

@app.route('/youtube_info')
def youtube_info():
    """
    Extracts basic YouTube video information without downloading.
    Used for adding a specific URL.
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
                "id": video_id, # Consistent ID for all tracks
                "title": title,
                "artist": uploader, # Use uploader as artist for consistency
                "albumArtSrc": thumbnail, # Use thumbnail as albumArtSrc
                "type": "youtube", # Indicate it's a YouTube video
                "videoId": video_id, # Store YouTube specific ID
                "duration": duration,
                "thumbnail": thumbnail # Also keep thumbnail
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

@app.route('/Youtube') # Renamed from /youtube_search to /Youtube for consistency with previous user code
def Youtube():
    """
    Searches YouTube for videos based on a query.
    """
    query = request.args.get('query')
    if not query:
        return jsonify({"error": "Query parameter is missing."}), 400

    ydl_opts = {
        'default_search': 'ytsearch10', # Search for top 10 results
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True, # Extract basic info without deeper parsing, faster
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
                            'id': entry['id'], # YouTube video ID as the unique ID
                            'type': "youtube", # Ensure type is "youtube" for direct playback
                            'title': entry['title'],
                            'artist': entry.get('uploader', 'Unknown'), # Use uploader as artist
                            'videoId': entry['id'], # Store YouTube specific ID
                            'thumbnail': entry.get('thumbnail', ''), # Thumbnail for display
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
    This uses the globally loaded HOSTED_SONGS_DATA.
    """
    query = request.args.get('query', '').lower()
    
    if not HOSTED_SONGS_DATA:
        logging.error("Hosted MP3 songs manifest not loaded or is empty on the server.")
        return jsonify({"error": "Hosted MP3 songs manifest not loaded or is empty on the server. Please ensure 'hosted_songs_manifest.json' is present and valid."}), 500

    filtered_songs = []
    for song in HOSTED_SONGS_DATA:
        # Check if query is in title or artist (case-insensitive)
        if query in song.get('title', '').lower() or query in song.get('artist', '').lower():
            filtered_songs.append(song)
    
    logging.info(f"Found {len(filtered_songs)} hosted MP3s for query '{query}'")
    return jsonify(filtered_songs)

# --- SocketIO Event Handlers ---
# Dictionaries to keep track of active jam sessions and SIDs.
# These are in-memory and will reset if the server restarts.
jam_sessions = {} # {jam_id: {host_sid: '...', participants: {sid: {'nickname': '...', 'permissions': {'play': True, 'add': True, 'remove': True}}}, playlist: [], playback_state: {}}}
sids_in_jams = {} # {sid: {jam_id: '...', nickname: '...'}}


@socketio.on('connect')
def handle_connect():
    """Handles new Socket.IO client connections."""
    logging.info(f"Socket.IO Client connected: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    """
    Handles Socket.IO client disconnections.
    Deletes the jam session if the host disconnects.
    Removes participant if a regular user disconnects.
    """
    logging.info(f"Socket.IO Client disconnected: {request.sid}")
    if request.sid in sids_in_jams:
        jam_id = sids_in_jams[request.sid]['jam_id']
        nickname = sids_in_jams[request.sid]['nickname']

        if jam_id in jam_sessions:
            jam_data = jam_sessions[jam_id]
            
            if jam_data.get('host_sid') == request.sid:
                # Host disconnected, delete the session
                logging.info(f"Host {nickname} ({request.sid}) for jam {jam_id} disconnected. Deleting session.")
                # Emit a session_ended event to all remaining clients in the room
                socketio.emit('session_ended', {'jam_id': jam_id, 'message': 'Host disconnected. Session ended.'}, room=jam_id)
                del jam_sessions[jam_id] # Remove the session from active jams
            else:
                # Participant disconnected, remove from participants list
                if request.sid in jam_data.get('participants', {}):
                    updated_participants = {sid: participant_data for sid, participant_data in jam_data['participants'].items() if sid != request.sid}
                    jam_data['participants'] = updated_participants # Update local state
                    logging.info(f"Participant {nickname} ({request.sid}) left jam {jam_id}.")
                    
                    # Notify all other participants in the room about the updated list
                    socketio.emit('update_participants', {
                        'jam_id': jam_id,
                        'participants': updated_participants
                    }, room=jam_id)
                
            # Clean up local socketio tracking for the disconnecting SID
            del sids_in_jams[request.sid]
            leave_room(jam_id) # Ensure socket leaves the room
        else:
            logging.warning(f"Disconnected client {request.sid} was in jam {jam_id}, but jam not found in local sessions. Cleaning up.")
            del sids_in_jams[request.sid] # Still clean up sids_in_jams
            leave_room(jam_id) # Ensure socket leaves the room

@socketio.on('create_session')
def create_session(data):
    """
    Handles a client's request to create a new jam session.
    The client becomes the host and gets all permissions.
    """
    jam_name = data.get('jam_name', 'Unnamed Jam Session')
    nickname = data.get('nickname', 'Host')
    
    # Generate a unique jam ID
    jam_id = str(uuid.uuid4())

    initial_jam_data = {
        'name': jam_name,
        'host_sid': request.sid, # The SID of the host
        # Host gets all permissions by default
        'participants': {request.sid: {'nickname': nickname, 'permissions': {'play': True, 'add': True, 'remove': True}}}, 
        'playlist': [],
        'playback_state': {
            'current_track_index': 0,
            'current_playback_time': 0,
            'is_playing': False,
            'timestamp': datetime.datetime.now().timestamp() # Use server timestamp for internal logic
        },
        'created_at': datetime.datetime.now().timestamp(),
        'is_active': True # Mark session as active
    }
    
    jam_sessions[jam_id] = initial_jam_data # Store the new jam session
    sids_in_jams[request.sid] = {'jam_id': jam_id, 'nickname': nickname} # Track which jam this SID is in

    join_room(jam_id) # Add the host's socket to the new room
    logging.info(f"Jam session '{jam_name}' created with ID: {jam_id} by host {nickname} ({request.sid})")

    shareable_link = f"{get_base_url()}join/{jam_id}" # Generate the shareable link

    # Emit success message back to the host client
    emit('session_created', {
        'jam_id': jam_id,
        'jam_name': jam_name,
        'is_host': True,
        'initial_state': initial_jam_data['playback_state'],
        'participants': initial_jam_data['participants'], # Send full participant data with permissions
        'shareable_link': shareable_link,
        'nickname_used': nickname
    })

@socketio.on('join_session')
def join_session_handler(data):
    """
    Handles a client's request to join an existing jam session.
    Assigns default guest permissions.
    """
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
    
    # Add participant to local dictionary with default guest permissions
    updated_participants = jam_data.get('participants', {})
    if request.sid in updated_participants:
        # If client is already in this jam, update nickname and keep existing permissions
        updated_participants[request.sid]['nickname'] = nickname
        logging.info(f"Client {request.sid} already in jam {jam_id}, updated nickname to {nickname}.")
    else:
        # New participant, assign default permissions
        updated_participants[request.sid] = {
            'nickname': nickname,
            'permissions': {'play': False, 'add': False, 'remove': False} # Guests get no permissions by default
        }
        logging.info(f"Client {nickname} ({request.sid}) joined jam {jam_id}")

    jam_data['participants'] = updated_participants # Update local state

    sids_in_jams[request.sid] = {'jam_id': jam_id, 'nickname': nickname} # Track which jam this SID is in

    join_room(jam_id) # Add the client's socket to the jam room

    playback_state = jam_data.get('playback_state', {})
    # Emit success message back to the joining client
    emit('session_join_success', {
        'jam_id': jam_id,
        'current_track_index': playback_state.get('current_track_index', 0),
        'current_playback_time': playback_state.get('current_playback_time', 0),
        'is_playing': playback_state.get('is_playing', False),
        'playlist': jam_data.get('playlist', []),
        'jam_name': jam_data.get('name', 'Unnamed Jam'),
        'last_synced_at': playback_state.get('timestamp', 0),
        'host_sid': jam_data.get('host_sid'), # Send host_sid for client-side role check
        'participants': updated_participants, # Send full participant data with permissions
        'nickname_used': nickname
    })

    # Notify all other participants in the room about the new participant
    emit('update_participants', {
        'jam_id': jam_id,
        'participants': updated_participants
    }, room=jam_id, include_self=False)

@socketio.on('sync_playback_state')
def sync_playback_state(data):
    """
    Allows the host to synchronize the playback state (track index, time, play/pause)
    and the entire playlist with all participants in the jam.
    """
    jam_id = data.get('jam_id')
    if not jam_id or jam_id not in jam_sessions:
        logging.warning(f"Received sync_playback_state for non-existent jam {jam_id} or without jam_id.")
        return

    jam_data = jam_sessions[jam_id]
    
    # Only the host can sync state
    if jam_data.get('host_sid') != request.sid: 
        logging.warning(f"Non-host {request.sid} attempted to sync state for jam {jam_id}")
        return # Do not emit permission_denied, just silently ignore as this is a background sync

    new_playback_state = {
        'current_track_index': data.get('current_track_index'),
        'current_playback_time': data.get('current_playback_time'),
        'is_playing': data.get('is_playing'),
        'timestamp': datetime.datetime.now().timestamp() # Use server timestamp for sync
    }
    
    jam_data['playback_state'] = new_playback_state
    jam_data['playlist'] = data.get('playlist', []) # Host sends full playlist (can be empty)

    logging.debug(f"Host {request.sid} synced playback state for jam {jam_id}.") # Use debug for frequent logs

    # Broadcast updated state and playlist to all other participants in the room
    emit('playback_state_updated', {
        'jam_id': jam_id,
        'playback_state': new_playback_state,
        'playlist': jam_data['playlist']
    }, room=jam_id, include_self=False)


@socketio.on('add_song_to_jam')
def add_song_to_jam(data):
    """
    Handles a client's request to add a song to the jam session's playlist.
    Requires 'add' permission.
    """
    jam_id = data.get('jam_id')
    song = data.get('song')

    if not jam_id or jam_id not in jam_sessions or not song or not song.get('type'):
        logging.warning(f"Invalid add_song_to_jam request from {request.sid} for jam {jam_id}")
        return
    
    jam_data = jam_sessions[jam_id]
    
    # Permission check: Only allow if 'add' permission is granted to the requesting client
    if not jam_data['participants'].get(request.sid, {}).get('permissions', {}).get('add', False):
        logging.warning(f"Client {request.sid} (nickname: {jam_data['participants'].get(request.sid, {}).get('nickname', 'Unknown')}) attempted to add song to jam {jam_id} without 'add' permission.")
        emit('permission_denied', {'action': 'add_song', 'message': 'You do not have permission to add songs to this jam.'}, room=request.sid)
        return

    # Assign a unique ID to the song if it doesn't have one (client should ideally do this)
    if 'id' not in song or song['id'] is None:
        song['id'] = str(uuid.uuid4())

    updated_playlist = jam_data.get('playlist', [])
    updated_playlist.append(song)
    jam_data['playlist'] = updated_playlist # Update local state

    logging.info(f"Song '{song.get('title', 'Unknown')}' (Type: {song.get('type')}) added to jam {jam_id} by {jam_data['participants'][request.sid]['nickname']} ({request.sid}).")

    # Broadcast updated playlist to all participants in the room
    emit('playlist_updated', {'jam_id': jam_id, 'playlist': updated_playlist}, room=jam_id)

@socketio.on('remove_song_from_jam')
def remove_song_from_jam(data):
    """
    Handles a client's request to remove a song from the jam session's playlist.
    Requires 'remove' permission.
    """
    jam_id = data.get('jam_id')
    song_id_to_remove = data.get('song_id')

    if not jam_id or jam_id not in jam_sessions or not song_id_to_remove:
        logging.warning(f"Invalid remove_song_from_jam request from {request.sid} for jam {jam_id}")
        return

    jam_data = jam_sessions[jam_id]

    # Permission check: Only allow if 'remove' permission is granted
    if not jam_data['participants'].get(request.sid, {}).get('permissions', {}).get('remove', False):
        logging.warning(f"Client {request.sid} (nickname: {jam_data['participants'].get(request.sid, {}).get('nickname', 'Unknown')}) attempted to remove song from jam {jam_id} without 'remove' permission.")
        emit('permission_denied', {'action': 'remove_song', 'message': 'You do not have permission to remove songs from this jam.'}, room=request.sid)
        return

    current_playlist = jam_data.get('playlist', [])
    index_to_remove = -1
    for i, song in enumerate(current_playlist):
        if song.get('id') == song_id_to_remove:
            index_to_remove = i
            break

    if index_to_remove != -1:
        removed_song = current_playlist.pop(index_to_remove)
        logging.info(f"Song '{removed_song.get('title', 'Unknown')}' removed from jam {jam_id} by {jam_data['participants'][request.sid]['nickname']} ({request.sid}).")
        
        # Adjust current_track_index if the removed song affects it
        current_track_index = jam_data['playback_state'].get('current_track_index', 0)
        if current_track_index == index_to_remove:
            if not current_playlist:
                current_track_index = 0
                jam_data['playback_state']['is_playing'] = False # Stop playing if playlist empty
            elif index_to_remove >= len(current_playlist):
                current_track_index = 0 # If last song was removed, go to beginning (or adjust logic)
        elif current_track_index > index_to_remove:
            current_track_index -= 1
        
        jam_data['playlist'] = current_playlist
        jam_data['playback_state']['current_track_index'] = current_track_index
        jam_data['playback_state']['current_playback_time'] = 0 # Reset time for new current track
        jam_data['playback_state']['timestamp'] = datetime.datetime.now().timestamp() # Update timestamp

        # Broadcast updated playlist and adjusted state to all participants
        emit('playlist_updated', {'jam_id': jam_id, 'playlist': current_playlist}, room=jam_id)
        # Also send playback state updated as index might have changed
        emit('playback_state_updated', {'jam_id': jam_id, 'playback_state': jam_data['playback_state'], 'playlist': current_playlist}, room=jam_id)

@socketio.on('update_participant_permissions')
def update_participant_permissions(data):
    """
    Allows the host to update specific permissions for a single participant.
    """
    jam_id = data.get('jam_id')
    target_sid = data.get('target_sid')
    new_permissions = data.get('permissions') # e.g., {'play': True} or {'add': False}

    if not jam_id or jam_id not in jam_sessions or not target_sid or not isinstance(new_permissions, dict):
        logging.warning(f"Invalid update_participant_permissions request from {request.sid}")
        return

    jam_data = jam_sessions[jam_id]

    # Only the host can update permissions
    if jam_data.get('host_sid') != request.sid:
        logging.warning(f"Non-host {request.sid} attempted to change permissions in jam {jam_id}")
        emit('permission_denied', {'action': 'change_permissions', 'message': 'Only the host can change participant permissions.'}, room=request.sid)
        return

    # Ensure target_sid exists in participants
    if target_sid not in jam_data['participants']:
        logging.warning(f"Host {request.sid} attempted to change permissions for non-existent SID {target_sid} in jam {jam_id}")
        emit('permission_denied', {'action': 'change_permissions', 'message': 'Target participant not found in this jam.'}, room=request.sid)
        return

    # Update permissions for the target participant
    participant_data = jam_data['participants'][target_sid]
    # Use .update() to merge new_permissions with existing ones
    participant_data['permissions'].update(new_permissions) 

    logging.info(f"Host {request.sid} updated permissions for {participant_data['nickname']} ({target_sid}) in jam {jam_id}: {new_permissions}")

    # Broadcast the updated participants list to all clients in the room
    emit('update_participants', {
        'jam_id': jam_id,
        'participants': jam_data['participants']
    }, room=jam_id)

@socketio.on('update_all_permissions')
def update_all_permissions(data):
    """
    Allows the host to grant all permissions (play, add, remove) to all
    participants in the jam session.
    """
    jam_id = data.get('jam_id')

    if not jam_id or jam_id not in jam_sessions:
        logging.warning(f"Invalid update_all_permissions request from {request.sid} for jam {jam_id}")
        return

    jam_data = jam_sessions[jam_id]

    # Only the host can grant all permissions
    if jam_data.get('host_sid') != request.sid:
        logging.warning(f"Non-host {request.sid} attempted to grant all permissions in jam {jam_id}")
        emit('permission_denied', {'action': 'grant_all_permissions', 'message': 'Only the host can grant all permissions.'}, room=request.sid)
        return

    # Iterate through all participants and set their permissions to True
    for sid, participant_data in jam_data['participants'].items():
        # Do not change host's permissions (they already have them implicitly)
        if sid != request.sid: 
            participant_data['permissions'] = {'play': True, 'add': True, 'remove': True}
            logging.info(f"Granted all permissions to {participant_data['nickname']} ({sid}) in jam {jam_id}.")

    logging.info(f"Host {request.sid} granted all permissions to all participants in jam {jam_id}.")

    # Broadcast the updated participants list to all clients in the room
    emit('update_participants', {
        'jam_id': jam_id,
        'participants': jam_data['participants']
    }, room=jam_id)


# When running on Vercel, the application is served by a WSGI server (e.g., Gunicorn),
# which handles starting the Flask app. The 'socketio.run(app, ...)' call is only for
# local development with Flask's built-in server.
if __name__ == '__main__':
    socketio.run(app, debug=True, port=5000)

# Global error handler to ensure all errors return JSON responses
@app.errorhandler(HTTPException)
def handle_http_exception(e):
    """Handles HTTP errors and returns a JSON response."""
    logging.error(f"Global HTTP error handler caught: {e}")
    code = e.code if isinstance(e, HTTPException) else 500
    message = e.description if isinstance(e, HTTPException) and e.description else "An unexpected server error occurred."
    if code == 500: # Generic message for server errors
        message = "An internal server error occurred. Please try again later."
    response = jsonify(error={"code": code, "message": message})
    response.status_code = code
    return response

@app.errorhandler(Exception)
def handle_generic_exception(e):
    """Handles all other unexpected exceptions and returns a generic JSON response."""
    logging.error(f"Global generic exception handler caught: {e}", exc_info=True)
    response = jsonify(error={"code": 500, "message": "An unexpected internal server error occurred."})
    response.status_code = 500
    return response


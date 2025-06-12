import os
import json
import eventlet
eventlet.monkey_patch() # Patch standard library for async operations (e.g., requests)
import tempfile
import requests # Used for robust HTTP requests, especially for streaming

from flask import Flask, request, Response, abort, render_template, jsonify, make_response, redirect, url_for, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
# import yt_dlp # COMMENTED OUT: Removed to improve efficiency and reduce dependencies
import logging
import uuid
import re # For regex parsing URLs
import random # Kept, potentially useful for future general randomization
import firebase_admin
from firebase_admin import credentials, firestore, auth
from functools import wraps
import datetime # For session cookie expiration
from flask_caching import Cache # Import Flask-Caching
import secrets # Import secrets for generating a secure key
from werkzeug.exceptions import HTTPException # Import for custom error handling

# Initialize Flask app, telling it to look for templates in the current directory (root)
app = Flask(__name__, template_folder='.')
CORS(app, supports_credentials=True) # Enable CORS and support credentials (for cookies)

# --- CONFIGURATION: Flask Secret Key ---
# IMPORTANT: For production, always set FLASK_SECRET_KEY as an environment variable (e.g., on Vercel).
# This key is crucial for Flask's session management, which Socket.IO might implicitly rely on
# for secure communication and internal operations. If not set, Flask sessions will not be secure.
if os.environ.get('FLASK_SECRET_KEY'):
    app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY')
    logging.info("Flask SECRET_KEY loaded from environment variable.")
else:
    # Generate a secure key for local development if not set, but warn in production
    app.config['SECRET_KEY'] = secrets.token_hex(32)
    logging.warning("FLASK_SECRET_KEY environment variable is NOT set. "
                    "A random key has been generated for this session. "
                    "For production deployments, please set FLASK_SECRET_KEY in your environment variables "
                    "to ensure session security.")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- CONFIGURATION: Flask-Caching (Simplified to 'simple' in-memory cache) ---
# As you're not using Redis, we will configure Flask-Caching to use the 'simple' in-memory cache.
# This cache stores data in the Flask application's process memory.
# It's good for single-instance deployments or development but won't share cache across multiple instances.
app.config["CACHE_TYPE"] = "simple"
app.config["CACHE_DEFAULT_TIMEOUT"] = 3600 # Cache items for 1 hour (3600 seconds) by default
logging.info("Flask-Caching configured with 'simple' in-memory cache.")

cache = Cache(app) # Initialize the cache after configuration

# Explicitly pass the Flask app to SocketIO and set async_mode
# Using 'eventlet' as async_mode, as it's monkey-patched. This helps ensure proper async behavior.
socketio = SocketIO(app, cors_allowed_origins="*", manage_session=False, async_mode='eventlet')

logging.info("Flask app and SocketIO initialized.")

# --- Ephemeral Directory for Downloads ---
# On serverless platforms like Vercel, this directory is ephemeral.
# Files downloaded here will NOT persist between requests or deployments.
# For persistent storage of downloaded audio, use a cloud storage service (e.g., Firebase Storage, AWS S3).
DOWNLOAD_DIR = tempfile.mkdtemp() # Correctly uses a writable temporary directory
logging.info(f"Using temporary directory for downloads: {DOWNLOAD_DIR}")

# --- Firebase Admin SDK Initialization (for Firestore and Auth) ---
db = None # Initialize db as None
firebase_auth = None # Initialize firebase_auth as None

try:
    firebase_credentials_json = os.environ.get('FIREBASE_ADMIN_CREDENTIALS_JSON')

    if firebase_credentials_json:
        try:
            # Load credentials from environment variable
            cred_dict = json.loads(firebase_credentials_json)
            cred = credentials.Certificate(cred_dict)
            if not firebase_admin._apps: # Initialize Firebase Admin SDK only once per process
                firebase_admin.initialize_app(cred)
            db = firestore.client()
            firebase_auth = auth
            logging.info("Firebase Admin SDK initialized successfully from environment variable.")
        except json.JSONDecodeError as e:
            logging.error(f"Error decoding FIREBASE_ADMIN_CREDENTIALS_JSON: {e}")
        except Exception as e:
            logging.error(f"Error initializing Firebase Admin SDK from environment variable: {e}")
    else:
        # Fallback for local development if environment variable is not set
        FIREBASE_ADMIN_KEY_FILE_LOCAL = 'firebase_admin_key.json' # Adjust path for local testing
        if os.path.exists(FIREBASE_ADMIN_KEY_FILE_LOCAL):
            try:
                if not firebase_admin._apps: # Initialize Firebase Admin SDK only once per process
                    cred = credentials.Certificate(FIREBASE_ADMIN_KEY_FILE_LOCAL)
                    firebase_admin.initialize_app(cred)
                db = firestore.client()
                firebase_auth = auth
                logging.info("Firebase Admin SDK initialized successfully from local file (for development).")
            except Exception as e:
                logging.error(f"Error initializing Firebase Admin SDK from local file: {e}")
        else:
            logging.error("Firebase Admin SDK credentials not found. Set 'FIREBASE_ADMIN_CREDENTIALS_JSON' "
                          "environment variable on Vercel or provide 'firebase_admin_key.json' for local development. "
                          "Jam Session and Authentication features will not work.")
            db = None # Ensure db is None if initialization fails
            firebase_auth = None
except Exception as e: # Catch any unexpected errors during the entire Firebase setup block
    logging.error(f"An unexpected error occurred during Firebase Admin SDK setup: {e}")
    db = None
    firebase_auth = None

# --- Hosted MP3 Songs Manifest (for Netlify-hosted songs) ---
# This manifest needs to be generated by your MP3 organization script
# and deployed alongside this app.py.
HOSTED_SONGS_MANIFEST_FILE = 'hosted_songs_manifest.json'
HOSTED_SONGS_DATA = [] # Global variable to store loaded manifest data

# Load the hosted songs manifest once at startup
try:
    if os.path.exists(HOSTED_SONGS_MANIFEST_FILE):
        with open(HOSTED_SONGS_MANIFEST_FILE, 'r', encoding='utf-8') as f:
            HOSTED_SONGS_DATA = json.load(f)
        logging.info(f"Loaded {len(HOSTED_SONGS_DATA)} songs from hosted manifest at startup.")
    else:
        logging.warning(f"Hosted songs manifest file '{HOSTED_SONGS_MANIFEST_FILE}' not found at startup.")
except Exception as e:
    logging.error(f"Error loading hosted songs manifest at startup: {e}")
    HOSTED_SONGS_DATA = [] # Ensure it's empty on error

# --- Helper for getting base URL ---
def get_base_url():
    # In a Vercel environment, request.base_url or request.host_url
    # should correctly reflect the public URL.
    # For local development, it will be http://127.0.0.1:5000/
    return request.host_url

# --- Authentication Decorator ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        session_cookie = request.cookies.get('session')
        if not session_cookie:
            logging.info("Login required: No session cookie found. Redirecting to login.")
            return redirect(url_for('login_page'))
        try:
            if not firebase_auth:
                logging.error("Firebase Admin SDK Auth not initialized. Cannot verify session cookie for login_required.")
                response = make_response(redirect(url_for('login_page')))
                response.set_cookie('session', '', expires=0) # Clear potentially bad cookie
                return response

            # Verify the session cookie. This will also check if the cookie is revoked.
            # Check the Firebase documentation for latest recommended duration.
            decoded_claims = firebase_auth.verify_session_cookie(session_cookie, check_revoked=True)
            request.user = decoded_claims # Attach user info to request object
            logging.info(f"User {request.user['uid']} authenticated via session cookie for route access.")
        except auth.InvalidSessionCookieError:
            logging.warning("Invalid or revoked session cookie. Redirecting to login.")
            response = make_response(redirect(url_for('login_page')))
            response.set_cookie('session', '', expires=0) # Clear invalid cookie
            return response
        except Exception as e:
            logging.error(f"Error verifying session cookie in login_required decorator: {e}")
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated_function

# --- Flask Routes ---
# Public facing login page
@app.route('/login', methods=['GET'])
def login_page():
    return render_template('login.html')

# Public facing registration page
@app.route('/register', methods=['GET'])
def register_page():
    return render_template('register.html')

# Handles user login POST request from frontend
@app.route('/login', methods=['POST'])
def login():
    if not firebase_auth:
        logging.error("Firebase Admin SDK Auth not initialized for login route.")
        return jsonify({"error": "Server authentication not ready."}), 500

    id_token = request.json.get('id_token')
    if not id_token:
        return jsonify({"error": "ID token missing."}), 400

    try:
        # Verify the ID token and create a session cookie
        # Set session expiration to 5 days.
        expires_in = datetime.timedelta(days=5)
        session_cookie = firebase_auth.create_session_cookie(id_token, expires_in=expires_in)

        # Create a response and set the session cookie
        response = make_response(jsonify({"message": "Login successful!"}))
        # httponly=True, secure=True (for HTTPS), samesite='Lax' (good balance for CSRF protection)
        response.set_cookie('session', session_cookie, httponly=True, secure=True, samesite='Lax', expires=datetime.datetime.now() + expires_in)
        logging.info("Session cookie created and set.")
        return response
    except auth.InvalidIdTokenError:
        logging.warning("Invalid ID token during login.")
        return jsonify({"error": "Invalid ID token."}), 401
    except Exception as e:
        logging.error(f"Error during login process: {e}")
        return jsonify({"error": "Authentication failed."}), 500

# Handles user logout POST request from frontend
@app.route('/logout', methods=['POST'])
@login_required # Ensure only logged-in users can log out
def logout():
    session_cookie = request.cookies.get('session')
    if session_cookie and firebase_auth:
        try:
            # Revoke the session cookie
            firebase_auth.revoke_session_cookies(session_cookie)
            logging.info("Session cookie revoked.")
        except Exception as e:
            logging.error(f"Error revoking session cookie: {e}")
            # Even if revocation fails, still clear the client-side cookie
    response = make_response(jsonify({"message": "Logout successful!"}))
    response.set_cookie('session', '', expires=0) # Clear the cookie on the client side
    logging.info("Client-side session cookie cleared.")
    return response

# Main application page, accessible after login
@app.route('/')
@login_required
def index():
    return render_template('index.html')

# Route to serve the hosted_songs_manifest.json dynamically
@app.route('/hosted_songs_manifest.json')
@cache.cached(timeout=86400) # Cache the manifest for 24 hours (86400 seconds)
def hosted_songs_manifest_route():
    """
    Serves the hosted_songs_manifest.json file.
    Uses the globally loaded HOSTED_SONGS_DATA for efficiency.
    """
    if HOSTED_SONGS_DATA:
        logging.info("Served hosted songs manifest from global cache.")
        return jsonify(HOSTED_SONGS_DATA)
    else:
        logging.error("Hosted songs manifest data is empty or not loaded.")
        return jsonify({"error": "Hosted songs manifest not found on server."}), 404


# Route to serve static files from the DOWNLOAD_DIR
@app.route('/downloads/<path:filename>')
def serve_downloaded_file(filename):
    # Security: Ensure filename is safe and within the DOWNLOAD_DIR
    return send_from_directory(DOWNLOAD_DIR, filename)

# COMMENTED OUT ALL YOUTUBE RELATED ROUTES TO IMPROVE EFFICIENCY
# @app.route('/proxy_youtube_audio/<video_id>', methods=['GET'])
# def proxy_youtube_audio(video_id):
#     pass

# @app.route('/download_youtube_audio/<video_id>')
# def download_youtube_audio(video_id):
#     pass

# @app.route('/local_audio/<filename>')
# def local_audio(filename):
#     pass

# @app.route('/youtube_info')
# def youtube_info():
#     pass

# @app.route('/Youtube')
# def Youtube():
#     pass

@app.route('/search_hosted_mp3s') # For Netlify-hosted songs
def search_hosted_mp3s():
    """
    Searches the loaded HOSTED_SONGS_DATA manifest for MP3s matching a query.
    """
    query = request.args.get('query', '').lower()
    
    if not HOSTED_SONGS_DATA:
        return jsonify({"error": "Hosted MP3 songs manifest not loaded or is empty on the server. Please ensure 'hosted_songs_manifest.json' is present."}), 500

    filtered_songs = []
    for song in HOSTED_SONGS_DATA:
        # Check if query is in title or artist (case-insensitive)
        if query in song.get('title', '').lower() or query in song.get('artist', '').lower():
            filtered_songs.append(song)
    
    logging.info(f"Found {len(filtered_songs)} hosted MP3s for query '{query}'")
    return jsonify(filtered_songs)

# --- Jam Session Firestore Utilities ---
# IMPORTANT: Firestore operations are asynchronous. Ensure proper handling in Socket.IO events.

def get_jam_session_ref(jam_id):
    """Returns a reference to a specific jam session document."""
    if not db:
        logging.error("Firestore DB not initialized for get_jam_session_ref.")
        return None
    return db.collection('jam_sessions').document(jam_id)

def get_user_jam_session_status(user_id):
    """Fetches the jam session the user is currently in, if any."""
    if not db:
        logging.error("Firestore DB not initialized for get_user_jam_session_status.")
        return None
    users_ref = db.collection('users').document(user_id)
    user_doc = users_ref.get()
    if user_doc.exists and 'current_jam_session_id' in user_doc.to_dict():
        return user_doc.to_dict()['current_jam_session_id']
    return None

def set_user_jam_session_status(user_id, jam_id=None):
    """Sets or clears the user's current jam session in Firestore."""
    if not db:
        logging.error("Firestore DB not initialized for set_user_jam_session_status.")
        return
    users_ref = db.collection('users').document(user_id)
    if jam_id:
        users_ref.set({'current_jam_session_id': jam_id}, merge=True)
        logging.info(f"User {user_id} joined jam session {jam_id} in Firestore.")
    else:
        users_ref.update({'current_jam_session_id': firestore.DELETE_FIELD})
        logging.info(f"User {user_id} left jam session in Firestore (field deleted).")

# --- Socket.IO Events ---

@socketio.on('connect')
def handle_connect():
    user_id = request.args.get('userId')
    if user_id and db:
        current_jam_id = get_user_jam_session_status(user_id)
        if current_jam_id:
            join_room(current_jam_id)
            logging.info(f"User {user_id} reconnected and joined existing jam session: {current_jam_id}")
            # Emit current state of the jam session to the reconnected user
            emit_jam_session_state(current_jam_id)
        else:
            logging.info(f"User {user_id} connected without an active jam session.")
    else:
        logging.warning("User ID or Firebase DB not available on connect.")
    logging.info(f"Client connected: {request.sid}, User ID: {user_id}")

@socketio.on('disconnect')
def handle_disconnect():
    user_id = request.args.get('userId')
    logging.info(f"Client disconnected: {request.sid}, User ID: {user_id}")
    # No need to remove from jam session here; handled by explicit leave event or user status cleanup

def emit_jam_session_state(jam_id):
    """Emits the current state of a jam session to all clients in that room."""
    if not db:
        logging.error("Firestore DB not initialized for emit_jam_session_state.")
        return
    jam_ref = get_jam_session_ref(jam_id)
    jam_doc = jam_ref.get()
    if jam_doc.exists:
        state = jam_doc.to_dict()
        logging.info(f"Emitting jam session state for {jam_id}: {state}")
        socketio.emit('jam_session_state', state, room=jam_id)
    else:
        logging.warning(f"Attempted to emit state for non-existent jam session: {jam_id}")

# This function should be called whenever the Firestore document for a jam session changes.
def on_jam_session_snapshot(col_snapshot, changes, read_time):
    """Callback for Firestore snapshot listener on jam_sessions collection."""
    for change in changes:
        jam_id = change.document.id
        if change.type.name == 'ADDED' or change.type.name == 'MODIFIED':
            logging.info(f"Firestore Change Detected (MODIFIED/ADDED) for jam_id: {jam_id}")
            emit_jam_session_state(jam_id)
        elif change.type.name == 'REMOVED':
            logging.info(f"Firestore Change Detected (REMOVED) for jam_id: {jam_id}. Emitting jam_ended.")
            socketio.emit('jam_ended', {'jamId': jam_id, 'message': 'The jam session has ended.'}, room=jam_id)
            # Optionally, you might want to force leave all users from this room on the server side
            # This is complex with SocketIO and usually better handled client-side upon 'jam_ended' event.

# Attach the snapshot listener for real-time updates
if db:
    try:
        db.collection('jam_sessions').on_snapshot(on_jam_session_snapshot)
        logging.info("Attached Firestore snapshot listener for 'jam_sessions' collection.")
    except Exception as e:
        logging.error(f"Error attaching Firestore snapshot listener: {e}")
else:
    logging.warning("Firestore DB not available, cannot attach snapshot listener.")

@socketio.on('create_jam_session')
def handle_create_jam_session(data):
    if not db:
        emit('jam_error', {'message': 'Server database not available.'}, room=request.sid)
        return

    user_id = data.get('userId')
    jam_name = data.get('jamName', 'New Jam Session')
    if not user_id:
        emit('jam_error', {'message': 'User not authenticated for creating jam session.'}, room=request.sid)
        return

    # Check if user is already in a jam session
    current_jam_id = get_user_jam_session_status(user_id)
    if current_jam_id:
        emit('jam_error', {'message': f'You are already in jam session: {current_jam_id}. Please leave it first.'}, room=request.sid)
        return

    jam_id = str(uuid.uuid4()) # Generate a unique ID for the jam session
    jam_ref = get_jam_session_ref(jam_id)
    initial_state = {
        'jam_id': jam_id,
        'jam_name': jam_name,
        'current_song': None,
        'current_song_url': None,
        'is_playing': False,
        'play_position': 0,
        'timestamp': firestore.SERVER_TIMESTAMP,
        'participants': [user_id], # Host is the first participant
        'playlist': [], # Initial empty playlist
        'host_id': user_id # Store host ID
    }
    try:
        jam_ref.set(initial_state)
        set_user_jam_session_status(user_id, jam_id) # Update user's status in Firestore
        join_room(jam_id)
        emit('jam_session_created', {'jamId': jam_id, 'jamName': jam_name}, room=request.sid)
        emit_jam_session_state(jam_id) # Announce the new session state to the creator
        logging.info(f"User {user_id} created and joined jam session: {jam_id} - '{jam_name}'")
    except Exception as e:
        logging.error(f"Error creating jam session for user {user_id}: {e}")
        emit('jam_error', {'message': f'Failed to create jam session: {e}'}, room=request.sid)

@socketio.on('join_jam_session')
def handle_join_jam_session(data):
    if not db:
        emit('jam_error', {'message': 'Server database not available.'}, room=request.sid)
        return

    user_id = data.get('userId')
    jam_id = data.get('jamId')
    if not user_id or not jam_id:
        emit('jam_error', {'message': 'User ID or Jam ID missing.'}, room=request.sid)
        return

    # Check if user is already in a jam session
    current_jam_id = get_user_jam_session_status(user_id)
    if current_jam_id and current_jam_id != jam_id:
        emit('jam_error', {'message': f'You are already in jam session: {current_jam_id}. Please leave it first.'}, room=request.sid)
        return

    jam_ref = get_jam_session_ref(jam_id)
    jam_doc = jam_ref.get()

    if jam_doc.exists:
        try:
            current_participants = jam_doc.to_dict().get('participants', [])
            if user_id not in current_participants:
                current_participants.append(user_id)
                jam_ref.update({'participants': current_participants})
                set_user_jam_session_status(user_id, jam_id) # Update user's status in Firestore

            join_room(jam_id)
            emit('jam_session_joined', {'jamId': jam_id, 'jamName': jam_doc.to_dict().get('jam_name', 'Unknown Jam')}, room=request.sid)
            emit_jam_session_state(jam_id) # Send current state to newly joined user
            logging.info(f"User {user_id} joined jam session: {jam_id}")
        except Exception as e:
            logging.error(f"Error joining jam session {jam_id} for user {user_id}: {e}")
            emit('jam_error', {'message': f'Failed to join jam session: {e}'}, room=request.sid)
    else:
        emit('jam_error', {'message': 'Jam session not found.'}, room=request.sid)
        logging.warning(f"User {user_id} attempted to join non-existent jam session: {jam_id}")

@socketio.on('leave_jam_session')
def handle_leave_jam_session(data):
    if not db:
        emit('jam_error', {'message': 'Server database not available.'}, room=request.sid)
        return

    user_id = data.get('userId')
    jam_id = data.get('jamId')
    if not user_id or not jam_id:
        emit('jam_error', {'message': 'User ID or Jam ID missing.'}, room=request.sid)
        return

    jam_ref = get_jam_session_ref(jam_id)
    jam_doc = jam_ref.get()

    if jam_doc.exists:
        try:
            current_participants = jam_doc.to_dict().get('participants', [])
            if user_id in current_participants:
                current_participants.remove(user_id)
                if current_participants: # If there are still participants, update list
                    jam_ref.update({'participants': current_participants})
                    logging.info(f"User {user_id} removed from jam session {jam_id} participants.")
                else: # If no participants left, delete the jam session
                    jam_ref.delete()
                    logging.info(f"Jam session {jam_id} deleted as no participants remain.")
            
            set_user_jam_session_status(user_id, None) # Clear user's status in Firestore
            leave_room(jam_id)
            emit('jam_session_left', {'jamId': jam_id, 'message': 'You have left the jam session.'}, room=request.sid)
            emit_jam_session_state(jam_id) # Update state for remaining participants (if any)
            logging.info(f"User {user_id} left jam session: {jam_id}")

        except Exception as e:
            logging.error(f"Error leaving jam session {jam_id} for user {user_id}: {e}")
            emit('jam_error', {'message': f'Failed to leave jam session: {e}'}, room=request.sid)
    else:
        emit('jam_error', {'message': 'Jam session not found.'}, room=request.sid)
        logging.warning(f"User {user_id} attempted to leave non-existent jam session: {jam_id}")

@socketio.on('update_jam_state')
def handle_update_jam_state(data):
    """
    Handles updates to the jam session state from the host.
    This includes current song, playback position, and playing status.
    """
    if not db:
        emit('jam_error', {'message': 'Server database not available.'}, room=request.sid)
        return

    user_id = data.get('userId')
    jam_id = data.get('jamId')
    if not user_id or not jam_id:
        emit('jam_error', {'message': 'User ID or Jam ID missing.'}, room=request.sid)
        return

    jam_ref = get_jam_session_ref(jam_id)
    jam_doc = jam_ref.get()

    if jam_doc.exists:
        current_jam_state = jam_doc.to_dict()
        if current_jam_state.get('host_id') != user_id:
            emit('jam_error', {'message': 'Only the host can update the jam session state.'}, room=request.sid)
            return

        # Prepare updates
        updates = {}
        if 'currentSong' in data:
            updates['current_song'] = data['currentSong']
            updates['current_song_url'] = data['currentSongUrl']
        if 'isPlaying' in data:
            updates['is_playing'] = data['isPlaying']
        if 'playPosition' in data:
            updates['play_position'] = data['playPosition']
        if 'playlist' in data:
            updates['playlist'] = data['playlist']

        updates['timestamp'] = firestore.SERVER_TIMESTAMP # Update timestamp on any state change

        try:
            jam_ref.update(updates)
            logging.info(f"Jam session {jam_id} state updated by host {user_id}.")
            # The on_jam_session_snapshot listener will automatically emit the updated state to the room
        except Exception as e:
            logging.error(f"Error updating jam session {jam_id} state: {e}")
            emit('jam_error', {'message': f'Failed to update jam session state: {e}'}, room=request.sid)
    else:
        emit('jam_error', {'message': 'Jam session not found.'}, room=request.sid)

@socketio.on('add_hosted_song')
def handle_add_hosted_song(data):
    if not db:
        emit('jam_error', {'message': 'Server database not available.'}, room=request.sid)
        return

    user_id = data.get('userId')
    jam_id = data.get('jamId')
    song_data = data.get('song') # Expecting a dict with id, title, artist, url, duration, type, thumbnail
    if not user_id or not jam_id or not song_data:
        emit('jam_error', {'message': 'Missing user ID, jam ID, or song data.'}, room=request.sid)
        return

    jam_ref = get_jam_session_ref(jam_id)
    jam_doc = jam_ref.get()

    if jam_doc.exists:
        try:
            current_playlist = jam_doc.to_dict().get('playlist', [])
            current_playlist.append(song_data)
            jam_ref.update({'playlist': current_playlist, 'timestamp': firestore.SERVER_TIMESTAMP})
            logging.info(f"Added hosted song '{song_data.get('title')}' to jam session {jam_id}.")
            emit('song_added_to_playlist', song_data, room=request.sid)
            emit_jam_session_state(jam_id) # Update all clients in the jam session
        except Exception as e:
            logging.error(f"Error adding hosted song: {e}")
            emit('jam_error', {'message': f'Failed to add hosted song: {e}'}, room=request.sid)
    else:
        emit('jam_error', {'message': 'Jam session not found.'}, room=request.sid)

@socketio.on('remove_song_from_playlist')
def handle_remove_song_from_playlist(data):
    if not db:
        emit('jam_error', {'message': 'Server database not available.'}, room=request.sid)
        return

    user_id = data.get('userId')
    jam_id = data.get('jamId')
    song_id_to_remove = data.get('songId')
    if not user_id or not jam_id or not song_id_to_remove:
        emit('jam_error', {'message': 'Missing user ID, jam ID, or song ID.'}, room=request.sid)
        return

    jam_ref = get_jam_session_ref(jam_id)
    jam_doc = jam_ref.get()

    if jam_doc.exists:
        try:
            current_playlist = jam_doc.to_dict().get('playlist', [])
            updated_playlist = [song for song in current_playlist if song.get('id') != song_id_to_remove]

            if len(current_playlist) != len(updated_playlist): # If a song was actually removed
                jam_ref.update({'playlist': updated_playlist, 'timestamp': firestore.SERVER_TIMESTAMP})
                logging.info(f"Removed song {song_id_to_remove} from jam session {jam_id}.")
                emit('song_removed_from_playlist', {'songId': song_id_to_remove}, room=request.sid)
                emit_jam_session_state(jam_id) # Update all clients in the jam session
            else:
                emit('jam_error', {'message': 'Song not found in playlist.'}, room=request.sid)

        except Exception as e:
            logging.error(f"Error removing song from playlist: {e}")
            emit('jam_error', {'message': f'Failed to remove song: {e}'}, room=request.sid)
    else:
        emit('jam_error', {'message': 'Jam session not found.'}, room=request.sid)


# --- Error Handlers ---
if db is None:
    logging.error("Firestore DB is not initialized. Jam Session feature will not work.")
if firebase_auth is None:
    logging.error("Firebase Admin SDK Auth is not initialized. Authentication features will not work.")
    
# This line is for local development only. Vercel will run `app` directly.
# if __name__ == '__main__':
#     socketio.run(app, debug=True, port=5000)

# Global error handler to ensure all errors return JSON responses
@app.errorhandler(HTTPException) # Catch all HTTP errors from Werkzeug/Flask
def handle_http_exception(e):
    # Log the error details
    logging.error(f"Global HTTP error handler caught: {e}")
    
    # Get the status code from the HTTPException
    code = e.code if isinstance(e, HTTPException) else 500
    message = e.description if isinstance(e, HTTPException) and e.description else "An unexpected server error occurred."

    # For internal server errors (500), provide a generic message to the client
    if code == 500:
        message = "An internal server error occurred. Please try again later."
    
    response = jsonify(error={"code": code, "message": message})
    response.status_code = code
    return response

@app.errorhandler(Exception) # Catch all other Python exceptions
def handle_generic_exception(e):
    logging.error(f"Global generic exception handler caught: {e}", exc_info=True) # Log full traceback
    response = jsonify(error={"code": 500, "message": "An internal server error occurred. Please try again later."})
    response.status_code = 500
    return response

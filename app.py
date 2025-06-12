import os
import json
import eventlet
eventlet.monkey_patch() # Patch standard library for async operations (e.g., requests)
import tempfile
import requests # Used for robust HTTP requests, especially for streaming

from flask import Flask, request, Response, abort, render_template, jsonify, make_response, redirect, url_for, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
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
if os.environ.get('FLASK_SECRET_KEY'):
    app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY')
    logging.info("Flask SECRET_KEY loaded from environment variable.")
else:
    app.config['SECRET_KEY'] = secrets.token_hex(32)
    logging.warning("FLASK_SECRET_KEY environment variable is NOT set. "
                    "A random key has been generated for this session. "
                    "For production deployments, please set FLASK_SECRET_KEY in your environment variables "
                    "to ensure session security.")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- CONFIGURATION: Flask-Caching (Simplified to 'simple' in-memory cache) ---
app.config["CACHE_TYPE"] = "simple"
app.config["CACHE_DEFAULT_TIMEOUT"] = 3600 # Cache items for 1 hour (3600 seconds) by default
logging.info("Flask-Caching configured with 'simple' in-memory cache.")

cache = Cache(app) # Initialize the cache after configuration

# Explicitly pass the Flask app to SocketIO and set async_mode
socketio = SocketIO(app, cors_allowed_origins="*", manage_session=False, async_mode='eventlet')

logging.info("Flask app and SocketIO initialized.")

# --- Ephemeral Directory for Downloads ---
DOWNLOAD_DIR = tempfile.mkdtemp() # Correctly uses a writable temporary directory
logging.info(f"Using temporary directory for downloads: {DOWNLOAD_DIR}")

# --- Firebase Admin SDK Initialization (for Firestore and Auth) ---
db = None
firebase_auth = None

try:
    firebase_credentials_json = os.environ.get('FIREBASE_ADMIN_CREDENTIALS_JSON')

    if firebase_credentials_json:
        try:
            cred_dict = json.loads(firebase_credentials_json)
            cred = credentials.Certificate(cred_dict)
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)
            db = firestore.client()
            firebase_auth = auth
            logging.info("Firebase Admin SDK initialized successfully from environment variable.")
        except json.JSONDecodeError as e:
            logging.error(f"Error decoding FIREBASE_ADMIN_CREDENTIALS_JSON: {e}")
        except Exception as e:
            logging.error(f"Error initializing Firebase Admin SDK from environment variable: {e}")
    else:
        FIREBASE_ADMIN_KEY_FILE_LOCAL = 'firebase_admin_key.json'
        if os.path.exists(FIREBASE_ADMIN_KEY_FILE_LOCAL):
            try:
                if not firebase_admin._apps:
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
            db = None
            firebase_auth = None
except Exception as e:
    logging.error(f"An unexpected error occurred during Firebase Admin SDK setup: {e}")
    db = None
    firebase_auth = None

# --- Hosted MP3 Songs Manifest ---
HOSTED_SONGS_MANIFEST_FILE = 'hosted_songs_manifest.json'
HOSTED_SONGS_DATA = []

try:
    if os.path.exists(HOSTED_SONGS_MANIFEST_FILE):
        with open(HOSTED_SONGS_MANIFEST_FILE, 'r', encoding='utf-8') as f:
            HOSTED_SONGS_DATA = json.load(f)
        logging.info(f"Loaded {len(HOSTED_SONGS_DATA)} songs from hosted manifest at startup.")
    else:
        logging.warning(f"Hosted songs manifest file '{HOSTED_SONGS_MANIFEST_FILE}' not found at startup.")
except Exception as e:
    logging.error(f"Error loading hosted songs manifest at startup: {e}")
    HOSTED_SONGS_DATA = []

# --- Helper for getting base URL ---
def get_base_url():
    # In a Vercel environment, request.base_url or request.host_url
    # should correctly reflect the public URL.
    # For local development, it will be http://127.0.0.1:5000/
    # Ensure this works correctly in your Vercel setup
    # If not, you might need to get the Vercel URL from an environment variable
    return request.url_root # This typically returns http://example.com/

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
                response.set_cookie('session', '', expires=0)
                return response

            decoded_claims = firebase_auth.verify_session_cookie(session_cookie, check_revoked=True)
            request.user = decoded_claims
            logging.info(f"User {request.user['uid']} authenticated via session cookie for route access.")
        except auth.InvalidSessionCookieError:
            logging.warning("Invalid or revoked session cookie. Redirecting to login.")
            response = make_response(redirect(url_for('login_page')))
            response.set_cookie('session', '', expires=0)
            return response
        except Exception as e:
            logging.error(f"Error verifying session cookie in login_required decorator: {e}")
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated_function

# --- Flask Routes ---
@app.route('/login', methods=['GET'])
def login_page():
    return render_template('login.html')

@app.route('/register', methods=['GET'])
def register_page():
    return render_template('register.html')

@app.route('/login', methods=['POST'])
def login():
    if not firebase_auth:
        logging.error("Firebase Admin SDK Auth not initialized for login route.")
        return jsonify({"error": "Server authentication not ready."}), 500

    id_token = request.json.get('id_token')
    if not id_token:
        return jsonify({"error": "ID token missing."}), 400

    try:
        expires_in = datetime.timedelta(days=5)
        session_cookie = firebase_auth.create_session_cookie(id_token, expires_in=expires_in)

        response = make_response(jsonify({"message": "Login successful!"}))
        response.set_cookie('session', session_cookie, httponly=True, secure=True, samesite='Lax', expires=datetime.datetime.now() + expires_in)
        logging.info("Session cookie created and set.")
        return response
    except auth.InvalidIdTokenError:
        logging.warning("Invalid ID token during login.")
        return jsonify({"error": "Invalid ID token."}), 401
    except Exception as e:
        logging.error(f"Error during login process: {e}")
        return jsonify({"error": "Authentication failed."}), 500

@app.route('/logout', methods=['POST'])
@login_required
def logout():
    session_cookie = request.cookies.get('session')
    if session_cookie and firebase_auth:
        try:
            firebase_auth.revoke_session_cookies(session_cookie)
            logging.info("Session cookie revoked.")
        except Exception as e:
            logging.error(f"Error revoking session cookie: {e}")
    response = make_response(jsonify({"message": "Logout successful!"}))
    response.set_cookie('session', '', expires=0)
    logging.info("Client-side session cookie cleared.")
    return response

@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/hosted_songs_manifest.json')
@cache.cached(timeout=86400)
def hosted_songs_manifest_route():
    if HOSTED_SONGS_DATA:
        logging.info("Served hosted songs manifest from global cache.")
        return jsonify(HOSTED_SONGS_DATA)
    else:
        logging.error("Hosted songs manifest data is empty or not loaded.")
        return jsonify({"error": "Hosted songs manifest not found on server."}), 404

@app.route('/downloads/<path:filename>')
def serve_downloaded_file(filename):
    return send_from_directory(DOWNLOAD_DIR, filename)

@app.route('/search_hosted_mp3s')
def search_hosted_mp3s():
    query = request.args.get('query', '').lower()
    
    if not HOSTED_SONGS_DATA:
        return jsonify({"error": "Hosted MP3 songs manifest not loaded or is empty on the server. Please ensure 'hosted_songs_manifest.json' is present."}), 500

    filtered_songs = []
    for song in HOSTED_SONGS_DATA:
        if query in song.get('title', '').lower() or query in song.get('artist', '').lower():
            filtered_songs.append(song)
    
    logging.info(f"Found {len(filtered_songs)} hosted MP3s for query '{query}'")
    return jsonify(filtered_songs)

# --- Jam Session Firestore Utilities ---
def get_jam_session_ref(jam_id):
    if not db:
        logging.error("Firestore DB not initialized for get_jam_session_ref.")
        return None
    return db.collection('jam_sessions').document(jam_id)

async def generate_unique_6_digit_jam_id():
    """Generates a unique 6-digit numeric jam ID."""
    if not db:
        logging.error("Firestore DB not initialized. Cannot generate unique jam ID.")
        return None
    
    for _ in range(10): # Try up to 10 times to find a unique ID
        jam_id = str(random.randint(100000, 999999))
        jam_doc = await db.collection('jam_sessions').document(jam_id).get()
        if not jam_doc.exists:
            return jam_id
    logging.error("Failed to generate a unique 6-digit jam ID after multiple attempts.")
    return None

def get_user_jam_session_status(user_id):
    if not db:
        logging.error("Firestore DB not initialized for get_user_jam_session_status.")
        return None
    users_ref = db.collection('users').document(user_id)
    user_doc = users_ref.get()
    if user_doc.exists and 'current_jam_session_id' in user_doc.to_dict():
        return user_doc.to_dict()['current_jam_session_id']
    return None

def set_user_jam_session_status(user_id, jam_id=None):
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
    logging.info(f"Client connected: {request.sid}, User ID: {user_id}")
    # Logic to rejoin existing jam session will be handled by client or Firestore listener
    # The current_jam_id check was moved to client-side for better reactive UI.

@socketio.on('disconnect')
def handle_disconnect():
    user_id = request.args.get('userId')
    logging.info(f"Client disconnected: {request.sid}, User ID: {user_id}")
    # Removed automatic leave logic here; client should explicitly leave or host handles session end

def emit_jam_session_state(jam_id):
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
# This listener setup ensures real-time updates are pushed to all connected clients.
if db:
    try:
        # Use a collection group query for all jam sessions if you want a single listener
        # Or listen to individual jam sessions when a user joins one
        # For simplicity, we are listening to all changes on 'jam_sessions'
        db.collection('jam_sessions').on_snapshot(on_jam_session_snapshot)
        logging.info("Attached Firestore snapshot listener for 'jam_sessions' collection.")
    except Exception as e:
        logging.error(f"Error attaching Firestore snapshot listener: {e}")
else:
    logging.warning("Firestore DB not available, cannot attach snapshot listener for jam sessions.")


@socketio.on('create_session')
async def handle_create_session(data):
    if not db or not firebase_auth:
        emit('join_failed', {'message': 'Server database or authentication not available.'}, room=request.sid)
        return

    user_id = data.get('userId')
    jam_name = data.get('jam_name', 'Unnamed Jam')
    nickname = data.get('nickname', 'Host')

    if not user_id:
        emit('join_failed', {'message': 'User not authenticated for creating jam session.'}, room=request.sid)
        return

    # Generate a unique 6-digit jam ID
    jam_id = await generate_unique_6_digit_jam_id()
    if not jam_id:
        emit('join_failed', {'message': 'Could not generate a unique jam ID. Please try again.'}, room=request.sid)
        return

    jam_ref = get_jam_session_ref(jam_id)
    
    # Construct the shareable link for the frontend
    base_url = get_base_url()
    shareable_link = f"{base_url}?jam_id={jam_id}"

    initial_state = {
        'name': jam_name,
        'host_id': user_id,
        'host_sid': request.sid, # Store the host's Socket.IO SID
        'participants': {request.sid: nickname}, # Map of SID to nickname
        'playlist': [],
        'playback_state': {
            'current_track_index': 0,
            'current_playback_time': 0,
            'is_playing': False,
            'timestamp': firestore.SERVER_TIMESTAMP
        },
        'is_active': True,
        'created_at': firestore.SERVER_TIMESTAMP
    }
    try:
        await jam_ref.set(initial_state) # Use await for async Firestore operation
        set_user_jam_session_status(user_id, jam_id) # Set user's current jam

        join_room(jam_id) # Join the Socket.IO room

        emit('session_created', {
            'jam_id': jam_id,
            'jam_name': jam_name,
            'shareable_link': shareable_link,
            'is_host': True,
            'nickname_used': nickname,
            'participants': initial_state['participants']
        }, room=request.sid)
        logging.info(f"User {user_id} (SID: {request.sid}) created jam session: {jam_id} - '{jam_name}'")
    except Exception as e:
        logging.error(f"Error creating jam session for user {user_id}: {e}")
        emit('join_failed', {'message': f'Failed to create jam session: {e}'}, room=request.sid)

@socketio.on('join_session')
async def handle_join_session(data):
    if not db or not firebase_auth:
        emit('join_failed', {'message': 'Server database or authentication not available.'}, room=request.sid)
        return

    user_id = data.get('userId')
    jam_id = data.get('jam_id')
    nickname = data.get('nickname', 'Listener')

    if not user_id or not jam_id:
        emit('join_failed', {'message': 'User ID or Jam ID missing.'}, room=request.sid)
        return

    jam_ref = get_jam_session_ref(jam_id)
    jam_doc = await jam_ref.get() # Use await

    if jam_doc.exists and jam_doc.to_dict().get('is_active'):
        try:
            current_participants = jam_doc.to_dict().get('participants', {})
            # Add new participant (Socket.IO SID to nickname mapping)
            current_participants[request.sid] = nickname 
            await jam_ref.update({'participants': current_participants}) # Use await
            set_user_jam_session_status(user_id, jam_id) # Set user's current jam

            join_room(jam_id) # Join the Socket.IO room

            # Emit success to joining client with current jam state
            jam_state = jam_doc.to_dict()
            emit('session_join_success', {
                'jam_id': jam_id,
                'jam_name': jam_state.get('name', 'Unknown Jam'),
                'playlist': jam_state.get('playlist', []),
                'playback_state': jam_state.get('playback_state', {}),
                'current_track_index': jam_state.get('playback_state', {}).get('current_track_index', 0),
                'current_playback_time': jam_state.get('playback_state', {}).get('current_playback_time', 0),
                'is_playing': jam_state.get('playback_state', {}).get('is_playing', False),
                'last_synced_at': jam_state.get('playback_state', {}).get('timestamp', 0), # Pass timestamp for client-side sync
                'nickname_used': nickname,
                'participants': current_participants # Send updated list of participants
            }, room=request.sid)

            # Inform all other clients in the room about the new participant
            socketio.emit('update_participants', {
                'jam_id': jam_id,
                'participants': current_participants
            }, room=jam_id, skip_sid=request.sid)

            logging.info(f"User {user_id} (SID: {request.sid}) joined jam session: {jam_id}")
        except Exception as e:
            logging.error(f"Error joining jam session {jam_id} for user {user_id}: {e}")
            emit('join_failed', {'message': f'Failed to join jam session: {e}'}, room=request.sid)
    else:
        emit('join_failed', {'message': 'Jam session not found or is inactive.'}, room=request.sid)
        logging.warning(f"User {user_id} attempted to join non-existent jam session: {jam_id}")

@socketio.on('leave_session')
async def handle_leave_session(data):
    if not db:
        emit('jam_error', {'message': 'Server database not available.'}, room=request.sid)
        return

    user_id = data.get('userId')
    jam_id = data.get('jam_id')

    if not user_id or not jam_id:
        emit('jam_error', {'message': 'User ID or Jam ID missing.'}, room=request.sid)
        return

    jam_ref = get_jam_session_ref(jam_id)
    jam_doc = await jam_ref.get() # Use await

    if jam_doc.exists:
        try:
            jam_data = jam_doc.to_dict()
            current_participants = jam_data.get('participants', {})

            if request.sid in current_participants:
                del current_participants[request.sid] # Remove by SID

                if jam_data.get('host_sid') == request.sid: # If host is leaving
                    await jam_ref.update({'is_active': False, 'ended_at': firestore.SERVER_TIMESTAMP})
                    logging.info(f"Host (SID: {request.sid}) ended jam session {jam_id}.")
                    # No need to update participants if session is ending, as 'session_ended' will be sent
                else: # Participant leaving
                    await jam_ref.update({'participants': current_participants})
                    logging.info(f"User (SID: {request.sid}) left jam session {jam_id}.")
                    # Inform others about updated participant list
                    socketio.emit('update_participants', {
                        'jam_id': jam_id,
                        'participants': current_participants
                    }, room=jam_id, skip_sid=request.sid)
            
            set_user_jam_session_status(user_id, None) # Clear user's current jam status in Firestore
            leave_room(jam_id)
            emit('session_ended', {'jam_id': jam_id, 'message': 'You have left the jam session.'}, room=request.sid) # Confirm leave to self

        except Exception as e:
            logging.error(f"Error leaving jam session {jam_id} for user {user_id}: {e}")
            emit('jam_error', {'message': f'Failed to leave jam session: {e}'}, room=request.sid)
    else:
        emit('jam_error', {'message': 'Jam session not found.'}, room=request.sid)
        logging.warning(f"User {user_id} attempted to leave non-existent jam session: {jam_id}")

@socketio.on('sync_playback_state')
async def handle_sync_playback_state(data):
    if not db:
        emit('jam_error', {'message': 'Server database not available.'}, room=request.sid)
        return

    jam_id = data.get('jam_id')
    current_track_index = data.get('current_track_index')
    current_playback_time = data.get('current_playback_time')
    is_playing = data.get('is_playing')
    playlist = data.get('playlist') # Host sends its current playlist

    jam_ref = get_jam_session_ref(jam_id)
    jam_doc = await jam_ref.get() # Use await

    if jam_doc.exists:
        jam_data = jam_doc.to_dict()
        if jam_data.get('host_sid') != request.sid:
            emit('jam_error', {'message': 'Only the host can control playback.'}, room=request.sid)
            return

        playback_state = {
            'current_track_index': current_track_index,
            'current_playback_time': current_playback_time,
            'is_playing': is_playing,
            'timestamp': firestore.SERVER_TIMESTAMP # Update timestamp on every sync
        }
        try:
            # Update both playlist and playback state in one go
            await jam_ref.update({
                'playlist': playlist,
                'playback_state': playback_state
            }) # Use await
            logging.info(f"Jam session {jam_id} state updated by host (SID: {request.sid}).")
            # Firestore listener will propagate this change to all clients in the room.
        except Exception as e:
            logging.error(f"Error updating jam session {jam_id} state from host: {e}")
            emit('jam_error', {'message': f'Failed to sync state: {e}'}, room=request.sid)
    else:
        emit('jam_error', {'message': 'Jam session not found.'}, room=request.sid)

@socketio.on('add_song_to_jam')
async def handle_add_song_to_jam(data):
    if not db:
        emit('jam_error', {'message': 'Server database not available.'}, room=request.sid)
        return

    jam_id = data.get('jam_id')
    song = data.get('song') # Expecting a dict with song details

    if not jam_id or not song:
        emit('jam_error', {'message': 'Missing jam ID or song data.'}, room=request.sid)
        return

    jam_ref = get_jam_session_ref(jam_id)
    jam_doc = await jam_ref.get()

    if jam_doc.exists:
        jam_data = jam_doc.to_dict()
        if jam_data.get('host_sid') != request.sid:
            emit('jam_error', {'message': 'Only the host can add songs to the playlist.'}, room=request.sid)
            return

        current_playlist = jam_data.get('playlist', [])
        current_playlist.append(song)
        try:
            await jam_ref.update({
                'playlist': current_playlist,
                'playback_state.timestamp': firestore.SERVER_TIMESTAMP # Update timestamp to trigger sync
            })
            logging.info(f"Song '{song.get('title', 'unknown')}' added to jam {jam_id} by host.")
            # The Firestore listener will propagate this to all clients
        except Exception as e:
            logging.error(f"Error adding song to jam {jam_id} playlist: {e}")
            emit('jam_error', {'message': f'Failed to add song: {e}'}, room=request.sid)
    else:
        emit('jam_error', {'message': 'Jam session not found.'}, room=request.sid)

@socketio.on('remove_song_from_jam')
async def handle_remove_song_from_jam(data):
    if not db:
        emit('jam_error', {'message': 'Server database not available.'}, room=request.sid)
        return

    jam_id = data.get('jam_id')
    song_id_to_remove = data.get('song_id')

    if not jam_id or not song_id_to_remove:
        emit('jam_error', {'message': 'Missing jam ID or song ID to remove.'}, room=request.sid)
        return

    jam_ref = get_jam_session_ref(jam_id)
    jam_doc = await jam_ref.get()

    if jam_doc.exists:
        jam_data = jam_doc.to_dict()
        if jam_data.get('host_sid') != request.sid:
            emit('jam_error', {'message': 'Only the host can remove songs from the playlist.'}, room=request.sid)
            return

        current_playlist = jam_data.get('playlist', [])
        updated_playlist = [s for s in current_playlist if s.get('id') != song_id_to_remove]

        if len(current_playlist) != len(updated_playlist): # If a song was actually removed
            try:
                await jam_ref.update({
                    'playlist': updated_playlist,
                    'playback_state.timestamp': firestore.SERVER_TIMESTAMP # Update timestamp
                })
                logging.info(f"Song '{song_id_to_remove}' removed from jam {jam_id} by host.")
                # Firestore listener will propagate this to all clients
            except Exception as e:
                logging.error(f"Error removing song '{song_id_to_remove}' from jam {jam_id}: {e}")
                emit('jam_error', {'message': f'Failed to remove song: {e}'}, room=request.sid)
        else:
            emit('jam_error', {'message': 'Song not found in playlist.'}, room=request.sid)
    else:
        emit('jam_error', {'message': 'Jam session not found.'}, room=request.sid)


# --- Error Handlers ---
if db is None:
    logging.error("Firestore DB is not initialized. Jam Session feature will not work.")
if firebase_auth is None:
    logging.error("Firebase Admin SDK Auth is not initialized. Authentication features will not work.")
    
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
    response = jsonify(error={"code": 500, "message": "An internal server error occurred. Please try again later."})
    response.status_code = 500
    return response


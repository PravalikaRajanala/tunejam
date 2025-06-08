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
import firebase_admin
from firebase_admin import credentials, firestore, auth
from functools import wraps
import datetime # For session cookie expiration
from flask_caching import Cache # Import Flask-Caching
import secrets # Import secrets for generating a secure key

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
# We will now serve this dynamically with caching.
HOSTED_SONGS_MANIFEST_FILE = 'hosted_songs_manifest.json'

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
        logging.info(f"User logged in, session cookie set for UID: {firebase_auth.verify_id_token(id_token)['uid']}.")
        return response

    except auth.InvalidIdTokenError:
        logging.warning("Invalid ID token during login attempt.")
        return jsonify({"error": "Invalid ID token."}), 401
    except Exception as e:
        logging.error(f"Error during login process: {e}")
        return jsonify({"error": f"Authentication failed: {e}"}), 500

# Handles user registration POST request from frontend
@app.route('/register', methods=['POST'])
def register():
    if not firebase_auth or not db:
        logging.error("Firebase Admin SDK or Firestore not initialized for register route.")
        return jsonify({"error": "Server components not ready for registration."}), 500

    id_token = request.json.get('id_token')
    username = request.json.get('username')
    favorite_artist = request.json.get('favorite_artist')
    favorite_genre = request.json.get('favorite_genre')
    experience_level = request.json.get('experience_level')

    if not id_token or not username:
        return jsonify({"error": "Missing required registration data (id_token or username)."}), 400

    try:
        # Verify the ID token to get the UID
        decoded_token = firebase_auth.verify_id_token(id_token)
        uid = decoded_token['uid']

        # Store additional user data in Firestore
        user_ref = db.collection('users').document(uid)
        user_ref.set({
            'username': username,
            'email': decoded_token.get('email'), # Get email from token
            'favorite_artist': favorite_artist,
            'favorite_genre': favorite_genre,
            'experience_level': experience_level,
            'created_at': firestore.SERVER_TIMESTAMP
        })

        # Create a session cookie for the newly registered user
        expires_in = datetime.timedelta(days=5)
        session_cookie = firebase_auth.create_session_cookie(id_token, expires_in=expires_in)

        response = make_response(jsonify({"message": "Registration successful!"}))
        response.set_cookie('session', session_cookie, httponly=True, secure=True, samesite='Lax', expires=datetime.datetime.now() + expires_in)
        logging.info(f"User {uid} registered and session cookie set.")
        return response

    except auth.InvalidIdTokenError:
        logging.warning("Invalid ID token during registration attempt.")
        return jsonify({"error": "Invalid ID token."}), 401
    except Exception as e:
        logging.error(f"Error during registration process: {e}")
        return jsonify({"error": f"Registration failed: {e}"}), 500

# Handles user logout
@app.route('/logout', methods=['POST'])
def logout():
    if not firebase_auth:
        logging.error("Firebase Admin SDK Auth not initialized for logout route.")
        return jsonify({"error": "Server authentication not ready."}), 500

    session_cookie = request.cookies.get('session')
    if session_cookie:
        try:
            # Revoke the session cookie
            decoded_claims = firebase_auth.verify_session_cookie(session_cookie)
            firebase_auth.revoke_refresh_tokens(decoded_claims['sub'])
            logging.info(f"Revoked refresh token for user: {decoded_claims['sub']}")
        except Exception as e:
            logging.warning(f"Error revoking session cookie during logout: {e}")
    
    response = make_response(jsonify({"message": "Logged out successfully!"}))
    response.set_cookie('session', '', expires=0, httponly=True, secure=True, samesite='Lax') # Clear the cookie
    logging.info("User logged out, session cookie cleared.")
    return response

# Main application page, now protected
@app.route('/dashboard')
@login_required # Protect this route
def dashboard():
    # If login_required passes, request.user contains the decoded Firebase claims
    # We pass __initial_auth_token to client which needs to sign in again with this token.
    # The token is generated from the session cookie on the server.
    if firebase_auth:
        try:
            # Create a custom token for the client-side Firebase SDK based on the authenticated user's UID
            # This token is temporary and passed to the frontend for client-side authentication.
            initial_auth_token = firebase_auth.create_custom_token(request.user['uid']).decode('utf-8')
            logging.info(f"Generated custom token for dashboard user: {request.user['uid']}")
        except Exception as e:
            logging.error(f"Error generating custom token for dashboard: {e}")
            initial_auth_token = None # Fallback to anonymous if token generation fails
    else:
        initial_auth_token = None # Firebase Admin SDK not initialized

    # Pass the initial_auth_token and current app ID to the frontend
    return render_template('index.html', 
                           __initial_auth_token=json.dumps(initial_auth_token) if initial_auth_token else 'null',
                           __app_id=os.environ.get('VERCEL_GIT_COMMIT_SHA', 'default-app-id')) # Vercel provides a unique ID


# Default root route - redirect to login page
@app.route('/')
def index():
    return redirect(url_for('login_page'))

# Route to handle joining a session via a URL (e.g., /join/some_jam_id)
# This route also needs to be protected
@app.route('/join/<jam_id>')
@login_required # Protect this route as well
def join_by_link(jam_id):
    logging.info(f"Received request to join jam via link: {jam_id}")
    # This route simply serves the main application page and passes the jam_id.
    # The client-side JavaScript will then read the jam_id from the URL.
    if firebase_auth:
        try:
            initial_auth_token = firebase_auth.create_custom_token(request.user['uid']).decode('utf-8')
            logging.info(f"Generated custom token for join link user: {request.user['uid']}")
        except Exception as e:
            logging.error(f"Error generating custom token for join link: {e}")
            initial_auth_token = None
    else:
        initial_auth_token = None
    
    return render_template('index.html', 
                           initial_jam_id=jam_id,
                           __initial_auth_token=json.dumps(initial_auth_token) if initial_auth_token else 'null',
                           __app_id=os.environ.get('VERCEL_GIT_COMMIT_SHA', 'default-app-id'))

@app.route('/hosted_songs_manifest.json')
@cache.cached(timeout=86400) # Cache the manifest for 24 hours (86400 seconds)
def hosted_songs_manifest_route():
    """
    Serves the hosted_songs_manifest.json file.
    Cached to improve performance as this file is requested frequently.
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
            
            audio_url = None
            audio_ext = None
            content_length = None
            
            # Prioritize finding a format with a filesize for better content-length header accuracy
            for f in info.get('formats', []):
                if f.get('ext') in ['m4a', 'webm', 'mp3', 'ogg', 'opus'] and f.get('url') and f.get('acodec') != 'none':
                    if f.get('filesize') is not None:
                        audio_url = f['url']
                        audio_ext = f['ext']
                        content_length = f['filesize']
                        break
            # Fallback if no filesize found, just get the first suitable audio format
            if not audio_url:
                for f in info.get('formats', []):
                    if f.get('ext') in ['m4a', 'webm', 'mp3', 'ogg', 'opus'] and f.get('url') and f.get('acodec') != 'none':
                        audio_url = f['url']
                        audio_ext = f['ext']
                        content_length = f.get('filesize') # May be None
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

            # Stream the response from YouTube
            youtube_stream_response = requests.get(
                audio_url,
                headers=headers_for_youtube_request,
                stream=True, # Important for streaming large files
                allow_redirects=True,
                timeout=(30, 90) # Connection timeout, Read timeout
            )
            youtube_stream_response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

            mimetype = youtube_stream_response.headers.get('Content-Type') or f'audio/{audio_ext}' if audio_ext else 'application/octet-stream'
            actual_content_length = youtube_stream_response.headers.get('Content-Length') or content_length

            flask_response = Response(youtube_stream_response.iter_content(chunk_size=8192), mimetype=mimetype)
            flask_response.headers['Accept-Ranges'] = 'bytes'

            if actual_content_length:
                flask_response.headers['Content-Length'] = actual_content_length
            # If the client sent a Range header and YouTube responded with 206 Partial Content
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
    This is used for older browsers that don't support direct YouTube streaming.
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
        'outtmpl': audio_path, # Save to the temporary directory
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
    return Response(open(file_path, 'rb').read(), mimetype='audio/mpeg') # Or appropriate mime type

@app.route('/youtube_info')
@cache.cached(timeout=3600) # Cache YouTube video info for 1 hour
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
                "type": "youtube" # Important for client-side logic
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


@app.route('/Youtube') # User's original casing is preserved
@cache.cached(timeout=3600) # Cache YouTube search results for 1 hour
def Youtube():
    """
    Searches YouTube for videos based on a query.
    """
    query = request.args.get('query')
    if not query:
        return jsonify({"error": "Query parameter is missing."}), 400

    ydl_opts = {
        'default_search': 'ytsearch10', # Search for up to 10 results
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True, # Only extract metadata, don't recursively extract playlist items
        'force_ipv4': True,
        'geo_bypass': True,
        'logger': logging.getLogger(),
        'external_downloader_args': ['--socket-timeout', '15'] # Timeout for external downloader if used
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


@app.route('/search_hosted_mp3s') # For Netlify-hosted songs
def search_hosted_mp3s():
    """
    Searches the loaded HOSTED_SONGS_DATA manifest for MP3s matching a query.
    NOTE: This route should use the HOSTED_SONGS_DATA loaded by `hosted_songs_manifest_route`.
    """
    # Load HOSTED_SONGS_DATA from the cached manifest route
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
        # Check if query is in title or artist (case-insensitive)
        if query in song.get('title', '').lower() or query in song.get('artist', '').lower():
            filtered_songs.append(song)
    
    logging.info(f"Found {len(filtered_songs)} hosted MP3s for query '{query}'")
    return jsonify(filtered_songs)

# --- SocketIO Event Handlers ---
# Dictionaries to keep track of active jam sessions and SIDs.
# In a multi-instance Vercel environment, these local dictionaries will not be synchronized
# across instances. Firestore is the source of truth for jam session state.
# These local caches are primarily for quick lookups within the scope of a single Flask instance
# and for managing the host_sid to know which socket controls the session state.
jam_sessions = {} # {jam_id: {host_sid: '...', participants: {sid: 'nickname'}, ...}}
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

        if db: # Only proceed if Firestore is initialized
            jam_ref = db.collection('jam_sessions').document(jam_id)
            try:
                jam_doc = jam_ref.get()
                if jam_doc.exists:
                    jam_data = jam_doc.to_dict()
                    if jam_data.get('host_sid') == request.sid: # Use .get() for safety
                        # Host disconnected, mark session as ended in Firestore
                        logging.info(f"Host {nickname} ({request.sid}) for jam {jam_id} disconnected. Marking session as ended.")
                        jam_ref.update({'is_active': False, 'ended_at': firestore.SERVER_TIMESTAMP})
                        socketio.emit('session_ended', {'jam_id': jam_id, 'message': 'Host disconnected. Session ended.'}, room=jam_id)
                    else:
                        # Participant disconnected, remove from participants list
                        if request.sid in jam_data.get('participants', {}): # Use .get() for safety
                            updated_participants = {sid: name for sid, name in jam_data['participants'].items() if sid != request.sid}
                            jam_ref.update({'participants': updated_participants})
                            logging.info(f"Participant {nickname} ({request.sid}) left jam {jam_id}.")
                            # Send the entire participants map (nicknames)
                            socketio.emit('update_participants', {
                                'jam_id': jam_id,
                                'participants': updated_participants # Send the map directly
                            }, room=jam_id)
                else:
                    logging.warning(f"Disconnected client {request.sid} was in jam {jam_id}, but jam not found in Firestore.")
            except Exception as e:
                logging.error(f"Error handling disconnect for jam {jam_id} in Firestore: {e}")
        else:
            logging.warning("Firestore DB not initialized. Cannot process disconnect for jam sessions.")
        
        # Clean up local socketio tracking
        # Note: Local jam_sessions cache will eventually become stale in multi-instance environments.
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
        logging.error("Firestore DB not initialized. Cannot create jam session.")
        emit('join_failed', {'message': 'Server database not initialized. Cannot create session.'})
        return

    jam_name = data.get('jam_name', 'Unnamed Jam Session')
    nickname = data.get('nickname', 'Host')
    
    try:
        new_jam_doc_ref = db.collection('jam_sessions').document() # Firestore generates ID
        jam_id = new_jam_doc_ref.id

        initial_jam_data = {
            'name': jam_name,
            'host_sid': request.sid,
            'participants': {request.sid: nickname}, # Store SID to nickname mapping
            'playlist': [],
            'playback_state': {
                'current_track_index': 0,
                'current_playback_time': 0,
                'is_playing': False,
                'timestamp': firestore.SERVER_TIMESTAMP # Use server timestamp for sync
            },
            'created_at': firestore.SERVER_TIMESTAMP,
            'is_active': True # Mark session as active
        }
        new_jam_doc_ref.set(initial_jam_data)

        # Update local server-side tracking (not authoritative, Firestore is)
        jam_sessions[jam_id] = {
            'name': jam_name,
            'host_sid': request.sid,
            'participants': {request.sid: nickname},
            'playlist': [],
            'playback_state': {
                'current_track_index': 0,
                'current_playback_time': 0,
                'is_playing': False,
                'timestamp': 0 # Local cache doesn't need server timestamp
            }
        }
        sids_in_jams[request.sid] = {'jam_id': jam_id, 'nickname': nickname}

        join_room(jam_id)
        logging.info(f"Jam session '{jam_name}' created with ID: {jam_id} by host {nickname} ({request.sid})")

        shareable_link = f"{get_base_url()}join/{jam_id}" # Dynamic link

        emit('session_created', {
            'jam_id': jam_id,
            'jam_name': jam_name,
            'is_host': True,
            'initial_state': initial_jam_data['playback_state'],
            'participants': initial_jam_data['participants'], # Send full participants map for client-side display
            'shareable_link': shareable_link,
            'nickname_used': nickname # Send back the nickname that was used
        })

    except Exception as e:
        logging.error(f"Error creating jam session in Firestore: {e}")
        emit('join_failed', {'message': f'Error creating session: {e}'})

@socketio.on('join_session')
def join_session_handler(data): # Renamed to avoid conflict with Flask route
    if db is None:
        logging.error("Firestore DB not initialized. Cannot join jam session.")
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
        if not jam_doc.exists or not jam_doc.to_dict().get('is_active', False): # Check for 'is_active'
            logging.warning(f"Client {request.sid} attempted to join non-existent or inactive jam {jam_id}")
            emit('join_failed', {'message': 'Jam session not found or has ended.'})
            return

        jam_data = jam_doc.to_dict()
        
        # Add participant to Firestore (or update if already there)
        updated_participants = jam_data.get('participants', {})
        updated_participants[request.sid] = nickname
        db.collection('jam_sessions').document(jam_id).update({'participants': updated_participants})

        # Update local tracking (redundant if using Firestore as source of truth, but kept for consistency)
        # It's better to rely on Firestore as the single source of truth for jam_sessions data.
        # This local cache is mostly for convenience within SocketIO handlers for quick lookups.
        if jam_id not in jam_sessions:
            jam_sessions[jam_id] = {
                'name': jam_data.get('name', 'Unnamed Jam'),
                'host_sid': jam_data.get('host_sid'),
                'playlist': jam_data.get('playlist', []),
                'playback_state': jam_data.get('playback_state', {})
            }
        jam_sessions[jam_id]['participants'] = updated_participants
        sids_in_jams[request.sid] = {'jam_id': jam_id, 'nickname': nickname}

        join_room(jam_id)
        logging.info(f"Client {nickname} ({request.sid}) joined jam {jam_id}")

        # Send current state to the newly joined participant
        playback_state = jam_data.get('playback_state', {})
        emit('session_join_success', {
            'jam_id': jam_id,
            'current_track_index': playback_state.get('current_track_index', 0),
            'current_playback_time': playback_state.get('current_playback_time', 0),
            'is_playing': playback_state.get('is_playing', False),
            'playlist': jam_data.get('playlist', []),
            'jam_name': jam_data.get('name', 'Unnamed Jam'),
            'last_synced_at': playback_state.get('timestamp', firestore.SERVER_TIMESTAMP),
            'participants': updated_participants, # Send full participants map
            'nickname_used': nickname
        })

        # Notify all other participants in the room about the new participant
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
        logging.warning("Firestore DB not initialized. Cannot sync playback state.")
        return

    jam_id = data.get('jam_id')
    if not jam_id:
        logging.warning("Received sync_playback_state without jam_id.")
        return

    try:
        jam_doc = db.collection('jam_sessions').document(jam_id).get()
        if not jam_doc.exists:
            logging.warning(f"Sync request for non-existent jam {jam_id}")
            return
        
        jam_data = jam_doc.to_dict()
        if jam_data.get('host_sid') != request.sid: # Only host can sync state
            logging.warning(f"Non-host {request.sid} attempted to sync state for jam {jam_id}")
            return

        new_playback_state = {
            'current_track_index': data.get('current_track_index'),
            'current_playback_time': data.get('current_playback_time'),
            'is_playing': data.get('is_playing'),
            'timestamp': firestore.SERVER_TIMESTAMP # Always update with server timestamp
        }
        
        # Host sends the full playlist with every sync for robustness
        db.collection('jam_sessions').document(jam_id).update({
            'playback_state': new_playback_state,
            'playlist': data.get('playlist', []) # Host sends full playlist
        })
        logging.info(f"Host {request.sid} synced playback state for jam {jam_id}.")

    except Exception as e:
        logging.error(f"Error syncing playback state for jam {jam_id} to Firestore: {e}")

@socketio.on('add_song_to_jam')
def add_song_to_jam(data):
    """
    Adds a song to the jam's playlist.
    Song object now includes 'type' (e.g., 'audio' or 'youtube') and appropriate 'url' or 'videoId'.
    """
    if db is None:
        logging.warning("Firestore DB not initialized. Cannot add song to jam.")
        return

    jam_id = data.get('jam_id')
    song = data.get('song') # Song object from client

    # Assign a unique ID to the song if it doesn't have one (for hosted MP3s etc.)
    if 'id' not in song or song['id'] is None:
        song['id'] = str(uuid.uuid4())

    # Validate song type and URL/videoId
    if not jam_id or not song or not song.get('type'):
        logging.warning(f"Invalid add_song_to_jam request from {request.sid} for jam {jam_id}: Missing jam_id, song, or song type.")
        return
    if song['type'] == 'youtube' and not song.get('videoId'):
        logging.warning(f"Invalid YouTube song: Missing videoId for jam {jam_id}, song: {song}")
        return
    if (song['type'] == 'audio' or song['type'] == 'youtube_download') and not song.get('url'):
        logging.warning(f"Invalid audio/youtube_download song: Missing URL for jam {jam_id}, song: {song}")
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
        logging.info(f"Song '{song.get('title', 'Unknown')}' (Type: {song.get('type')}) added to jam {jam_id} by host {request.sid} via Firestore.")

    except Exception as e:
        logging.error(f"Error adding song to jam {jam_id} in Firestore: {e}")

@socketio.on('remove_song_from_jam')
def remove_song_from_jam(data):
    if db is None:
        logging.warning("Firestore DB not initialized. Cannot remove song from jam.")
        return

    jam_id = data.get('jam_id')
    song_id_to_remove = data.get('song_id') # Now removing by unique song ID

    if not jam_id or not song_id_to_remove:
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
        
        # Find index by song_id
        index_to_remove = -1
        for i, song in enumerate(current_playlist):
            if song.get('id') == song_id_to_remove:
                index_to_remove = i
                break

        if index_to_remove != -1:
            removed_song = current_playlist.pop(index_to_remove)
            logging.info(f"Song '{removed_song.get('title', 'Unknown')}' removed from jam {jam_id} by host {request.sid} via Firestore.")

            # Adjust current_track_index if the removed song affects it
            current_track_index = jam_data['playback_state'].get('current_track_index', 0)
            if current_track_index == index_to_remove:
                if not current_playlist:
                    current_track_index = 0
                elif index_to_remove >= len(current_playlist):
                    current_track_index = 0 # If last song was removed, go to beginning or 0
            elif current_track_index > index_to_remove:
                current_track_index -= 1
            
            # Update Firestore
            jam_ref.update({
                'playlist': current_playlist,
                'playback_state.current_track_index': current_track_index,
                'playback_state.current_playback_time': 0, # Reset time for new current track
                'playback_state.is_playing': jam_data['playback_state'].get('is_playing', False) and len(current_playlist) > 0 # Keep playing if playlist not empty
            })

    except Exception as e:
        logging.error(f"Error removing song from jam {jam_id} in Firestore: {e}")


# When running on Vercel, the application is served by a WSGI server (e.g., Gunicorn),
# which handles starting the Flask app. The 'socketio.run(app, ...)' call is only for
# local development with Flask's built-in server.
if __name__ == '__main__':
    if db is None:
        logging.error("Firestore database is not initialized. Jam Session feature will not work.")
    if firebase_auth is None:
        logging.error("Firebase Admin SDK Auth is not initialized. Authentication features will not work.")
    
    # This line is for local development only. Vercel will run `app` directly.
    socketio.run(app, debug=True, port=5000)


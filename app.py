# app.py
from flask import Flask, request, jsonify, render_template, redirect, url_for, session
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
import requests
import os
import firebase_admin
from google.cloud import firestore # Keep this as 'firestore' for Timestamp access
from firebase_admin import credentials, firestore as admin_firestore, auth as firebase_auth # Alias firebase_admin.firestore
from firebase_admin import exceptions as firebase_exceptions
import time # For tracking last_seen in active users
from functools import wraps # For decorators
import secrets # For generating secure random strings
import string # For character sets for random strings
import hashlib # For hashing passwords
import traceback # Import traceback for detailed error logging
import uuid # <--- ADDED THIS IMPORT FOR uuid.uuid4()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', '9dbd7f596fcc558bdc9cf3a4d153dc4243d3bff01e66f14f69f68edb7f6fed17') # IMPORTANT: CHANGE THIS IN PRODUCTION!
CORS(app, resources={r"/*": {"origins": "http://localhost:3000"}}, supports_credentials=True)
socketio = SocketIO(app, cors_allowed_origins="*", manage_session=False) # manage_session=False as Flask handles sessions

# --- Firebase Admin SDK Initialization ---
# IMPORTANT: Replace 'serviceAccountKey.json' with the actual path to your downloaded JSON key.
# For example: 'path/to/your/downloaded-firebase-adminsdk-xxxxx-firebase-adminsdk-xxxxx.json'
# It's best practice to keep this file out of version control and load its path from an environment variable in production.
FIREBASE_SERVICE_ACCOUNT_KEY_FILE = os.getenv('FIREBASE_SERVICE_ACCOUNT_KEY_PATH', 'serviceAccountKey.json')
APP_ID = "tunejam_app" # This should match the document ID in 'artifacts' collection

db = None # Initialize db to None

try:
    if not os.path.exists(FIREBASE_SERVICE_ACCOUNT_KEY_FILE):
        raise FileNotFoundError(f"Firebase service account key not found at: {FIREBASE_SERVICE_ACCOUNT_KEY_FILE}")

    if not firebase_admin._apps:
        cred = credentials.Certificate(FIREBASE_SERVICE_ACCOUNT_KEY_FILE)
        firebase_admin.initialize_app(cred)
        print("Firebase Admin SDK initialized successfully.")

    # Get the Firestore client AFTER initialization
    db = admin_firestore.client()
    print("Firestore client initialized successfully.")

except FileNotFoundError as e:
    print(f"Error initializing Firebase Admin SDK: {e}")
    print(f"Please ensure the service account key path '{FIREBASE_SERVICE_ACCOUNT_KEY_FILE}' is correct and the file exists.")
    exit(1) # Exit if the key file is not found, as the app won't function without Firebase.
except Exception as e:
    print(f"An unexpected error occurred during Firebase Admin SDK initialization: {e}")
    traceback.print_exc() # Print full traceback for debugging
    exit(1) # Exit on other initialization errors


# --- Helper functions ---
def generate_unique_jam_code(length=6):
    """Generates a unique 6-character alphanumeric jam code."""
    characters = string.ascii_letters + string.digits
    while True:
        code = ''.join(secrets.choice(characters) for _ in range(length))
        # Check if code already exists in Firestore
        jam_ref = db.collection('artifacts').document(APP_ID).collection('public_jams').document(code)
        if not jam_ref.get().exists:
            return code

def hash_password(password):
    """Hashes a password using SHA256."""
    return hashlib.sha256(password.encode()).hexdigest()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({"error": "Unauthorized: No user session."}), 401
        return f(*args, **kwargs)
    return decorated_function

# --- Routes ---
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/register', methods=['POST'])
def register():
    data = request.get_json()
    id_token = data.get('id_token')
    user_name = data.get('name') # Get name from frontend

    if not id_token:
        return jsonify({"error": "ID token is required."}), 400

    try:
        decoded_token = firebase_auth.verify_id_token(id_token)
        user_id = decoded_token['uid']
        email = decoded_token.get('email')

        # Create or update a user document in Firestore
        db.collection('users').document(user_id).set({
            'email': email,
            'created_at': firestore.SERVER_TIMESTAMP,
            'name': user_name or email.split('@')[0] # Use provided name or default to part of email
        })

        return jsonify({"message": f"User {user_id} registered successfully."}), 201

    except firebase_exceptions.FirebaseError as e:
        print(f"Firebase registration error: {e}")
        return jsonify({"error": "Registration failed."}), 400
    except Exception as e:
        print(f"Unexpected registration error: {e}")
        traceback.print_exc()
        return jsonify({"error": "Internal server error."}), 500
    
@app.route('/login', methods=['GET', 'POST'])
def login():
    data = request.get_json()
    id_token = data.get('id_token')

    if not id_token:
        return jsonify({"error": "ID token is required."}), 400

    try:
        decoded_token = firebase_auth.verify_id_token(id_token)
        user_id = decoded_token['uid']
        # Set user_id in Flask session
        session['user_id'] = user_id
        session['logged_in'] = True

        # Update last_seen in Firestore for the user
        user_ref = db.collection('users').document(user_id)
        user_ref.update({'last_seen': firestore.SERVER_TIMESTAMP})

        return jsonify({"message": "Logged in successfully.", "user_id": user_id}), 200

    except firebase_exceptions.FirebaseError as e:
        print(f"Firebase login error: {e}")
        return jsonify({"error": "Login failed."}), 401
    except Exception as e:
        print(f"Unexpected login error: {e}")
        traceback.print_exc()
        return jsonify({"error": "Internal server error."}), 500

@app.route('/logout', methods=['POST'])
@login_required
def logout():
    session.pop('user_id', None)
    session.pop('logged_in', None)
    return jsonify({"message": "Logged out successfully."}), 200

@app.route('/me', methods=['GET'])
@login_required
def get_current_user_profile():
    user_id = session.get('user_id')
    try:
        user_doc = db.collection('users').document(user_id).get()
        if user_doc.exists:
            user_data = user_doc.to_dict()
            # Return the name for display
            return jsonify({
                "user_id": user_id,
                "name": user_data.get("name", user_data.get("email", "Unknown"))
            }), 200
        else:
            session.pop('user_id', None) # Clear session if user not found
            return jsonify({"error": "User profile not found. Please log in again."}), 404
    except Exception as e:
        print(f"Error fetching current user profile: {e}")
        traceback.print_exc()
        return jsonify({"error": "Failed to fetch user profile."}), 500

# --- Jam Management Routes ---
@app.route('/jams', methods=['POST']) # <--- ROUTE CHANGED FROM /create_jam TO /jams
@login_required
def create_jam():
    user_id = session.get('user_id')
    data = request.get_json()
    name = data.get('name')
    is_private = data.get('is_private', False)
    password = data.get('password') if is_private else None

    if not name:
        return jsonify({"error": "Jam name is required."}), 400

    # Ensure password is provided for private jams
    if is_private and not password:
        return jsonify({"error": "Password is required for private jams."}), 400

    try:
        # Use UUID for a unique jam ID, then truncate for a shorter code
        # jam_code = str(uuid.uuid4())[:8] # Example using uuid (ensure import uuid)
        jam_code = generate_unique_jam_code() # Using helper function for 6-char code

        password_hash = hash_password(password) if password else None

        jam_data = {
            'name': name,
            'host_id': user_id,
            'members': [user_id], # Host is automatically a member
            'is_private': is_private,
            'password_hash': password_hash,
            'created_at': firestore.SERVER_TIMESTAMP,
            'status': 'active', # You might have 'active', 'ended', etc.
            'current_song': None, # No song playing initially
            'current_song_state': 'stopped', # 'playing', 'paused', 'stopped'
            'current_song_time': 0,
            'queue': [],
            'join_requests': [] # For private jams
        }

        # Save the jam in the correct collection path
        db.collection('artifacts').document(APP_ID).collection('public_jams').document(jam_code).set(jam_data)

        # Update user's active_jam_id
        db.collection('users').document(user_id).update({'active_jam_id': jam_code})

        print(f"Jam '{name}' created by {user_id} with code {jam_code}. Private: {is_private}")
        return jsonify({"message": "Jam created successfully!", "jam_code": jam_code}), 201

    except Exception as e:
        print(f"Error creating jam: {e}")
        traceback.print_exc()
        return jsonify({"error": f"Failed to create jam: {e}"}), 500

@app.route('/jams/<jam_id>', methods=['GET'])
@login_required
def get_jam_details(jam_id):
    user_id = session.get('user_id')
    try:
        jam_ref = db.collection('artifacts').document(APP_ID).collection('public_jams').document(jam_id)
        jam_doc = jam_ref.get()

        if not jam_doc.exists:
            return jsonify({"error": "Jam not found."}), 404

        jam_data = jam_doc.to_dict()
        if user_id not in jam_data.get('members', []):
            return jsonify({"error": "Unauthorized to view this jam."}), 403

        return jsonify(jam_data), 200

    except Exception as e:
        print(f"Error fetching jam details: {e}")
        traceback.print_exc()
        return jsonify({"error": "Failed to fetch jam details."}), 500

@app.route('/jams/user_jams', methods=['GET'])
@login_required
def get_user_jams():
    user_id = session.get('user_id')
    try:
        # Get jams where the user is the host or a member
        user_hosted_jams = db.collection('artifacts').document(APP_ID).collection('public_jams').where('host_id', '==', user_id).stream()
        user_member_jams = db.collection('artifacts').document(APP_ID).collection('public_jams').where('members', 'array_contains', user_id).stream()

        jams = {}
        for jam_doc in user_hosted_jams:
            jams[jam_doc.id] = jam_doc.to_dict()
        for jam_doc in user_member_jams:
            jams[jam_doc.id] = jam_doc.to_dict() # Will overwrite if already added from hosted jams

        # Fetch host names for each jam
        for jam_id, jam_data in jams.items():
            host_id = jam_data.get('host_id')
            if host_id:
                host_doc = db.collection('users').document(host_id).get()
                if host_doc.exists:
                    host_data = host_doc.to_dict()
                    jam_data['host_name'] = host_data.get('name', host_data.get('email', 'Unknown Host'))
                else:
                    jam_data['host_name'] = 'Unknown Host'
            jams[jam_id] = jam_data # Update the dict with host name

        return jsonify([{'jam_code': jam_id, **data} for jam_id, data in jams.items()]), 200

    except Exception as e:
        print(f"Error fetching user jams: {e}")
        traceback.print_exc()
        return jsonify({"error": "Failed to fetch user jams."}), 500

@app.route('/public_jams', methods=['GET'])
@login_required
def get_public_jams():
    user_id = session.get('user_id')
    try:
        public_jams_query = db.collection('artifacts').document(APP_ID).collection('public_jams').where('is_private', '==', False).stream()
        public_jams = []
        for jam_doc in public_jams_query:
            jam_data = jam_doc.to_dict()
            if user_id not in jam_data.get('members', []): # Don't show jams user is already a member of
                # Fetch host name
                host_id = jam_data.get('host_id')
                if host_id:
                    host_doc = db.collection('users').document(host_id).get()
                    if host_doc.exists:
                        host_data = host_doc.to_dict()
                        jam_data['host_name'] = host_data.get('name', host_data.get('email', 'Unknown Host'))
                    else:
                        jam_data['host_name'] = 'Unknown Host'
                public_jams.append({'jam_code': jam_doc.id, **jam_data})

        return jsonify(public_jams), 200

    except Exception as e:
        print(f"Error fetching public jams: {e}")
        traceback.print_exc()
        return jsonify({"error": "Failed to fetch public jams."}), 500

@app.route('/jams/join', methods=['POST'])
@login_required
def join_jam():
    user_id = session.get('user_id')
    data = request.get_json()
    jam_id = data.get('jam_id')
    password = data.get('password')

    if not jam_id:
        return jsonify({"error": "Jam ID is required."}), 400

    try:
        jam_ref = db.collection('artifacts').document(APP_ID).collection('public_jams').document(jam_id)
        jam_doc = jam_ref.get()

        if not jam_doc.exists:
            return jsonify({"error": "Jam not found."}), 404

        jam_data = jam_doc.to_dict()

        if user_id in jam_data.get('members', []):
            # User is already a member, just set active jam
            db.collection('users').document(user_id).update({'active_jam_id': jam_id})
            return jsonify({"message": "Already a member, active jam set.", "jam_code": jam_id}), 200

        if jam_data.get('is_private'):
            # Handle private jam
            if password:
                # Check password
                if hash_password(password) == jam_data.get('password_hash'):
                    # Password matches, add user to members
                    jam_ref.update({'members': admin_firestore.ArrayUnion([user_id])})
                    db.collection('users').document(user_id).update({'active_jam_id': jam_id})
                    emit('jam_member_update', {'jam_id': jam_id, 'user_id': user_id, 'action': 'joined'}, room=jam_id)
                    print(f"User {user_id} joined private jam {jam_id}")
                    return jsonify({"message": "Successfully joined private jam.", "jam_code": jam_id}), 200
                else:
                    return jsonify({"error": "Incorrect password."}), 401
            else:
                # No password provided for private jam, send join request
                if user_id not in jam_data.get('join_requests', []):
                    jam_ref.update({'join_requests': admin_firestore.ArrayUnion([user_id])})
                    host_id = jam_data.get('host_id')
                    # Notify host about new request
                    if host_id:
                        emit('new_join_request', {'jam_id': jam_id, 'requester_id': user_id}, room=host_id) # Need to implement host-specific room/socket
                    print(f"User {user_id} sent join request to private jam {jam_id}")
                return jsonify({"message": "Join request sent. Waiting for host approval."}), 202
        else:
            # Public jam, add user to members
            jam_ref.update({'members': admin_firestore.ArrayUnion([user_id])})
            db.collection('users').document(user_id).update({'active_jam_id': jam_id})
            emit('jam_member_update', {'jam_id': jam_id, 'user_id': user_id, 'action': 'joined'}, room=jam_id)
            print(f"User {user_id} joined public jam {jam_id}")
            return jsonify({"message": "Successfully joined public jam.", "jam_code": jam_id}), 200

    except Exception as e:
        print(f"Error joining jam: {e}")
        traceback.print_exc()
        return jsonify({"error": "Failed to send join request or join jam. Please try again."}), 500

@app.route('/jams/leave', methods=['POST'])
@login_required
def leave_jam():
    user_id = session.get('user_id')
    data = request.get_json()
    jam_id = data.get('jam_id')

    if not jam_id:
        return jsonify({"error": "Jam ID is required."}), 400

    try:
        jam_ref = db.collection('artifacts').document(APP_ID).collection('public_jams').document(jam_id)
        jam_doc = jam_ref.get()

        if not jam_doc.exists:
            return jsonify({"error": "Jam not found."}), 404

        jam_data = jam_doc.to_dict()

        if user_id in jam_data.get('members', []):
            jam_ref.update({'members': admin_firestore.ArrayRemove([user_id])})
            # Also clear active jam if it's the one being left
            user_doc_ref = db.collection('users').document(user_id)
            user_doc = user_doc_ref.get()
            if user_doc.exists and user_doc.to_dict().get('active_jam_id') == jam_id:
                user_doc_ref.update({'active_jam_id': firestore.DELETE_FIELD})
            emit('jam_member_update', {'jam_id': jam_id, 'user_id': user_id, 'action': 'left'}, room=jam_id)
            print(f"User {user_id} left jam {jam_id}")
            return jsonify({"message": "Successfully left jam."}), 200
        else:
            return jsonify({"error": "Not a member of this jam."}), 400

    except Exception as e:
        print(f"Error leaving jam: {e}")
        traceback.print_exc()
        return jsonify({"error": "Failed to leave jam."}), 500

@app.route('/get_user_id')
@login_required # Ensure only logged-in users can get their ID
def get_user_id():
    # user_id is guaranteed to be in session if @login_required passes
    user_id = session.get('user_id')
    if user_id:
        return jsonify({'user_id': user_id}), 200
    else:
        # This case ideally shouldn't be hit if @login_required works
        return jsonify({'error': 'User not logged in or ID not found'}), 401

# ... (rest of your app.py code)

@app.route('/jams/pending_requests', methods=['GET'])
@login_required
def get_pending_requests():
    user_id = session.get('user_id')
    try:
        # Get jams where the current user is the host and there are join requests
        hosted_jams_with_requests = db.collection('artifacts').document(APP_ID).collection('public_jams') \
            .where('host_id', '==', user_id) \
            .where('join_requests', '!=', []) \
            .stream()

        pending_requests_data = []
        for jam_doc in hosted_jams_with_requests:
            jam_data = jam_doc.to_dict()
            jam_code = jam_doc.id
            for requester_id in jam_data.get('join_requests', []):
                requester_doc = db.collection('users').document(requester_id).get()
                if requester_doc.exists:
                    requester_data = requester_doc.to_dict()
                    pending_requests_data.append({
                        'jam_code': jam_code,
                        'jam_name': jam_data.get('name'),
                        'requester_id': requester_id,
                        'requester_name': requester_data.get('name', requester_data.get('email', 'Unknown User'))
                    })
        return jsonify(pending_requests_data), 200
    except Exception as e:
        print(f"Error fetching pending requests: {e}")
        traceback.print_exc()
        return jsonify({"error": "Failed to fetch pending requests."}), 500

@app.route('/jams/approve_request', methods=['POST'])
@login_required
def approve_join_request():
    user_id = session.get('user_id')
    data = request.get_json()
    jam_id = data.get('jam_id')
    requester_id = data.get('requester_id')

    if not all([jam_id, requester_id]):
        return jsonify({"error": "Jam ID and Requester ID are required."}), 400

    try:
        jam_ref = db.collection('artifacts').document(APP_ID).collection('public_jams').document(jam_id)
        jam_doc = jam_ref.get()

        if not jam_doc.exists:
            return jsonify({"error": "Jam not found."}), 404

        jam_data = jam_doc.to_dict()

        if jam_data.get('host_id') != user_id:
            return jsonify({"error": "Only the host can approve join requests."}), 403

        if requester_id in jam_data.get('join_requests', []):
            # Remove from requests and add to members
            jam_ref.update({
                'join_requests': admin_firestore.ArrayRemove([requester_id]),
                'members': admin_firestore.ArrayUnion([requester_id])
            })
            # Set requester's active_jam_id
            db.collection('users').document(requester_id).update({'active_jam_id': jam_id})
            emit('join_request_approved', {'jam_id': jam_id}, room=requester_id) # Notify requester
            emit('jam_member_update', {'jam_id': jam_id, 'user_id': requester_id, 'action': 'joined'}, room=jam_id) # Notify jam members
            print(f"Join request from {requester_id} for jam {jam_id} approved by host {user_id}")
            return jsonify({"message": "Join request approved successfully."}), 200
        else:
            return jsonify({"error": "Requester not found in pending requests."}), 400

    except Exception as e:
        print(f"Error approving join request: {e}")
        traceback.print_exc()
        return jsonify({"error": "Failed to approve join request."}), 500

@app.route('/jams/deny_request', methods=['POST'])
@login_required
def deny_join_request():
    user_id = session.get('user_id')
    data = request.get_json()
    jam_id = data.get('jam_id')
    requester_id = data.get('requester_id')

    if not all([jam_id, requester_id]):
        return jsonify({"error": "Jam ID and Requester ID are required."}), 400

    try:
        jam_ref = db.collection('artifacts').document(APP_ID).collection('public_jams').document(jam_id)
        jam_doc = jam_ref.get()

        if not jam_doc.exists:
            return jsonify({"error": "Jam not found."}), 404

        jam_data = jam_doc.to_dict()

        if jam_data.get('host_id') != user_id:
            return jsonify({"error": "Only the host can deny join requests."}), 403

        if requester_id in jam_data.get('join_requests', []):
            jam_ref.update({'join_requests': admin_firestore.ArrayRemove([requester_id])})
            emit('join_request_denied', {'jam_id': jam_id}, room=requester_id) # Notify requester
            print(f"Join request from {requester_id} for jam {jam_id} denied by host {user_id}")
            return jsonify({"message": "Join request denied successfully."}), 200
        else:
            return jsonify({"error": "Requester not found in pending requests."}), 400

    except Exception as e:
        print(f"Error denying join request: {e}")
        traceback.print_exc()
        return jsonify({"error": "Failed to deny join request."}), 500


@app.route('/jams/user_active_jam', methods=['GET']) # <--- ROUTE CHANGED FROM /jams/user_active_jam/<uid>
@login_required
def get_active_jam():
    user_id = session.get('user_id')
    try:
        user_doc = db.collection('users').document(user_id).get()
        if not user_doc.exists:
            return jsonify({"error": "User not found."}), 404

        active_jam_id = user_doc.to_dict().get('active_jam_id')

        if active_jam_id:
            jam_doc = db.collection('artifacts').document(APP_ID).collection('public_jams').document(active_jam_id).get()
            if jam_doc.exists:
                jam_data = jam_doc.to_dict()
                if user_id in jam_data.get('members', []): # Ensure user is still a member
                    return jsonify({"active_jam_id": active_jam_id, "jam_name": jam_data.get('name')}), 200
                else:
                    # Clear active_jam_id if user is no longer a member
                    db.collection('users').document(user_id).update({'active_jam_id': firestore.DELETE_FIELD})
                    return jsonify({"active_jam_id": None, "message": "You are no longer a member of that jam."}), 200
            else:
                # Clear active_jam_id if jam no longer exists
                db.collection('users').document(user_id).update({'active_jam_id': firestore.DELETE_FIELD})
                return jsonify({"active_jam_id": None, "message": "Active jam not found or no longer exists."}), 200
        else:
            return jsonify({"active_jam_id": None, "message": "No active jam."}), 200
    except Exception as e:
        print(f"Error fetching active jam: {e}")
        traceback.print_exc()
        return jsonify({"error": "Failed to fetch active jam."}), 500


@app.route('/get_jam_state', methods=['GET'])
@login_required
def get_jam_state():
    user_id = session.get('user_id')
    jam_id = request.args.get('jam_id') # Get jam_id from query parameter

    if not jam_id:
        return jsonify({"error": "Jam ID is required."}), 400

    try:
        jam_ref = db.collection('artifacts').document(APP_ID).collection('public_jams').document(jam_id)
        jam_doc = jam_ref.get()

        if not jam_doc.exists:
            return jsonify({"error": "Jam not found."}), 404

        jam_data = jam_doc.to_dict()

        if user_id not in jam_data.get('members', []):
            return jsonify({"error": "Unauthorized to access this jam's state."}), 403

        # Return only the relevant state for the frontend
        return jsonify({
            'current_song': jam_data.get('current_song'),
            'current_song_state': jam_data.get('current_song_state'),
            'current_song_time': jam_data.get('current_song_time'),
            'queue': jam_data.get('queue', []),
            'host_id': jam_data.get('host_id'),
            'members': jam_data.get('members', [])
        }), 200

    except Exception as e:
        print(f"Error fetching jam state for {jam_id}: {e}")
        traceback.print_exc()
        return jsonify({"error": "Failed to fetch jam state."}), 500
YOUTUBE_API_KEY = 'AIzaSyCkRrCzqelHrxOBUIv85am3LkRynyxETk8'

@app.route('/search_youtube')
def search_youtube():
    query = request.args.get('q')
    if not query:
        return jsonify({"error": "Missing search query"}), 400

    try:
        url = f'https://www.googleapis.com/youtube/v3/search?part=snippet&type=video&maxResults=5&q={query}&key={YOUTUBE_API_KEY}'
        response = requests.get(url)
        data = response.json()

        results = []
        for item in data.get('items', []):
            video = {
                'videoId': item['id']['videoId'],
                'title': item['snippet']['title'],
                'channel': item['snippet']['channelTitle'],
                'thumbnail': item['snippet']['thumbnails']['default']['url']
            }
            results.append(video)

        return jsonify({"results": results})

    except Exception as e:
        print(f"Search error: {e}")
        return jsonify({"error": "Failed to fetch YouTube results"}), 500

# --- HTML serving routes ---
@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')

@app.route('/login_page')
def login_page():
    return render_template('login.html')

@app.route('/register_page')
def register_page():
    return render_template('register.html')

@app.route('/jam')
@login_required
def jam_page():
    return render_template('jam.html')

# --- SocketIO Events ---
@socketio.on('connect')
def connect():
    user_id = session.get('user_id')
    if user_id:
        join_room(user_id) # Join a room specific to the user ID
        print(f"User {user_id} connected via SocketIO.")
        # Update user's last_seen
        db.collection('users').document(user_id).update({'last_seen': firestore.SERVER_TIMESTAMP})
        # If user is in an active jam, have them join the jam's room
        user_doc = db.collection('users').document(user_id).get()
        if user_doc.exists:
            active_jam_id = user_doc.to_dict().get('active_jam_id')
            if active_jam_id:
                join_room(active_jam_id)
                print(f"User {user_id} joined jam room {active_jam_id}")
    else:
        print("Unauthenticated user connected via SocketIO.")
        emit('error', {'message': 'Unauthorized: Please log in.'})
        return False # Disallow connection if not authenticated

@socketio.on('disconnect')
def disconnect():
    user_id = session.get('user_id')
    if user_id:
        print(f"User {user_id} disconnected.")
        # Update last_seen or mark as offline
        db.collection('users').document(user_id).update({'last_seen': firestore.SERVER_TIMESTAMP}) # Or a separate 'is_online' field
    else:
        print("Unauthenticated client disconnected.")

@socketio.on('join_jam_room')
@login_required
def handle_join_jam_room(data):
    user_id = session.get('user_id')
    jam_id = data.get('jam_id')
    if not jam_id:
        emit('error', {'message': 'Jam ID is required to join room.'}, room=request.sid)
        return

    try:
        jam_doc = db.collection('artifacts').document(APP_ID).collection('public_jams').document(jam_id).get()
        if not jam_doc.exists or user_id not in jam_doc.to_dict().get('members', []):
            emit('error', {'message': 'Unauthorized to join this jam room.'}, room=request.sid)
            return

        join_room(jam_id)
        print(f"User {user_id} joined SocketIO room for jam {jam_id}")
        emit('room_joined', {'jam_id': jam_id, 'message': 'Successfully joined jam room.'}, room=request.sid)

    except Exception as e:
        print(f"Error joining jam room: {e}")
        traceback.print_exc()
        emit('error', {'message': 'Failed to join jam room.'}, room=request.sid)

@socketio.on('leave_jam_room')
@login_required
def handle_leave_jam_room(data):
    user_id = session.get('user_id')
    jam_id = data.get('jam_id')
    if not jam_id:
        emit('error', {'message': 'Jam ID is required to leave room.'}, room=request.sid)
        return

    try:
        leave_room(jam_id)
        print(f"User {user_id} left SocketIO room for jam {jam_id}")
        emit('room_left', {'jam_id': jam_id, 'message': 'Successfully left jam room.'}, room=request.sid)
    except Exception as e:
        print(f"Error leaving jam room: {e}")
        traceback.print_exc()
        emit('error', {'message': 'Failed to leave jam room.'}, room=request.sid)

@socketio.on('add_song_to_queue')
@login_required
def add_song_to_queue(data):
    user_id = session.get('user_id')
    jam_id = data.get('jam_id')
    song = data.get('song') # Expects {id, title, channel, thumbnail}

    if not all([jam_id, song, song.get('id'), song.get('title')]):
        emit('error', {'message': 'Invalid song data or jam ID.'}, room=request.sid)
        return

    try:
        jam_ref = db.collection('artifacts').document(APP_ID).collection('public_jams').document(jam_id)
        jam_doc = jam_ref.get()

        if not jam_doc.exists or user_id not in jam_doc.to_dict().get('members', []):
            emit('error', {'message': 'Unauthorized to add song to this jam.'}, room=request.sid)
            return

        # Add song to queue and update last_updated timestamp
        jam_ref.update({
            'queue': admin_firestore.ArrayUnion([song]),
            'last_updated': firestore.SERVER_TIMESTAMP
        })
        emit('jam_queue_update', {'jam_id': jam_id, 'queue': jam_doc.to_dict().get('queue', []) + [song]}, room=jam_id)
        print(f"Song '{song.get('title')}' added to queue for jam {jam_id} by {user_id}")
    except Exception as e:
        print(f"Error adding song to queue for jam {jam_id}: {e}")
        traceback.print_exc()
        emit('error', {'message': f'Failed to add song to queue: {e}'}, room=request.sid)

@socketio.on('play_song')
@login_required
def play_song(data):
    user_id = session.get('user_id')
    jam_id = data.get('jam_id')
    song_id = data.get('song_id') # YouTube video ID

    if not all([jam_id, song_id]):
        emit('error', {'message': 'Jam ID and song ID are required.'}, room=request.sid)
        return

    try:
        jam_ref = db.collection('artifacts').document(APP_ID).collection('public_jams').document(jam_id)
        jam_doc = jam_ref.get()

        if not jam_doc.exists:
            emit('error', {'message': 'Jam not found.'}, room=request.sid)
            return

        jam_data = jam_doc.to_dict()

        # Only host can play/control songs
        if jam_data.get('host_id') != user_id:
            emit('error', {'message': 'Only the host can control song playback.'}, room=request.sid)
            return

        # Find the song in the queue
        song_to_play = next((s for s in jam_data.get('queue', []) if s.get('id') == song_id), None)

        if song_to_play:
            # Set current song and state
            jam_ref.update({
                'current_song': song_to_play,
                'current_song_state': 'playing',
                'current_song_time': 0, # Start from beginning
                'last_updated': firestore.SERVER_TIMESTAMP
            })
            emit('jam_state_update', {
                'current_song': song_to_play,
                'current_song_state': 'playing',
                'current_song_time': 0,
                'updater_id': user_id # Let frontend know who initiated the update
            }, room=jam_id, include_self=True)
            print(f"Jam {jam_id}: Host {user_id} playing song: {song_to_play.get('title')}")
        else:
            emit('error', {'message': 'Song not found in queue.'}, room=request.sid)
            return

    except Exception as e:
        print(f"Error playing song for jam {jam_id}: {e}")
        traceback.print_exc()
        emit('error', {'message': f'Failed to play song: {e}'}, room=request.sid)

@socketio.on('pause_song')
@login_required
def pause_song(data):
    user_id = session.get('user_id')
    jam_id = data.get('jam_id')
    current_time = data.get('current_time', 0) # Current playback time from client

    if not jam_id:
        emit('error', {'message': 'Jam ID is required.'}, room=request.sid)
        return

    try:
        jam_ref = db.collection('artifacts').document(APP_ID).collection('public_jams').document(jam_id)
        jam_doc = jam_ref.get()

        if not jam_doc.exists:
            emit('error', {'message': 'Jam not found.'}, room=request.sid)
            return

        jam_data = jam_doc.to_dict()
        if jam_data.get('host_id') != user_id:
            emit('error', {'message': 'Only the host can control song playback.'}, room=request.sid)
            return

        if jam_data.get('current_song'):
            jam_ref.update({
                'current_song_state': 'paused',
                'current_song_time': current_time,
                'last_updated': firestore.SERVER_TIMESTAMP
            })
            emit('jam_state_update', {
                'current_song_state': 'paused',
                'current_song_time': current_time,
                'updater_id': user_id
            }, room=jam_id, include_self=True)
            print(f"Jam {jam_id}: Host {user_id} paused song at {current_time}s")
        else:
            emit('error', {'message': 'No song currently playing to pause.'}, room=request.sid)

    except Exception as e:
        print(f"Error pausing song for jam {jam_id}: {e}")
        traceback.print_exc()
        emit('error', {'message': f'Failed to pause song: {e}'}, room=request.sid)

@socketio.on('resume_song')
@login_required
def resume_song(data):
    user_id = session.get('user_id')
    jam_id = data.get('jam_id')

    if not jam_id:
        emit('error', {'message': 'Jam ID is required.'}, room=request.sid)
        return

    try:
        jam_ref = db.collection('artifacts').document(APP_ID).collection('public_jams').document(jam_id)
        jam_doc = jam_ref.get()

        if not jam_doc.exists:
            emit('error', {'message': 'Jam not found.'}, room=request.sid)
            return

        jam_data = jam_doc.to_dict()
        if jam_data.get('host_id') != user_id:
            emit('error', {'message': 'Only the host can control song playback.'}, room=request.sid)
            return

        if jam_data.get('current_song') and jam_data.get('current_song_state') == 'paused':
            jam_ref.update({
                'current_song_state': 'playing',
                'last_updated': firestore.SERVER_TIMESTAMP
            })
            emit('jam_state_update', {
                'current_song_state': 'playing',
                'updater_id': user_id
            }, room=jam_id, include_self=True)
            print(f"Jam {jam_id}: Host {user_id} resumed song.")
        else:
            emit('error', {'message': 'No paused song to resume.'}, room=request.sid)

    except Exception as e:
        print(f"Error resuming song for jam {jam_id}: {e}")
        traceback.print_exc()
        emit('error', {'message': f'Failed to resume song: {e}'}, room=request.sid)

@socketio.on('seek_song')
@login_required
def seek_song(data):
    user_id = session.get('user_id')
    jam_id = data.get('jam_id')
    seek_time = data.get('seek_time', 0)

    if not jam_id:
        emit('error', {'message': 'Jam ID is required.'}, room=request.sid)
        return

    try:
        jam_ref = db.collection('artifacts').document(APP_ID).collection('public_jams').document(jam_id)
        jam_doc = jam_ref.get()

        if not jam_doc.exists:
            emit('error', {'message': 'Jam not found.'}, room=request.sid)
            return

        jam_data = jam_doc.to_dict()
        if jam_data.get('host_id') != user_id:
            emit('error', {'message': 'Only the host can control song playback.'}, room=request.sid)
            return

        if jam_data.get('current_song'):
            jam_ref.update({
                'current_song_time': seek_time,
                'last_updated': firestore.SERVER_TIMESTAMP
            })
            emit('jam_state_update', {
                'current_song_time': seek_time,
                'updater_id': user_id
            }, room=jam_id, include_self=True)
            print(f"Jam {jam_id}: Host {user_id} seeked song to {seek_time}s")
        else:
            emit('error', {'message': 'No song playing to seek.'}, room=request.sid)

    except Exception as e:
        print(f"Error seeking song for jam {jam_id}: {e}")
        traceback.print_exc()
        emit('error', {'message': f'Failed to seek song: {e}'}, room=request.sid)

@socketio.on('next_song')
@login_required
def next_song(data):
    user_id = session.get('user_id')
    jam_id = data.get('jam_id')

    if not jam_id:
        emit('error', {'message': 'Jam ID is required.'}, room=request.sid)
        return

    try:
        jam_ref = db.collection('artifacts').document(APP_ID).collection('public_jams').document(jam_id)
        jam_doc = jam_ref.get()

        if not jam_doc.exists:
            emit('error', {'message': 'Jam not found.'}, room=request.sid)
            return

        jam_data = jam_doc.to_dict()
        if jam_data.get('host_id') != user_id:
            emit('error', {'message': 'Only the host can control song playback.'}, room=request.sid)
            return

        queue = jam_data.get('queue', [])
        if queue:
            next_song_in_queue = queue.pop(0) # Remove first song
            jam_ref.update({
                'current_song': next_song_in_queue,
                'current_song_state': 'playing',
                'current_song_time': 0,
                'queue': queue, # Update queue in Firestore
                'last_updated': firestore.SERVER_TIMESTAMP
            })
            emit('jam_state_update', {
                'current_song': next_song_in_queue,
                'current_song_state': 'playing',
                'current_song_time': 0,
                'updater_id': user_id
            }, room=jam_id, include_self=True)
            emit('jam_queue_update', {'jam_id': jam_id, 'queue': queue}, room=jam_id)
            print(f"Jam {jam_id}: Host {user_id} played next song: {next_song_in_queue.get('title')}")
        else:
            # If queue is empty, stop playback
            jam_ref.update({
                'current_song': None,
                'current_song_state': 'stopped',
                'current_song_time': 0,
                'last_updated': firestore.SERVER_TIMESTAMP
            })
            emit('jam_state_update', {
                'current_song': None,
                'current_song_state': 'stopped',
                'current_song_time': 0,
                'updater_id': user_id
            }, room=jam_id, include_self=True)
            emit('jam_queue_update', {'jam_id': jam_id, 'queue': []}, room=jam_id)
            print(f"Jam {jam_id}: Queue empty, stopping playback.")

    except Exception as e:
        print(f"Error playing next song for jam {jam_id}: {e}")
        traceback.print_exc()
        emit('error', {'message': f'Failed to play next song: {e}'}, room=request.sid)


@socketio.on('remove_song_from_queue')
@login_required
def remove_song_from_queue(data):
    user_id = session.get('user_id')
    jam_id = data.get('jam_id')
    song_id = data.get('song_id')

    if not all([jam_id, song_id]):
        emit('error', {'message': 'Jam ID and song ID are required.'}, room=request.sid)
        return

    try:
        jam_ref = db.collection('artifacts').document(APP_ID).collection('public_jams').document(jam_id)
        jam_doc = jam_ref.get()

        if not jam_doc.exists:
            emit('error', {'message': 'Jam not found.'}, room=request.sid)
            return

        jam_data = jam_doc.to_dict()
        if jam_data.get('host_id') != user_id: # Only host can remove songs
            emit('error', {'message': 'Only the host can remove songs from the queue.'}, room=request.sid)
            return

        queue = jam_data.get('queue', [])
        updated_queue = [s for s in queue if s.get('id') != song_id]

        if len(updated_queue) < len(queue): # If a song was actually removed
            jam_ref.update({
                'queue': updated_queue,
                'last_updated': firestore.SERVER_TIMESTAMP
            })
            emit('jam_queue_update', {'jam_id': jam_id, 'queue': updated_queue}, room=jam_id)
            print(f"Song {song_id} removed from queue for jam {jam_id} by {user_id}")
        else:
            emit('error', {'message': 'Song not found in queue.'}, room=request.sid)

    except Exception as e:
        print(f"Error removing song from queue for jam {jam_id}: {e}")
        traceback.print_exc()
        emit('error', {'message': f'Failed to remove song: {e}'}, room=request.sid)


if __name__ == '__main__':
    # Use 0.0.0.0 to make it accessible from other devices on the network
    # Use app.run for development, socketio.run for production or when using websockets
    # app.run(debug=True, host='0.0.0.0', port=5000)
    socketio.run(app, debug=True, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True) # allow_unsafe_werkzeug for latest Werkzeug version
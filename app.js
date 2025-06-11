import React, { useState, useEffect, useRef, useCallback } from 'react';
import { initializeApp } from 'firebase/app';
import { getAuth, signInAnonymously, onAuthStateChanged } from 'firebase/auth';
import { getFirestore, doc, getDoc, setDoc, updateDoc, deleteDoc, onSnapshot, collection, serverTimestamp, runTransaction } from 'firebase/firestore';
import { FieldValue } from 'firebase/firestore'; // Import FieldValue for delete operation

// Custom Message Box Component
const CustomMessageBox = ({ message, type, duration, onClose }) => {
  const [isVisible, setIsVisible] = useState(false);
  const timerRef = useRef(null);

  useEffect(() => {
    if (message) {
      setIsVisible(true);
      if (timerRef.current) {
        clearTimeout(timerRef.current);
      }
      if (duration > 0) {
        timerRef.current = setTimeout(() => {
          setIsVisible(false);
          if (onClose) onClose();
        }, duration);
      }
    } else {
      setIsVisible(false);
      if (timerRef.current) {
        clearTimeout(timerRef.current);
      }
    }
  }, [message, duration, onClose]);

  if (!isVisible) return null;

  const bgColor = type === 'error' ? 'bg-red-500' : 'bg-gray-700';

  return (
    <div
      className={`fixed bottom-4 left-1/2 -translate-x-1/2 p-3 rounded-lg shadow-lg text-white text-center z-50 transition-opacity duration-300 ${bgColor}`}
      style={{ opacity: isVisible ? 1 : 0 }}
    >
      {message}
    </div>
  );
};

// Main App component
const App = () => {
  const [db, setDb] = useState(null);
  const [auth, setAuth] = useState(null);
  const [userId, setUserId] = useState(null);
  const [isAuthReady, setIsAuthReady] = useState(false);
  const [currentView, setCurrentView] = useState('home'); // 'home' or 'jam'
  const [currentJamId, setCurrentJamId] = useState(null);
  const [userName, setUserName] = useState('');
  const [showNameModal, setShowNameModal] = useState(false);
  const [appMessage, setAppMessage] = useState('');
  const [appMessageType, setAppMessageType] = useState('info');

  const showAppMessage = (message, type = 'info', duration = 3000) => {
    setAppMessage(message);
    setAppMessageType(type);
    // The CustomMessageBox component will handle clearing the message based on duration
  };

  // Initialize Firebase and set up auth listener
  useEffect(() => {
    try {
      // Use Canvas global variables for app_id and firebase_config
      const appId = typeof __app_id !== 'undefined' ? __app_id : 'default-app-id';
      const firebaseConfig = typeof __firebase_config !== 'undefined' ? JSON.parse(__firebase_config) : {};

      const app = initializeApp(firebaseConfig);
      const firestore = getFirestore(app);
      const authentication = getAuth(app);

      setDb(firestore);
      setAuth(authentication);

      // Listen for auth state changes
      const unsubscribe = onAuthStateChanged(authentication, async (user) => {
        if (user) {
          setUserId(user.uid);
          console.log('Firebase user signed in:', user.uid);
        } else {
          // If no user, sign in anonymously
          try {
            const anonymousUserCredential = await signInAnonymously(authentication);
            setUserId(anonymousUserCredential.user.uid);
            console.log('Signed in anonymously:', anonymousUserCredential.user.uid);
          } catch (error) {
            console.error('Error signing in anonymously:', error);
            showAppMessage(`Authentication failed: ${error.message}. Jam features may not work.`, 'error', 6000);
          }
        }
        setIsAuthReady(true); // Auth state is ready after initial check or sign-in
      });

      return () => unsubscribe(); // Cleanup auth listener
    } catch (error) {
      console.error("Failed to initialize Firebase:", error);
      showAppMessage(`Failed to initialize Firebase: ${error.message}`, 'error', 6000);
      setIsAuthReady(true); // Still set ready to allow UI to render, maybe show an error
    }
  }, []);

  // Handle URL parameters for joining a jam
  useEffect(() => {
    if (isAuthReady && userId) {
      const params = new URLSearchParams(window.location.search);
      const jamIdFromUrl = params.get('jamId');
      if (jamIdFromUrl) {
        // Automatically try to join if jamId is in URL
        setCurrentJamId(jamIdFromUrl);
        setShowNameModal(true); // Prompt for name before joining
      }
    }
  }, [isAuthReady, userId]);

  // Handle user name input before joining/creating a jam
  const handleStartJam = (jamId = null) => {
    setShowNameModal(true);
    setCurrentJamId(jamId); // Store jamId for later use
  };

  const confirmUserName = () => {
    if (userName.trim()) {
      setShowNameModal(false);
      // Logic for joining/creating jam is handled by the Home/JamSession components
      // The `currentJamId` being set in handleStartJam will trigger the JamSession view in the return JSX
    } else {
      showAppMessage("Please enter a name to join or create a jam.", 'error', 3000); // FIX: Replaced alert
    }
  };

  if (!isAuthReady) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-gray-900 text-white">
        <div className="text-xl font-semibold">Loading Firebase...</div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-900 text-white font-inter">
      {showNameModal && (
        <div className="fixed inset-0 bg-black bg-opacity-75 flex items-center justify-center z-50">
          <div className="bg-gray-800 p-8 rounded-lg shadow-xl max-w-sm w-full">
            <h2 className="text-2xl font-bold mb-4 text-center">Enter Your Name</h2>
            <p className="text-gray-300 mb-6 text-center">This name will be visible to others in the jam.</p>
            <input
              type="text"
              className="w-full p-3 mb-6 bg-gray-700 border border-gray-600 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
              placeholder="Your Name"
              value={userName}
              onChange={(e) => setUserName(e.target.value)}
              onKeyPress={(e) => {
                if (e.key === 'Enter') {
                  confirmUserName();
                }
              }}
            />
            <button
              onClick={confirmUserName}
              className="w-full bg-blue-600 hover:bg-blue-700 text-white font-bold py-3 px-4 rounded-md shadow-lg transition duration-300 ease-in-out transform hover:scale-105"
            >
              Start Jam
            </button>
          </div>
        </div>
      )}

      {currentView === 'home' && (
        <Home
          db={db}
          auth={auth}
          userId={userId}
          setCurrentJamId={setCurrentJamId}
          setCurrentView={setCurrentView}
          onStartJam={handleStartJam}
          showAppMessage={showAppMessage} // Pass showAppMessage to Home
        />
      )}

      {currentView === 'jam' && currentJamId && userName && (
        <JamSession
          db={db}
          auth={auth}
          userId={userId}
          jamId={currentJamId}
          userName={userName}
          setCurrentView={setCurrentView}
          setCurrentJamId={setCurrentJamId}
          showAppMessage={showAppMessage} // Pass showAppMessage to JamSession
        />
      )}

      <CustomMessageBox message={appMessage} type={appMessageType} duration={3000} onClose={() => setAppMessage('')} />
    </div>
  );
};

// Home Component: For creating or joining a jam
const Home = ({ db, auth, userId, setCurrentJamId, setCurrentView, onStartJam, showAppMessage }) => {
  const [joinInput, setJoinInput] = useState('');

  const createNewJam = async () => {
    showAppMessage('Creating jam...');
    try {
      const jamsCollectionRef = collection(db, `artifacts/${typeof __app_id !== 'undefined' ? __app_id : 'default-app-id'}/public/data/jams`);
      const newJamRef = doc(jamsCollectionRef); // Create a new document reference with an auto-generated ID

      const initialJamData = {
        hostId: userId,
        currentSong: null,
        currentTime: 0,
        isPlaying: false,
        playlist: [],
        users: { [userId]: 'Unnamed User' }, // Will be updated with actual name via prop drill
        allPermissions: false,
        createdAt: serverTimestamp(),
      };

      await setDoc(newJamRef, initialJamData);
      const newJamId = newJamRef.id;
      setCurrentJamId(newJamId);
      onStartJam(newJamId); // Pass the new jam ID to the App component to trigger name modal
      showAppMessage('Jam created successfully!', 'success', 2000);
    } catch (error) {
      console.error('Error creating new jam:', error);
      showAppMessage(`Failed to create jam: ${error.message}`, 'error', 4000);
    }
  };

  const joinExistingJam = async () => {
    if (!joinInput.trim()) {
      showAppMessage('Please enter a Jam ID.', 'error', 3000);
      return;
    }
    showAppMessage('Joining jam...');
    try {
      const jamDocRef = doc(db, `artifacts/${typeof __app_id !== 'undefined' ? __app_id : 'default-app-id'}/public/data/jams`, joinInput.trim());
      const jamDocSnap = await getDoc(jamDocRef);

      if (jamDocSnap.exists()) {
        setCurrentJamId(joinInput.trim());
        onStartJam(joinInput.trim()); // Pass the existing jam ID to trigger name modal
        showAppMessage('Successfully joined jam!', 'success', 2000);
      } else {
        showAppMessage('Jam not found. Please check the ID.', 'error', 4000);
      }
    } catch (error) {
      console.error('Error joining jam:', error);
      showAppMessage(`Failed to join jam: ${error.message}`, 'error', 4000);
    }
  };

  return (
    <div className="flex flex-col items-center justify-center min-h-screen p-4">
      <h1 className="text-5xl font-extrabold mb-8 text-transparent bg-clip-text bg-gradient-to-r from-purple-400 to-pink-600 animate-pulse">
        Music Jam Session
      </h1>
      <p className="text-xl text-gray-300 mb-10">Collaborate and listen to music together!</p>

      <div className="flex flex-col items-center space-y-6 w-full max-w-md">
        <button
          onClick={createNewJam}
          className="w-full bg-gradient-to-r from-blue-500 to-indigo-600 hover:from-blue-600 hover:to-indigo-700 text-white font-bold py-4 px-6 rounded-xl shadow-lg transition duration-300 ease-in-out transform hover:scale-105 border-2 border-blue-400"
        >
          <i className="fas fa-plus-circle mr-3"></i> Create New Jam
        </button>

        <div className="w-full text-center text-gray-400 font-semibold uppercase tracking-wide">
          — OR —
        </div>

        <div className="w-full bg-gray-800 p-6 rounded-xl shadow-lg border border-gray-700">
          <h2 className="text-3xl font-bold mb-4 text-center text-gray-100">Join Existing Jam</h2>
          <input
            type="text"
            className="w-full p-4 mb-4 bg-gray-700 border border-gray-600 rounded-lg text-lg placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-purple-500"
            placeholder="Enter Jam ID"
            value={joinInput}
            onChange={(e) => setJoinInput(e.target.value)}
            onKeyPress={(e) => {
              if (e.key === 'Enter') {
                joinExistingJam();
              }
            }}
          />
          <button
            onClick={joinExistingJam}
            className="w-full bg-gradient-to-r from-pink-500 to-rose-600 hover:from-pink-600 hover:to-rose-700 text-white font-bold py-4 px-6 rounded-xl shadow-lg transition duration-300 ease-in-out transform hover:scale-105 border-2 border-pink-400"
          >
            <i className="fas fa-sign-in-alt mr-3"></i> Join Jam
          </button>
        </div>
      </div>
    </div>
  );
};

// Jam Session Component: Handles real-time playback and collaboration
const JamSession = ({ db, auth, userId, jamId, userName, setCurrentView, setCurrentJamId, showAppMessage }) => {
  const audioRef = useRef(new Audio());
  const [jamData, setJamData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [fileInputKey, setFileInputKey] = useState(Date.now()); // Key to force re-render file input
  const [currentPlaybackTime, setCurrentPlaybackTime] = useState(0); // Local state for immediate UI update
  const [duration, setDuration] = useState(0); // Duration of current song
  const [isPlayingLocally, setIsPlayingLocally] = useState(false); // Local state for play/pause button
  const [hostedSongsList, setHostedSongsList] = useState([]); // List of songs from hosted_songs_manifest.json
  const [searchTerm, setSearchTerm] = useState('');
  const [searchResults, setSearchResults] = useState([]);
  const [showSearchSection, setShowSearchSection] = useState(false);


  const jamDocRef = doc(db, `artifacts/${typeof __app_id !== 'undefined' ? __app_id : 'default-app-id'}/public/data/jams`, jamId);
  const isHost = jamData && jamData.hostId === userId;
  const canControl = jamData && (isHost || jamData.allPermissions);

  // Load hosted songs manifest
  useEffect(() => {
    const loadHostedSongsManifest = async () => {
      try {
        const response = await fetch('hosted_songs_manifest.json');
        if (!response.ok) {
          throw new Error(`HTTP error! status: ${response.status}`);
        }
        const data = await response.json();
        setHostedSongsList(data);
        console.log("Loaded hosted songs manifest:", data.length, "songs");
      } catch (err) {
        console.error("Error loading hosted_songs_manifest.json:", err);
        showAppMessage("Failed to load hosted songs manifest. Search feature may be limited.", 'error', 6000);
      }
    };
    loadHostedSongsManifest();
  }, []);

  // Update search results whenever searchTerm or hostedSongsList changes
  useEffect(() => {
    if (searchTerm.trim() === '') {
      setSearchResults([]);
      return;
    }
    const lowerCaseQuery = searchTerm.toLowerCase();
    const filtered = hostedSongsList.filter(song =>
      (song.title && song.title.toLowerCase().includes(lowerCaseQuery)) ||
      (song.artist && song.artist.toLowerCase().includes(lowerCaseQuery))
    );
    setSearchResults(filtered);
  }, [searchTerm, hostedSongsList]);


  // Set up real-time listener for jam data
  useEffect(() => {
    if (!db || !jamId) return;

    setLoading(true);
    const unsubscribe = onSnapshot(jamDocRef, (docSnap) => {
      if (docSnap.exists()) {
        const data = docSnap.data();
        setJamData(data);

        // Add current user to the jam's user list if not already present or name changed
        const users = data.users || {};
        if (!users[userId] || users[userId] !== userName) {
          updateDoc(jamDocRef, {
            [`users.${userId}`]: userName,
          }).catch(e => console.error("Error updating user nickname in jam:", e));
        }

        // Sync audio player with Firestore state
        const audio = audioRef.current;
        if (data.currentSong && data.currentSong.url !== audio.src) {
          audio.src = data.currentSong.url;
          audio.load();
        }

        const timeDiff = Math.abs(audio.currentTime - data.currentTime);
        // Only seek if the difference is significant and current user is not actively seeking
        // Also, only allow guests to seek, or the host if it's the very first load or a major desync
        if (timeDiff > 1.5 && !audio.seeking && (!isHost || audio.readyState < 3)) {
          audio.currentTime = data.currentTime;
        }

        if (data.isPlaying && !isPlayingLocally) {
          audio.play().catch(e => {
            console.error("Error playing audio:", e);
            showAppMessage("Autoplay prevented or error playing audio. Please interact to play.", 'error', 4000);
          });
          setIsPlayingLocally(true);
        } else if (!data.isPlaying && isPlayingLocally) {
          audio.pause();
          setIsPlayingLocally(false);
        }
        setLoading(false);
      } else {
        setError('Jam session not found or has ended. Returning to home.');
        setJamData(null);
        setLoading(false);
        // Remove user from the URL if jam ends for them
        const newUrl = window.location.origin + window.location.pathname;
        window.history.replaceState({}, document.title, newUrl);
        setTimeout(() => {
          setCurrentView('home');
          setCurrentJamId(null);
        }, 3000);
      }
    }, (err) => {
      console.error('Error listening to jam data:', err);
      setError('Failed to fetch real-time jam updates.');
      setLoading(false);
      showAppMessage('Lost connection to jam updates. Rejoining might be needed.', 'error', 5000);
    });

    return () => unsubscribe(); // Cleanup listener on unmount
  }, [db, jamId, userId, userName, isPlayingLocally, isHost, showAppMessage]);


  // Handle beforeunload to remove user from jam or delete room if host
  useEffect(() => {
    const handleBeforeUnload = async (event) => {
      if (!db || !userId || !jamId || !jamData) return;

      try {
        if (jamData.hostId === userId) {
          // If host is leaving, delete the jam room
          await deleteDoc(jamDocRef);
          console.log('Host left, jam room deleted.');
        } else {
          // If a regular user is leaving, remove them from the users map
          await updateDoc(jamDocRef, {
            [`users.${userId}`]: FieldValue.delete(), // FIX: Correctly use FieldValue.delete()
          });
          console.log('User left, removed from jam list.');
        }
      } catch (error) {
        console.error('Error handling user/host leave on beforeunload:', error);
      }
    };

    window.addEventListener('beforeunload', handleBeforeUnload);

    return () => {
      window.removeEventListener('beforeunload', handleBeforeUnload);
    };
  }, [db, userId, jamId, jamData, jamDocRef]);

  // Audio event listeners for local state and Firestore updates
  useEffect(() => {
    const audio = audioRef.current;

    const handlePlay = () => {
      setIsPlayingLocally(true);
      if (isHost) { // Only host updates Firestore state based on their playback
        updateDoc(jamDocRef, { isPlaying: true, currentTime: audio.currentTime }).catch(console.error);
      }
    };
    const handlePause = () => {
      setIsPlayingLocally(false);
      if (isHost) { // Only host updates Firestore state based on their playback
        updateDoc(jamDocRef, { isPlaying: false, currentTime: audio.currentTime }).catch(console.error);
      }
    };
    const handleTimeUpdate = () => {
      setCurrentPlaybackTime(audio.currentTime);
      // Only host updates Firestore if significant time difference and not actively seeking
      if (isHost && !audio.seeking && Math.abs(audio.currentTime - (jamData?.currentTime || 0)) > 1.5) {
        updateDoc(jamDocRef, { currentTime: audio.currentTime }).catch(console.error);
      }
    };
    const handleEnded = async () => {
      setIsPlayingLocally(false);
      if (canControl && jamData && jamData.playlist && jamData.playlist.length > 0) {
        const currentIndex = jamData.playlist.findIndex(s => s.url === jamData.currentSong?.url);
        const nextSongIndex = (currentIndex + 1) % jamData.playlist.length;
        const nextSong = jamData.playlist[nextSongIndex];
        await updateDoc(jamDocRef, {
          currentSong: nextSong,
          currentTime: 0,
          isPlaying: true, // Auto-play next song
        });
      } else if (canControl) {
        await updateDoc(jamDocRef, { isPlaying: false, currentTime: 0 });
      }
    };
    const handleDurationChange = () => {
      setDuration(audio.duration || 0);
    };

    audio.addEventListener('play', handlePlay);
    audio.addEventListener('pause', handlePause);
    audio.addEventListener('timeupdate', handleTimeUpdate);
    audio.addEventListener('ended', handleEnded);
    audio.addEventListener('durationchange', handleDurationChange);

    return () => {
      audio.removeEventListener('play', handlePlay);
      audio.removeEventListener('pause', handlePause);
      audio.removeEventListener('timeupdate', handleTimeUpdate);
      audio.removeEventListener('ended', handleEnded);
      audio.removeEventListener('durationchange', handleDurationChange);
    };
  }, [jamDocRef, canControl, jamData, isHost]);


  // Add song to playlist (handles both local file and hosted file)
  const addSongToCurrentPlaylist = async (song) => {
    // Assign a unique ID if missing
    if (!song.id) {
        song.id = 'song_' + Date.now() + Math.random().toString(36).substring(2, 9);
    }
    if (!canControl && jamData?.playlist.length > 0) { // Guests can't add if playlist not empty and no permissions
      showAppMessage("You don't have permission to add songs to this jam.", 'error', 3000);
      return;
    }
    if (!jamData) {
      showAppMessage("Jam data not loaded. Please try again.", 'error', 3000);
      return;
    }

    try {
      await runTransaction(db, async (transaction) => {
        const jamDoc = await transaction.get(jamDocRef);
        if (!jamDoc.exists()) {
          throw new Error("Jam document does not exist!");
        }
        const currentPlaylist = jamDoc.data().playlist || [];
        const updatedPlaylist = [...currentPlaylist, song];

        transaction.update(jamDocRef, { playlist: updatedPlaylist });

        // If no song is currently playing, set this as the current song and start playing
        if (!jamDoc.data().currentSong) {
          transaction.update(jamDocRef, {
            currentSong: song,
            currentTime: 0,
            isPlaying: true,
          });
        }
      });
      showAppMessage(`Added "${song.title}" to Jam Session playlist!`, 'success', 2000);
      setFileInputKey(Date.now()); // Reset file input
    } catch (error) {
      console.error('Error adding song to playlist:', error);
      showAppMessage(`Failed to add song: ${error.message}.`, 'error', 4000);
    }
  };


  const handleFileChange = async (event) => {
    const file = event.target.files[0];
    if (file && file.type === 'audio/mpeg') {
      const reader = new FileReader();
      reader.onload = (e) => {
        const newSong = {
          id: Date.now().toString(), // Simple unique ID
          title: file.name,
          url: e.target.result, // Data URL
          type: 'audio',
          thumbnail: "https://placehold.co/128x128/CCCCCC/FFFFFF?text=MP3"
        };
        addSongToCurrentPlaylist(newSong);
      };
      reader.readAsDataURL(file);
    } else {
      showAppMessage('Please select an MP3 file.', 'error', 3000);
    }
  };

  const playSong = async (song) => {
    if (canControl) {
      await updateDoc(jamDocRef, {
        currentSong: song,
        currentTime: 0,
        isPlaying: true,
      });
    } else {
      showAppMessage("You don't have permission to change the song.", 'error', 3000); // FIX: Replaced setError
    }
  };

  const togglePlayback = async () => {
    if (!jamData || !jamData.currentSong) {
      showAppMessage("No song loaded to play.", 'error', 3000); // FIX: Replaced setError
      return;
    }
    if (canControl) {
      await updateDoc(jamDocRef, {
        isPlaying: !jamData.isPlaying,
      });
    } else {
      showAppMessage("You don't have permission to control playback.", 'error', 3000); // FIX: Replaced setError
    }
  };

  const handleSeek = async (e) => {
    const newTime = parseFloat(e.target.value);
    audioRef.current.currentTime = newTime; // Update local immediately
    if (canControl) {
      // Only update Firestore if current user is host to prevent race conditions
      // Guests just update their local player based on host's state
      if (isHost) {
         await updateDoc(jamDocRef, { currentTime: newTime });
      }
    }
  };

  const formatTime = (seconds) => {
    if (isNaN(seconds) || seconds < 0) return "00:00";
    const minutes = Math.floor(seconds / 60);
    const remainingSeconds = Math.floor(seconds % 60);
    return `${minutes.toString().padStart(2, '0')}:${remainingSeconds.toString().padStart(2, '0')}`;
  };

  const toggleAllPermissions = async () => {
    if (isHost) {
      await updateDoc(jamDocRef, {
        allPermissions: !jamData.allPermissions,
      });
    } else {
      showAppMessage("Only the host can change permissions.", 'error', 3000); // FIX: Replaced setError
    }
  };

  const leaveJam = async () => {
    try {
      if (jamData.hostId === userId) {
        // If host is leaving, delete the jam room
        await deleteDoc(jamDocRef);
        console.log('Host left, jam room deleted.');
        showAppMessage('You have ended the Jam Session for everyone.', 'info', 3000);
      } else {
        // If a regular user is leaving, remove them from the users map
        const updatedUsers = { ...jamData.users };
        delete updatedUsers[userId];
        await updateDoc(jamDocRef, {
          users: updatedUsers,
        });
        console.log('User left, removed from jam list.');
        showAppMessage('You have left the Jam Session.', 'info', 3000);
      }
    } catch (error) {
      console.error('Error leaving jam:', error);
      showAppMessage('Failed to leave jam cleanly. Please try again.', 'error', 3000); // FIX: Replaced setError
    } finally {
      setCurrentView('home');
      setCurrentJamId(null);
      // Remove jamId from URL params when leaving
      const newUrl = window.location.origin + window.location.pathname;
      window.history.replaceState({}, document.title, newUrl);
    }
  };

  const handleRandomHostedPlay = () => {
    if (!canControl) {
      showAppMessage("You don't have permission to start random playback.", 'error', 3000);
      return;
    }
    if (hostedSongsList.length === 0) {
      showAppMessage("No hosted songs available to play randomly.", 'error', 3000);
      return;
    }

    const randomSongs = [];
    const tempHostedList = [...hostedSongsList]; // Create a mutable copy
    while (randomSongs.length < Math.min(5, hostedSongsList.length)) {
      const randomIndex = Math.floor(Math.random() * tempHostedList.length);
      randomSongs.push(tempHostedList.splice(randomIndex, 1)[0]); // Remove to ensure uniqueness
    }

    // Replace current playlist with random songs and set the first one to play
    updateDoc(jamDocRef, {
      playlist: randomSongs,
      currentSong: randomSongs.length > 0 ? randomSongs[0] : null,
      currentTime: 0,
      isPlaying: randomSongs.length > 0,
    }).then(() => {
      showAppMessage(`Started random playback with ${randomSongs.length} songs!`, 'success', 2000);
    }).catch(e => {
      console.error("Error setting random playlist:", e);
      showAppMessage("Failed to start random playback.", 'error', 3000);
    });
  };


  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-gray-900 text-white">
        <div className="text-xl font-semibold">Loading Jam Session...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center min-h-screen bg-gray-900 text-red-400 p-4 text-center">
        <p className="text-2xl font-bold mb-4">{error}</p>
        <button
          onClick={() => { setCurrentView('home'); setCurrentJamId(null); setError(null); }}
          className="bg-blue-600 hover:bg-blue-700 text-white font-bold py-2 px-4 rounded-md shadow-lg transition duration-300 ease-in-out transform hover:scale-105"
        >
          Go to Home
        </button>
      </div>
    );
  }

  const shareLink = `${window.location.origin}?jamId=${jamId}`;

  return (
    <div className="min-h-screen flex flex-col bg-gray-900 text-white p-4">
      <header className="flex flex-col md:flex-row items-center justify-between p-4 bg-gray-800 rounded-lg shadow-xl mb-6">
        <h1 className="text-4xl font-extrabold text-transparent bg-clip-text bg-gradient-to-r from-green-400 to-teal-500 mb-4 md:mb-0">
          Jam Session: <span className="text-white text-3xl">{jamId}</span>
        </h1>
        <div className="flex flex-wrap justify-center gap-4">
          <button
            onClick={leaveJam}
            className="bg-red-600 hover:bg-red-700 text-white font-bold py-2 px-4 rounded-md shadow-md transition duration-300 ease-in-out transform hover:scale-105"
          >
            <i className="fas fa-sign-out-alt mr-2"></i> Leave Jam
          </button>
          {isHost && (
            <button
              onClick={toggleAllPermissions}
              className={`font-bold py-2 px-4 rounded-md shadow-md transition duration-300 ease-in-out transform hover:scale-105 ${
                jamData?.allPermissions ? 'bg-orange-500 hover:bg-orange-600' : 'bg-gray-600 hover:bg-gray-700'
              }`}
            >
              <i className="fas fa-users mr-2"></i> {jamData?.allPermissions ? 'All Can Control' : 'Host Controls'}
            </button>
          )}
          <button
            onClick={() => {
              navigator.clipboard.writeText(shareLink).then(() => {
                showAppMessage('Jam link copied to clipboard!', 'info', 2000); // FIX: Replaced alert
              }).catch(err => {
                console.error('Failed to copy text: ', err);
                showAppMessage('Failed to copy link. Please copy it manually: ' + shareLink, 'error', 4000); // FIX: Replaced alert
              });
            }}
            className="bg-purple-600 hover:bg-purple-700 text-white font-bold py-2 px-4 rounded-md shadow-md transition duration-300 ease-in-out transform hover:scale-105"
          >
            <i className="fas fa-share-alt mr-2"></i> Share Jam Link
          </button>
        </div>
      </header>

      <main className="flex flex-col md:flex-row flex-grow gap-6">
        {/* Music Player Section */}
        <section className="flex-1 bg-gray-800 p-6 rounded-lg shadow-xl flex flex-col justify-between">
          <div>
            <h2 className="text-3xl font-bold mb-4 text-center text-cyan-400">Now Playing</h2>
            {jamData?.currentSong ? (
              <div className="text-center">
                {/* FIX: Add thumbnail display */}
                <img src={jamData.currentSong.thumbnail || "https://placehold.co/128x128/CCCCCC/FFFFFF?text=MP3"} alt="Album Art" className="w-32 h-32 rounded-lg mx-auto mb-4 object-cover shadow-md" />
                <p className="text-xl font-semibold mb-2">{jamData.currentSong.title}</p>
                <p className="text-md text-gray-300 mb-4">{jamData.currentSong.artist || 'Unknown Artist'}</p>
                <div className="flex items-center justify-center space-x-4 mb-4">
                  <button
                    onClick={togglePlayback}
                    disabled={!canControl}
                    className="p-4 bg-indigo-600 rounded-full shadow-lg hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed transform hover:scale-110 transition duration-200"
                  >
                    <i className={`fas ${jamData.isPlaying ? 'fa-pause' : 'fa-play'} text-3xl`}></i>
                  </button>
                </div>
                <div className="flex items-center space-x-2 mt-4 text-gray-300">
                  <span>{formatTime(currentPlaybackTime)}</span>
                  <input
                    type="range"
                    min="0"
                    max={duration}
                    value={currentPlaybackTime}
                    onChange={handleSeek}
                    className="w-full h-2 bg-gray-700 rounded-lg appearance-none cursor-pointer range-lg accent-teal-400"
                    disabled={!canControl}
                  />
                  <span>{formatTime(duration)}</span>
                </div>
              </div>
            ) : (
              <p className="text-center text-gray-400 text-lg">No song selected. Add one to the playlist!</p>
            )}
          </div>

          <div className="mt-8 text-center">
            <h3 className="text-2xl font-bold mb-4 text-emerald-400">Add MP3 to Playlist</h3>
            <label htmlFor="mp3-upload" className="inline-block bg-green-600 hover:bg-green-700 text-white font-bold py-3 px-6 rounded-md shadow-lg transition duration-300 ease-in-out transform hover:scale-105 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed">
              <i className="fas fa-upload mr-2"></i> Upload Local MP3
            </label>
            <input
              key={fileInputKey} // Ensures input resets after file selection
              id="mp3-upload"
              type="file"
              accept=".mp3"
              onChange={handleFileChange}
              className="hidden"
              disabled={!canControl && jamData?.playlist.length > 0} // Allow anyone to upload if playlist is empty, otherwise only controllers
            />

            {/* FIX: Add Random Hosted Play Button */}
            <button
              onClick={handleRandomHostedPlay}
              disabled={!canControl}
              className="mt-4 w-full bg-yellow-600 hover:bg-yellow-700 text-white font-bold py-3 px-6 rounded-md shadow-lg transition duration-300 ease-in-out transform hover:scale-105 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <i className="fas fa-random mr-2"></i> Play Random Hosted Songs
            </button>

            {/* FIX: Add Search Hosted MP3s Section */}
            <button
              onClick={() => setShowSearchSection(!showSearchSection)}
              disabled={!canControl}
              className="mt-4 w-full bg-blue-600 hover:bg-blue-700 text-white font-bold py-3 px-6 rounded-md shadow-lg transition duration-300 ease-in-out transform hover:scale-105 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <i className="fas fa-search mr-2"></i> {showSearchSection ? 'Hide Hosted Search' : 'Search Hosted MP3s'}
            </button>

            {showSearchSection && (
              <div className="mt-6 p-4 bg-gray-700 rounded-lg">
                <input
                  type="text"
                  placeholder="Search hosted songs..."
                  className="w-full p-2 rounded-md bg-gray-600 border border-gray-500 text-white placeholder-gray-300 focus:outline-none focus:ring-2 focus:ring-blue-500"
                  value={searchTerm}
                  onChange={(e) => setSearchTerm(e.target.value)}
                />
                <div className="mt-4 max-h-60 overflow-y-auto custom-scrollbar">
                  {searchResults.length > 0 ? (
                    <ul className="space-y-2">
                      {searchResults.map(song => (
                        <li key={song.id} className="flex items-center justify-between p-2 bg-gray-600 rounded-md">
                          <span className="truncate text-sm">{song.title} - {song.artist}</span>
                          <button
                            onClick={() => addSongToCurrentPlaylist(song)}
                            className="ml-2 px-3 py-1 bg-green-500 text-white rounded-md hover:bg-green-600 text-xs disabled:opacity-50 disabled:cursor-not-allowed"
                            disabled={!canControl}
                          >
                            Add
                          </button>
                        </li>
                      ))}
                    </ul>
                  ) : searchTerm.length > 0 ? (
                    <p className="text-center text-gray-400">No results found.</p>
                  ) : (
                    <p className="text-center text-gray-400">Start typing to search hosted MP3s.</p>
                  )}
                </div>
              </div>
            )}
          </div>
        </section>

        {/* Playlist and Users Section */}
        <section className="md:w-1/3 bg-gray-800 p-6 rounded-lg shadow-xl flex flex-col">
          <h2 className="text-3xl font-bold mb-4 text-center text-yellow-400">Playlist</h2>
          <div className="flex-grow overflow-y-auto custom-scrollbar border border-gray-700 rounded-md p-2">
            {jamData?.playlist && jamData.playlist.length > 0 ? (
              <ul className="space-y-2">
                {jamData.playlist.map((song) => (
                  <li
                    key={song.id}
                    className={`p-3 rounded-lg flex justify-between items-center transition duration-200 ease-in-out
                      ${jamData.currentSong?.id === song.id ? 'bg-blue-600 text-white shadow-md' : 'bg-gray-700 hover:bg-gray-600'}`}
                  >
                    <span className="font-medium text-lg truncate flex-grow mr-2">{song.title}</span>
                    <button
                      onClick={() => playSong(song)}
                      disabled={!canControl}
                      className="bg-blue-500 hover:bg-blue-400 text-white text-sm py-1 px-3 rounded-md disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <i className="fas fa-play"></i> Play
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-center text-gray-400">Playlist is empty. Add some songs!</p>
            )}
          </div>

          <h2 className="text-3xl font-bold mt-6 mb-4 text-center text-orange-400">Participants</h2>
          <div className="flex-grow overflow-y-auto custom-scrollbar border border-gray-700 rounded-md p-2">
            {jamData?.users && Object.keys(jamData.users).length > 0 ? (
              <ul className="space-y-2">
                {Object.entries(jamData.users).map(([id, name]) => (
                  <li key={id} className="p-3 bg-gray-700 rounded-lg flex items-center shadow-sm">
                    <i className={`fas fa-user-circle mr-3 text-2xl ${id === jamData.hostId ? 'text-red-400' : 'text-gray-400'}`}></i>
                    <span className="font-medium text-lg">{name} {id === jamData.hostId && '(Host)'} {id === userId && '(You)'}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-center text-gray-400">No other participants yet.</p>
            )}
          </div>
        </section>
      </main>

      {/* Custom Scrollbar Styles */}
      <style jsx>{`
        .custom-scrollbar::-webkit-scrollbar {
          width: 8px;
        }
        .custom-scrollbar::-webkit-scrollbar-track {
          background: #374151; /* gray-700 */
          border-radius: 10px;
        }
        .custom-scrollbar::-webkit-scrollbar-thumb {
          background: #6b7280; /* gray-500 */
          border-radius: 10px;
        }
        .custom-scrollbar::-webkit-scrollbar-thumb:hover {
          background: #4b5563; /* gray-600 */
        }
        /* For Firefox */
        .custom-scrollbar {
          scrollbar-width: thin;
          scrollbar-color: #6b7280 #374151;
        }
      `}</style>
    </div>
  );
};

export default App;

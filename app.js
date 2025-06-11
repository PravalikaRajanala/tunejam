import React, { useState, useEffect, useRef, useCallback } from 'react';
import { initializeApp } from 'firebase/app';
import { getAuth, signInAnonymously, onAuthStateChanged } from 'firebase/auth';
import { getFirestore, doc, getDoc, setDoc, updateDoc, deleteDoc, onSnapshot, collection, addDoc, serverTimestamp, runTransaction } from 'firebase/firestore';

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

  // Initialize Firebase and set up auth listener
  useEffect(() => {
    try {
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
          }
        }
        setIsAuthReady(true); // Auth state is ready after initial check or sign-in
      });

      return () => unsubscribe(); // Cleanup auth listener
    } catch (error) {
      console.error("Failed to initialize Firebase:", error);
      setIsAuthReady(true); // Still set ready to allow UI to render, maybe show an error
    }
  }, []);

  // Handle URL parameters for joining a jam
  useEffect(() => {
    if (isAuthReady && userId) {
      const params = new URLSearchParams(window.location.search);
      const jamIdFromUrl = params.get('jamId');
      if (jamIdFromUrl) {
        setCurrentJamId(jamIdFromUrl);
        setCurrentView('jam');
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
      if (currentJamId) {
        // Joining existing jam
        setCurrentView('jam');
      } else {
        // Creating new jam
        setCurrentView('jam');
      }
    } else {
      alert("Please enter a name to join or create a jam."); // Using alert as a temporary simple message
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
        />
      )}
    </div>
  );
};

// Home Component: For creating or joining a jam
const Home = ({ db, auth, userId, setCurrentJamId, setCurrentView, onStartJam }) => {
  const [joinInput, setJoinInput] = useState('');
  const [message, setMessage] = useState('');

  const createNewJam = async () => {
    setMessage('Creating jam...');
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
      setMessage('');
    } catch (error) {
      console.error('Error creating new jam:', error);
      setMessage(`Failed to create jam: ${error.message}`);
    }
  };

  const joinExistingJam = async () => {
    if (!joinInput.trim()) {
      setMessage('Please enter a Jam ID.');
      return;
    }
    setMessage('Joining jam...');
    try {
      const jamDocRef = doc(db, `artifacts/${typeof __app_id !== 'undefined' ? __app_id : 'default-app-id'}/public/data/jams`, joinInput.trim());
      const jamDocSnap = await getDoc(jamDocRef);

      if (jamDocSnap.exists()) {
        setCurrentJamId(joinInput.trim());
        onStartJam(joinInput.trim()); // Pass the existing jam ID to trigger name modal
        setMessage('');
      } else {
        setMessage('Jam not found. Please check the ID.');
      }
    } catch (error) {
      console.error('Error joining jam:', error);
      setMessage(`Failed to join jam: ${error.message}`);
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
      {message && <p className="mt-6 text-lg font-semibold text-yellow-400">{message}</p>}
    </div>
  );
};

// Jam Session Component: Handles real-time playback and collaboration
const JamSession = ({ db, auth, userId, jamId, userName, setCurrentView, setCurrentJamId }) => {
  const audioRef = useRef(new Audio());
  const [jamData, setJamData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [fileInputKey, setFileInputKey] = useState(Date.now()); // Key to force re-render file input
  const [currentPlaybackTime, setCurrentPlaybackTime] = useState(0); // Local state for immediate UI update
  const [duration, setDuration] = useState(0); // Duration of current song
  const [isPlayingLocally, setIsPlayingLocally] = useState(false); // Local state for play/pause button

  const jamDocRef = doc(db, `artifacts/${typeof __app_id !== 'undefined' ? __app_id : 'default-app-id'}/public/data/jams`, jamId);
  const isHost = jamData && jamData.hostId === userId;
  const canControl = jamData && (isHost || jamData.allPermissions);

  const fetchJamData = useCallback(async () => {
    try {
      const docSnap = await getDoc(jamDocRef);
      if (docSnap.exists()) {
        setJamData(docSnap.data());
        // Add current user to the jam's user list if not already present
        const users = docSnap.data().users || {};
        if (!users[userId] || users[userId] !== userName) {
          await updateDoc(jamDocRef, {
            [`users.${userId}`]: userName,
          });
        }
      } else {
        setError('Jam session not found or has ended.');
        setJamData(null);
      }
    } catch (err) {
      console.error('Error fetching jam data:', err);
      setError('Failed to load jam session.');
    } finally {
      setLoading(false);
    }
  }, [db, jamId, userId, userName]);

  // Set up real-time listener for jam data
  useEffect(() => {
    if (!db || !jamId) return;

    setLoading(true);
    const unsubscribe = onSnapshot(jamDocRef, (docSnap) => {
      if (docSnap.exists()) {
        const data = docSnap.data();
        setJamData(data);
        // Sync audio player with Firestore state
        const audio = audioRef.current;
        if (data.currentSong && data.currentSong.url !== audio.src) {
          audio.src = data.currentSong.url;
          audio.load();
        }

        // Only seek if the difference is significant to avoid constant seeking
        // And only if *not* currently seeking by user interaction
        const timeDiff = Math.abs(audio.currentTime - data.currentTime);
        if (timeDiff > 1 && !audio.seeking && canControl) { // Only force seek if current user is not controlling
          audio.currentTime = data.currentTime;
        }

        if (data.isPlaying && !isPlayingLocally) {
          audio.play().catch(e => console.error("Error playing audio:", e));
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
        setTimeout(() => {
          setCurrentView('home');
          setCurrentJamId(null);
        }, 3000);
      }
    }, (err) => {
      console.error('Error listening to jam data:', err);
      setError('Failed to fetch real-time jam updates.');
      setLoading(false);
    });

    return () => unsubscribe(); // Cleanup listener on unmount
  }, [db, jamId, canControl, isPlayingLocally]);


  // Handle beforeunload to remove user from jam or delete room if host
  useEffect(() => {
    const handleBeforeUnload = async () => {
      if (!db || !userId || !jamId || !jamData) return;

      try {
        if (jamData.hostId === userId) {
          // If host is leaving, delete the jam room
          await deleteDoc(jamDocRef);
          console.log('Host left, jam room deleted.');
        } else {
          // If a regular user is leaving, remove them from the users map
          await updateDoc(jamDocRef, {
            [`users.${userId}`]: deleteDoc.FieldValue.delete(),
          });
          console.log('User left, removed from jam list.');
        }
      } catch (error) {
        console.error('Error handling user/host leave:', error);
        // Errors here might not be visible to the user as the page is closing
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
      if (canControl) {
        updateDoc(jamDocRef, { isPlaying: true });
      }
    };
    const handlePause = () => {
      setIsPlayingLocally(false);
      if (canControl) {
        updateDoc(jamDocRef, { isPlaying: false });
      }
    };
    const handleTimeUpdate = () => {
      setCurrentPlaybackTime(audio.currentTime);
      // Only update Firestore if controlling and significant time difference
      if (canControl && Math.abs(audio.currentTime - (jamData?.currentTime || 0)) > 1) {
        updateDoc(jamDocRef, { currentTime: audio.currentTime });
      }
    };
    const handleEnded = async () => {
      setIsPlayingLocally(false);
      if (canControl && jamData && jamData.playlist && jamData.playlist.length > 0) {
        const nextSongIndex = (jamData.playlist.findIndex(s => s.url === jamData.currentSong?.url) + 1) % jamData.playlist.length;
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
  }, [jamDocRef, canControl, jamData]);


  // Add song to playlist
  const handleFileChange = async (event) => {
    const file = event.target.files[0];
    if (file && file.type === 'audio/mpeg') {
      try {
        const reader = new FileReader();
        reader.onload = async (e) => {
          const newSong = {
            id: Date.now().toString(), // Simple unique ID
            title: file.name,
            url: e.target.result, // Data URL
          };

          // Use a transaction to ensure atomicity for playlist updates
          await runTransaction(db, async (transaction) => {
            const jamDoc = await transaction.get(jamDocRef);
            if (!jamDoc.exists()) {
              throw "Jam document does not exist!";
            }
            const currentPlaylist = jamDoc.data().playlist || [];
            const updatedPlaylist = [...currentPlaylist, newSong];

            transaction.update(jamDocRef, { playlist: updatedPlaylist });

            // If no song is currently playing, set this as the current song and start playing
            if (!jamDoc.data().currentSong) {
              transaction.update(jamDocRef, {
                currentSong: newSong,
                currentTime: 0,
                isPlaying: true,
              });
            }
          });
          setFileInputKey(Date.now()); // Reset file input
        };
        reader.readAsDataURL(file);
      } catch (error) {
        console.error('Error reading file or adding to playlist:', error);
        setError('Failed to add song. Please try again.');
      }
    } else {
      setError('Please select an MP3 file.');
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
      setError("You don't have permission to change the song.");
    }
  };

  const togglePlayback = async () => {
    if (!jamData || !jamData.currentSong) {
      setError("No song loaded to play.");
      return;
    }
    if (canControl) {
      await updateDoc(jamDocRef, {
        isPlaying: !jamData.isPlaying,
      });
    } else {
      setError("You don't have permission to control playback.");
    }
  };

  const handleSeek = async (e) => {
    const newTime = parseFloat(e.target.value);
    audioRef.current.currentTime = newTime; // Update local immediately
    if (canControl) {
      await updateDoc(jamDocRef, { currentTime: newTime });
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
      setError("Only the host can change permissions.");
    }
  };

  const leaveJam = async () => {
    try {
      if (jamData.hostId === userId) {
        // If host is leaving, delete the jam room
        await deleteDoc(jamDocRef);
        console.log('Host left, jam room deleted.');
      } else {
        // If a regular user is leaving, remove them from the users map
        await updateDoc(jamDocRef, {
          [`users.${userId}`]: deleteDoc.FieldValue.delete(),
        });
        console.log('User left, removed from jam list.');
      }
    } catch (error) {
      console.error('Error leaving jam:', error);
      setError('Failed to leave jam cleanly.');
    } finally {
      setCurrentView('home');
      setCurrentJamId(null);
    }
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
                alert('Jam link copied to clipboard!');
              }).catch(err => {
                console.error('Failed to copy text: ', err);
                alert('Failed to copy link. Please copy it manually: ' + shareLink);
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
                <p className="text-xl font-semibold mb-2">{jamData.currentSong.title}</p>
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
            <label htmlFor="mp3-upload" className="inline-block bg-green-600 hover:bg-green-700 text-white font-bold py-3 px-6 rounded-md shadow-lg transition duration-300 ease-in-out transform hover:scale-105 cursor-pointer">
              <i className="fas fa-upload mr-2"></i> Upload MP3
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
            {error && <p className="text-red-400 mt-4">{error}</p>}
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

      {/* Tailwind CSS and Font Awesome CDN */}
      <script src="https://cdn.tailwindcss.com"></script>
      <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet" />
      <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/5.15.3/css/all.min.css" />
    </div>
  );
};

export default App;

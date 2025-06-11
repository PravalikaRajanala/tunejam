import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App'; // Import your main App component

// Get the root element from index.html
const container = document.getElementById('root');

// Create a root.
const root = createRoot(container);

// Initial render: Render the App component into the root.
root.render(<App />);

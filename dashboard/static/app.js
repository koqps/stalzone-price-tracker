// Ensure API base uses relative paths in production environments
const API_BASE = '';

/**
 * Universal helper to execute API requests cleanly against FastAPI
 * @param {string} endpoint - The relative API path (e.g., '/api/summary')
 * @param {object} options - Optional fetch configuration
 */
async function apiFetch(endpoint, options = {}) {
    // Ensure endpoint starts with a leading slash and strips any residual port placeholders
    let cleanEndpoint = endpoint.replace('__PORT_8420__', '').replace('//', '/');
    if (!cleanEndpoint.startsWith('/')) {
        cleanEndpoint = '/' + cleanEndpoint;
    }

    const url = `${API_BASE}${cleanEndpoint}`;

    try {
        const response = await fetch(url, {
            headers: {
                'Content-Type': 'application/json',
                ...options.headers
            },
            ...options
        });

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        return await response.json();
    } catch (error) {
        console.error(`API Fetch Error [${url}]:`, error);
        throw error;
    }
}

// Example usage throughout app.js:
// apiFetch('/api/summary?region=na')
// apiFetch('/api/valuations?region=na')
// apiFetch('/api/alerts?region=na&days=7')
// apiFetch('/api/community?region=na&days=7')
// apiFetch('/api/seed')

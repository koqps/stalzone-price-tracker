// Ensure API base uses relative paths in production environments
const API_BASE = '';

/**
 * Universal helper to execute API requests cleanly against FastAPI
 * @param {string} endpoint - The relative API path (e.g., '/api/summary')
 * @param {object} options - Optional fetch configuration
 */
async function apiFetch(endpoint, options = {}) {
    // Ensure endpoint starts with a leading slash and strips any residual port placeholders
    let cleanEndpoint = endpoint.replace('/__PORT_8420__', '').replace('__PORT_8420__', '').replace('//', '/');
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

// Data loaders using apiFetch

async function loadSummary(region = 'na') {
    try {
        const data = await apiFetch(`/api/summary?region=${region}`);
        if (typeof renderSummary === 'function') renderSummary(data);
        return data;
    } catch (err) {
        console.error("Failed to load summary:", err);
    }
}

async function loadValuations(region = 'na') {
    try {
        const data = await apiFetch(`/api/valuations?region=${region}`);
        if (typeof renderValuations === 'function') renderValuations(data);
        return data;
    } catch (err) {
        console.error("Failed to load valuations:", err);
    }
}

async function loadAlerts(region = 'na', days = 7) {
    try {
        const data = await apiFetch(`/api/alerts?region=${region}&days=${days}`);
        if (typeof renderAlerts === 'function') renderAlerts(data);
        return data;
    } catch (err) {
        console.error("Failed to load alerts:", err);
    }
}

async function loadCommunity(region = 'na', days = 7) {
    try {
        const data = await apiFetch(`/api/community?region=${region}&days=${days}`);
        if (typeof renderCommunity === 'function') renderCommunity(data);
        return data;
    } catch (err) {
        console.error("Failed to load community data:", err);
    }
}

async function triggerSeed() {
    try {
        const data = await apiFetch('/api/seed', { method: 'POST' });
        console.log("Seed triggered successfully:", data);
        return data;
    } catch (err) {
        console.error("Failed to trigger seed:", err);
    }
}

// Global initialization
document.addEventListener('DOMContentLoaded', () => {
    const region = new URLSearchParams(window.location.search).get('region') || 'na';
    loadSummary(region);
    loadValuations(region);
    loadAlerts(region);
    loadCommunity(region);
});

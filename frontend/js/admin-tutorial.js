/**
 * Admin Tutorial JavaScript
 * ========================
 * Loads and displays tutorial content from the API
 */

let tutorialData = null;
let examplesData = null;
let currentSection = 'quick-start';

// Initialize on page load
document.addEventListener('DOMContentLoaded', async () => {
    // Backend already authenticated user before serving this page
    await loadTutorial();
    await loadExamples();
    setupEventListeners();
});

// Setup event listeners
function setupEventListeners() {
    // Logout
    const logoutBtn = document.getElementById('logout-btn');
    if (logoutBtn) {
        logoutBtn.addEventListener('click', (e) => {
            // Defer to the ONE real logout (navbar-loader.js): it calls
            // POST /api/auth/logout, which revokes the token server-side, and
            // clears the cached navigation. This handler used to remove a
            // localStorage 'access_token' that is never written — the credential
            // is an HttpOnly cookie — so it redirected while leaving the session
            // valid.
            if (typeof window.handleLogout === 'function') {
                e.preventDefault();
                window.handleLogout();
            }
            // If navbar-loader has not loaded, fall through to the button's
            // default behaviour rather than faking a logout that did not happen.
        });
    }
}

// Load tutorial content
async function loadTutorial() {
    const loading = document.getElementById('loading');
    const error = document.getElementById('error');
    const content = document.getElementById('tutorial-content');

    try {
        const response = await fetch('/api/admin/tutorial', {
            credentials: 'include' // Include HttpOnly cookies
        });

        if (!response.ok) {
            throw new Error(`Failed to load tutorial: ${response.status}`);
        }

        tutorialData = await response.json();
        loading.style.display = 'none';
        content.style.display = 'block';
        renderTutorial();
    } catch (err) {
        loading.style.display = 'none';
        error.style.display = 'block';
        error.textContent = `Error loading tutorial: ${err.message}`;
        console.error('Error loading tutorial:', err);
    }
}

// Load API examples
async function loadExamples() {
    try {
        const response = await fetch('/api/admin/tutorial/examples', {
            credentials: 'include' // Include HttpOnly cookies
        });

        if (response.ok) {
            examplesData = await response.json();
        }
    } catch (err) {
        console.error('Error loading examples:', err);
    }
}

// Render tutorial content
function renderTutorial() {
    if (!tutorialData) return;

    const content = document.getElementById('tutorial-content');
    content.innerHTML = '';

    // Nav first, so every rendered section is guaranteed a way to reach it.
    renderNav();

    // Render quick start
    renderQuickStart(content);

    // Render sections
    tutorialData.sections.forEach(section => {
        const sectionDiv = document.createElement('div');
        sectionDiv.className = 'tutorial-section';
        sectionDiv.id = `section-${getSectionId(section.title)}`;
        sectionDiv.style.display = 'none';
        // Pairs with role="tab" + aria-controls on the nav buttons.
        sectionDiv.setAttribute('role', 'tabpanel');
        sectionDiv.setAttribute('tabindex', '0');

        sectionDiv.innerHTML = `
            <h2>${section.title}</h2>
            <p style="color: #666; font-size: 1.1em; margin-bottom: 20px;">${section.description}</p>
            <div class="tutorial-content">${formatMarkdown(section.content)}</div>
            ${renderExamples(section.examples)}
            ${renderAPIEndpoints(section.api_endpoints)}
        `;

        content.appendChild(sectionDiv);
    });

    // The region has finished populating; stop announcing it as busy.
    content.setAttribute('aria-busy', 'false');

    // Which build is serving this. Deliberately not awaited: the tutorial must
    // render whether or not the health endpoint answers.
    renderBuildInfo();

    // Show initial section
    showSection(currentSection);
}

// Render quick start
function renderQuickStart(container) {
    if (!tutorialData.quick_start) return;

    const quickStartDiv = document.createElement('div');
    quickStartDiv.className = 'quick-start';
    quickStartDiv.id = 'section-quick-start';
    quickStartDiv.style.display = 'none';

    let stepsHTML = '';
    if (tutorialData.quick_start.steps) {
        stepsHTML = tutorialData.quick_start.steps.map(step => `
            <div class="quick-start-step">
                <h4>Step ${step.step}: ${step.title}</h4>
                <p>${step.description}</p>
            </div>
        `).join('');
    }

    let workflowsHTML = '';
    if (tutorialData.quick_start.common_workflows) {
        workflowsHTML = '<h3>Common Workflows</h3>';
        workflowsHTML += tutorialData.quick_start.common_workflows.map(workflow => `
            <div class="example-card">
                <h4>${workflow.scenario}</h4>
                <ol>
                    ${workflow.steps.map(step => `<li>${step}</li>`).join('')}
                </ol>
            </div>
        `).join('');
    }

    quickStartDiv.innerHTML = `
        <h2>${tutorialData.quick_start.title}</h2>
        ${stepsHTML}
        ${workflowsHTML}
    `;

    container.appendChild(quickStartDiv);
}

// Render examples
function renderExamples(examples) {
    if (!examples || examples.length === 0) return '';

    return `
        <h3>Examples</h3>
        ${examples.map(example => `
            <div class="example-card">
                <h4>${example.title}</h4>
                <p>${example.description}</p>
                ${example.steps ? `
                    <ol>
                        ${example.steps.map(step => `<li>${step}</li>`).join('')}
                    </ol>
                ` : ''}
                ${example.code ? `
                    <pre><code>${escapeHtml(example.code)}</code></pre>
                ` : ''}
                ${example.api_example ? `
                    <div class="api-endpoint">
                        <span class="method">${example.api_example.method}</span>
                        <span class="path">${example.api_example.url}</span>
                        ${example.api_example.body ? `
                            <pre><code>${JSON.stringify(example.api_example.body, null, 2)}</code></pre>
                        ` : ''}
                    </div>
                ` : ''}
            </div>
        `).join('')}
    `;
}

// Render API endpoints
function renderAPIEndpoints(endpoints) {
    if (!endpoints || endpoints.length === 0) return '';

    return `
        <h3>API Endpoints</h3>
        ${endpoints.map(endpoint => `
            <div class="api-endpoint">
                <div>
                    <span class="method">${endpoint.method}</span>
                    <span class="path">${endpoint.path}</span>
                </div>
                <p>${endpoint.description}</p>
                ${endpoint.parameters ? `
                    <p><strong>Parameters:</strong></p>
                    <ul>
                        ${Object.entries(endpoint.parameters).map(([key, value]) => 
                            `<li><code>${key}</code>: ${value}</li>`
                        ).join('')}
                    </ul>
                ` : ''}
                ${endpoint.body ? `
                    <p><strong>Request Body:</strong></p>
                    <pre><code>${JSON.stringify(endpoint.body, null, 2)}</code></pre>
                ` : ''}
                ${endpoint.response ? `
                    <p><strong>Response:</strong></p>
                    <pre><code>${JSON.stringify(endpoint.response, null, 2)}</code></pre>
                ` : ''}
            </div>
        `).join('')}
    `;
}

// Show specific section
function showSection(sectionId) {
    currentSection = sectionId;

    // Mark the button that owns this section, rather than whatever the global
    // `event` happened to be. The old form relied on the implicit window.event
    // and so highlighted nothing when called programmatically (including the
    // initial render) and the wrong thing when the click landed on a child.
    document.querySelectorAll('.tutorial-nav button').forEach(btn => {
        const current = btn.dataset.section === sectionId;
        btn.classList.toggle('active', current);
        btn.setAttribute('aria-selected', current ? 'true' : 'false');
    });

    // Hide all sections
    document.querySelectorAll('.tutorial-section, .quick-start').forEach(section => {
        section.style.display = 'none';
    });

    // Show selected section
    const section = document.getElementById(`section-${sectionId}`);
    if (section) {
        section.style.display = 'block';
        section.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
}

// Get section ID from title
//
// The short ids exist only so the legacy deep links (#authentication, #merge,
// ...) keep working. Anything not listed falls back to a slug of the title —
// and the nav is now built from the API response rather than hardcoded in the
// HTML, so a new backend section always gets a working button.
//
// The old hardcoded nav had drifted badly: five of thirteen sections had no
// button at all (including "Platform Hardening: What Changed", which the
// documentation index tells administrators to read), and two buttons pointed
// at ids no section produced. Deriving both sides from one function is what
// stops that recurring.
function getSectionId(title) {
    const map = {
        'API Authentication': 'authentication',
        'Understanding Unknown Faces': 'understanding',
        'Promoting Unknown to Known': 'promote',
        'Merging Identities': 'merge',
        'Quick Search': 'search',
        'Advanced Search': 'advanced-search',
        'System Settings Management': 'settings',
        'System Workflow': 'workflow',
        'Advanced SNA Features': 'advanced-sna'
    };
    // Punctuation must go, not just whitespace: "Platform Hardening: What
    // Changed" produced the id "platform-hardening:-what-changed", which is
    // not a valid CSS selector fragment and broke getElementById lookups.
    return map[title] || title
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '');
}

// Build the nav from the sections the API actually returned.
function renderNav() {
    const nav = document.querySelector('.tutorial-nav');
    if (!nav || !tutorialData) return;

    const items = [{ id: 'quick-start', label: 'Quick Start' }];
    tutorialData.sections.forEach(section => {
        items.push({ id: getSectionId(section.title), label: navLabel(section.title) });
    });

    nav.innerHTML = '';
    items.forEach((item, index) => {
        const button = document.createElement('button');
        button.textContent = item.label;
        button.title = item.label;
        button.dataset.action = 'showSection';
        button.dataset.section = item.id;
        // The container is role="tablist"; without role="tab" on the children
        // a screen reader announces a bare row of buttons and never says which
        // one is current.
        button.setAttribute('role', 'tab');
        button.setAttribute('aria-selected', index === 0 ? 'true' : 'false');
        button.setAttribute('aria-controls', `section-${item.id}`);
        if (index === 0) button.classList.add('active');
        nav.appendChild(button);
    });
}

// Which build is serving this tutorial.
//
// The content comes from the API, so it is by construction the running build's
// copy — but "always matches the running build" is a claim the reader cannot
// check. The runtime fingerprint on /health/detailed makes it checkable.
// Best-effort: a failure here must never block the tutorial.
async function renderBuildInfo() {
    const wrapper = document.getElementById('tutorial-build');
    const text = document.getElementById('tutorial-build-text');
    if (!wrapper || !text) return;
    try {
        const response = await fetch('/health/detailed', { credentials: 'same-origin' });
        if (!response.ok) return;
        const runtime = (await response.json()).runtime;
        if (!runtime) return;
        const commit = runtime.git_commit && runtime.git_commit !== 'unknown'
            ? ` · ${runtime.git_commit}` : '';
        text.textContent =
            `This tutorial is served by the running build: v${runtime.version}${commit}` +
            ` · ${runtime.environment}`;
        wrapper.hidden = false;
    } catch (err) {
        // Offline or health endpoint unavailable — the tutorial still works.
    }
}

// Long titles make an unusable button row; keep the label short but honest.
function navLabel(title) {
    const shortened = {
        'Advanced Search Intelligence': 'Advanced Search',
        'Live Alerts and Watchlists for Unknown Persons': 'Live Alerts & Watchlists',
        'System Settings Management': 'System Settings',
        'Complete Configuration Guide': 'Configuration',
        'Platform Hardening: What Changed': 'Platform Hardening',
        'Operating This System: Docs, Drift and the API Reference': 'Operating This System',
        'Promoting Unknown to Known': 'Promote to Known',
        'Merging Identities': 'Merge Identities',
        'Understanding Unknown Faces': 'Unknown Faces'
    };
    return shortened[title] || title;
}

// Format markdown (simple implementation)
function formatMarkdown(text) {
    if (!text) return '';
    
    // Convert markdown headers
    text = text.replace(/^### (.*$)/gim, '<h3>$1</h3>');
    text = text.replace(/^## (.*$)/gim, '<h2>$1</h2>');
    text = text.replace(/^# (.*$)/gim, '<h1>$1</h1>');
    
    // Convert code blocks
    text = text.replace(/```([\s\S]*?)```/g, '<pre><code>$1</code></pre>');
    
    // Convert inline code
    text = text.replace(/`([^`]+)`/g, '<code>$1</code>');
    
    // Convert bold
    text = text.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
    
    // Convert line breaks
    text = text.replace(/\n\n/g, '</p><p>');
    text = '<p>' + text + '</p>';
    
    return text;
}

// Escape HTML
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Show examples section
function showExamplesSection() {
    if (!examplesData) {
        alert('Examples not loaded yet. Please wait...');
        return;
    }

    const content = document.getElementById('tutorial-content');
    let html = '<div class="tutorial-section" id="section-examples"><h2>API Code Examples</h2>';
    html += '<p style="color: #666; margin-bottom: 20px;">Code examples for all admin API endpoints in multiple languages</p>';

    Object.entries(examplesData.examples).forEach(([key, example]) => {
        html += `
            <div class="example-card">
                <h3>${example.title}</h3>
                <div class="code-tabs">
                    <button class="code-tab active" data-action="showCodeTab" data-arg="${key}-curl">cURL</button>
                    <button class="code-tab" data-action="showCodeTab" data-arg="${key}-js">JavaScript</button>
                    <button class="code-tab" data-action="showCodeTab" data-arg="${key}-py">Python</button>
                </div>
                <div id="${key}-curl" class="code-content active">
                    <pre><code>${escapeHtml(example.curl)}</code></pre>
                </div>
                <div id="${key}-js" class="code-content">
                    <pre><code>${escapeHtml(example.javascript)}</code></pre>
                </div>
                <div id="${key}-py" class="code-content">
                    <pre><code>${escapeHtml(example.python)}</code></pre>
                </div>
            </div>
        `;
    });

    html += '</div>';
    content.innerHTML = html;
    
    // Hide all sections and show examples
    document.querySelectorAll('.tutorial-section, .quick-start').forEach(section => {
        section.style.display = 'none';
    });
    const examplesSection = document.getElementById('section-examples');
    if (examplesSection) {
        examplesSection.style.display = 'block';
    }
}

// Show code tab
function showCodeTab(button, tabId) {
    // Update tabs
    button.parentElement.querySelectorAll('.code-tab').forEach(tab => {
        tab.classList.remove('active');
    });
    button.classList.add('active');

    // Update content
    const container = button.closest('.example-card');
    container.querySelectorAll('.code-content').forEach(content => {
        content.classList.remove('active');
    });
    document.getElementById(tabId).classList.add('active');
}

// Setup nav button click handlers
function setupNavButtons() {
    document.querySelectorAll('.tutorial-nav button').forEach((btn, index) => {
        btn.addEventListener('click', function() {
            const sections = ['quick-start', 'authentication', 'understanding', 'promote', 'merge', 'search', 'settings', 'workflow', 'advanced-sna', 'examples'];
            const sectionId = sections[index];
            
            if (sectionId === 'examples') {
                showExamplesSection();
            } else {
                showSection(sectionId);
            }
            
            // Update active button
            document.querySelectorAll('.tutorial-nav button').forEach(b => b.classList.remove('active'));
            this.classList.add('active');
        });
    });
}

// Call setup after tutorial is loaded
setTimeout(setupNavButtons, 500);



// ---------------------------------------------------------------------------
// CSP-safe event registration
//
// These handlers were previously reached through inline onclick/onchange/
// onsubmit attributes, which `script-src 'self'` blocks. Registered rather
// than looked up on window, so only these names are invocable; delegated in
// actions.js, so dynamically rendered elements work without rebinding.
// ---------------------------------------------------------------------------
Actions.register({
    showSection: (el) => showSection(el.dataset.section),
    // showCodeTab took the clicked element as its first argument; delegation
    // supplies it, so the DOM lookup it used to rely on is unchanged.
    showCodeTab: (el) => showCodeTab(el, el.dataset.arg),
});

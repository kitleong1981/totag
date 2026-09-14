/**
 * ToTag Metadata Manager - Frontend Application
 */

// State
let currentFolder = null;
let selectedFolders = [];
let lastSelectedFolderIndex = -1;
let selectedFiles = [];
let allFiles = [];
let rawFiles = [];
let filterText = '';
let isSearchMode = false;
let searchDebounceTimer = null;
let searchPollInterval = null;
let currentSort = 'capture';
let dirtyCount = 0;
let dirtyPathSet = new Set();
let metadataSummaryCache = new Map();
let sortRequestSeq = 0;
let metadataRequestSeq = 0;
let isPendingSyncView = false;
let activeTab = 'metadata'; // 'metadata' | 'export'
let draggedFilePaths = []; // paths being dragged (global — more reliable than dataTransfer for same-page drags)

// Store original metadata values to detect changes
let originalMetadata = {
    title: '',
    description: '',
    keywords: '',
};

// Track if form has unsaved changes (UI dirty state)
let formHasChanges = false;

// Idle pre-fetch state
let idleTimer = null;
let isUserWorking = false;
let prefetchedFolders = new Set();
const IDLE_DELAY = 5000; // 5 seconds of idle before pre-fetch starts

// Debug flag - set to true to see prefetch logs in console
const DEBUG_PREFETCH = true;
function debugLog(msg) {
    if (DEBUG_PREFETCH) {
        console.log(`[PREFETCH] ${msg}`);
    }
}

function fileIsDirty(file) {
    return !!file && (file.is_dirty || dirtyPathSet.has(file.path));
}

function syncDirtyFlagsForFiles(files) {
    files.forEach(file => {
        file.is_dirty = dirtyPathSet.has(file.path);
    });
}

function applyDirtyClassesToVisibleThumbnails() {
    const dirtyStates = new Map();
    [...rawFiles, ...allFiles, ...visibleFiles].forEach(file => {
        if (file?.path) dirtyStates.set(file.path, fileIsDirty(file));
    });

    document.querySelectorAll('.thumbnail-item[data-path]').forEach(el => {
        const path = el.dataset.path;
        el.classList.toggle('dirty', dirtyPathSet.has(path) || dirtyStates.get(path) === true);
    });
}

function normalizeKeywordsForMissingCheck(keywords) {
    if (Array.isArray(keywords)) return keywords;
    if (typeof keywords !== 'string') return [];
    const trimmed = keywords.trim();
    if (!trimmed || trimmed === '[]') return [];
    try {
        const parsed = JSON.parse(trimmed);
        if (Array.isArray(parsed)) return parsed;
    } catch (e) {
        // Plain comma-separated keywords are valid metadata.
    }
    return trimmed.split(',').map(k => k.trim()).filter(Boolean);
}

function fileMissingMetadata(file) {
    if (!file) return false;
    const cached = metadataSummaryCache.get(file.path);
    const source = cached || file;
    const hasTitle = !!String(source.title || '').trim();
    const hasDescription = !!String(source.description || '').trim();
    const hasKeywords = normalizeKeywordsForMissingCheck(source.keywords).length > 0;
    return !hasTitle || !hasDescription || !hasKeywords;
}

function syncMissingMetadataFlags(files) {
    files.forEach(file => {
        applyMetadataSummaryCache(file);
        file.missing_metadata = fileMissingMetadata(file);
    });
}

function applyMetadataSummaryCache(file) {
    const summary = file?.path ? metadataSummaryCache.get(file.path) : null;
    if (!summary) return file;
    file.title = summary.title || '';
    file.description = summary.description || '';
    file.keywords = summary.keywords || [];
    file.is_dirty = !!summary.is_dirty;
    return file;
}

function setRawFiles(files) {
    rawFiles = (files || []).map(file => applyMetadataSummaryCache(file));
    syncMissingMetadataFlags(rawFiles);
}

function mergeFileUpdates(files) {
    const incoming = new Map((files || []).map(file => [file.path, file]));
    if (rawFiles.length === 0) {
        setRawFiles(files || []);
        applyFilter();
        return;
    }

    rawFiles.forEach(file => {
        const fresh = incoming.get(file.path);
        if (!fresh) return;
        Object.assign(file, fresh);
        applyMetadataSummaryCache(file);
        file.missing_metadata = fileMissingMetadata(file);
    });
    syncMissingMetadataFlags(rawFiles);
    applyFilter();
}

async function refreshMetadataSummaries(paths) {
    const uniquePaths = [...new Set(paths || [])].filter(Boolean);
    if (uniquePaths.length === 0) return;

    const response = await fetch('/api/metadata/summaries', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths: uniquePaths }),
    });
    const data = await response.json();
    const summaries = data.files || [];
    const lists = [rawFiles, allFiles, visibleFiles];

    summaries.forEach(summary => {
        metadataSummaryCache.set(summary.path, {
            title: summary.title || '',
            description: summary.description || '',
            keywords: summary.keywords || [],
            is_dirty: !!summary.is_dirty,
        });
        lists.forEach(files => {
            const file = files.find(f => f.path === summary.path);
            if (!file) return;
            applyMetadataSummaryCache(file);
            file.missing_metadata = fileMissingMetadata(file);
        });
        if (summary.is_dirty) dirtyPathSet.add(summary.path);
    });
}

// For Shift+click range selection
let lastSelectedIndex = -1;

// Batch generation cancellation flag
let batchCancelRequested = false;
let isBatchGenerating = false;
let batchQueue = []; // {path, location_override}
let multiSelectMode = false;
let activeJobs = new Map();
let activeJobStreams = new Map();
let liveDirtyJobPaths = new Map();
let selectedJobId = null;
const appliedJobIds = new Set(JSON.parse(localStorage.getItem('totag_applied_job_ids') || '[]'));

// For infinite scroll pagination
let visibleFiles = [];
const FILES_PER_BATCH = 40;
let currentPage = 0;
let isLoadingMore = false;
let renderQueue = [];
let isRendering = false;

// For background polling
let backgroundPollInterval = null;

function formatDateCompact(date) {
    if (!date || isNaN(date.getTime())) return 'N/A';
    const h = String(date.getHours()).padStart(2, '0');
    const min = String(date.getMinutes()).padStart(2, '0');
    return `${date.getFullYear()}/${date.getMonth() + 1}/${date.getDate()} ${h}:${min}`;
}

function selectFirstFile() {
    if (allFiles.length === 0) return;
    const first = allFiles[0];
    handleThumbnailClick({ shiftKey: false, ctrlKey: false, metaKey: false }, first.path, 0);
    const el = document.querySelector('.thumbnail-item[data-index="0"]');
    if (el) el.scrollIntoView({ block: 'nearest' });
}

function showSortBar() {
    const bar = document.getElementById('sort-bar');
    if (bar) {
        bar.style.display = 'flex';
        bar.querySelectorAll('.sort-btn').forEach(b => b.classList.toggle('active', b.dataset.sort === currentSort));
    }
}
function hideSortBar() {
    const bar = document.getElementById('sort-bar');
    if (bar) bar.style.display = 'none';
}


async function setSort(mode) {
    const requestId = ++sortRequestSeq;
    currentSort = mode;
    document.querySelectorAll('.sort-btn').forEach(b => b.classList.toggle('active', b.dataset.sort === mode));
    if (mode === 'missing') {
        await refreshMetadataSummaries(rawFiles.map(f => f.path));
        if (requestId !== sortRequestSeq || currentSort !== mode) return;
    }
    applySort();
}


function applySort() {
    syncMissingMetadataFlags(rawFiles);
    if (currentSort === 'filename') {
        rawFiles.sort((a, b) => a.filename.localeCompare(b.filename));
    } else if (currentSort === 'missing') {
        rawFiles.sort((a, b) => {
            const am = fileMissingMetadata(a) ? 0 : 1;
            const bm = fileMissingMetadata(b) ? 0 : 1;
            if (am !== bm) return am - bm;
            return (a.capture_date || '9999').localeCompare(b.capture_date || '9999');
        });
    } else {
        rawFiles.sort((a, b) => (a.capture_date || '9999').localeCompare(b.capture_date || '9999'));
    }
    applyFilter();
    visibleFiles = [];
    renderThumbnailsProgressive();
}

function applyFilter() {
    allFiles = rawFiles.slice();
    syncMissingMetadataFlags(allFiles);
}

function setFilter(text) {
    filterText = text;
    const clearBtn = document.getElementById('filter-clear-btn');
    if (clearBtn) clearBtn.style.display = text ? 'flex' : 'none';

    clearTimeout(searchDebounceTimer);
    if (!text.trim()) {
        clearSearch(false);
        return;
    }
    searchDebounceTimer = setTimeout(() => runSearch(text.trim()), 400);
}

async function runSearch(q) {
    if (q.length < 2) return;
    isSearchMode = true;
    hideSortBar();
    thumbnailGrid.innerHTML = '<div class="loading">Searching…</div>';
    fileCountEl.textContent = '';
    selectedFiles = [];

    try {
        const res = await fetch(`/api/search?q=${encodeURIComponent(q)}&tab=${activeTab}`);
        const data = await res.json();
        setRawFiles(data.files || []);
        allFiles = rawFiles.slice();
        renderSearchResults();
        setStatus(`${allFiles.length} result${allFiles.length !== 1 ? 's' : ''} for "${q}"`, false);
        startThumbnailPoll(
            () => fetch(`/api/search?q=${encodeURIComponent(q)}&tab=${activeTab}`).then(r => r.json()).then(d => d.files || []),
            () => isSearchMode && filterText.trim() === q
        );
    } catch (err) {
        setStatus(`Search error: ${err.message}`, false);
        thumbnailGrid.innerHTML = '';
    }
}

function startThumbnailPoll(fetchFn, guardFn) {
    clearInterval(searchPollInterval);
    searchPollInterval = null;
    if (!rawFiles.some(f => !f.thumb_url)) return;

    searchPollInterval = setInterval(async () => {
        if (!guardFn()) {
            clearInterval(searchPollInterval);
            searchPollInterval = null;
            return;
        }
        try {
            const freshFiles = await fetchFn();
            freshFiles.forEach(fresh => {
                if (!fresh.thumb_url) return;
                const existing = rawFiles.find(f => f.path === fresh.path);
                if (existing && !existing.thumb_url) {
                    existing.thumb_url = fresh.thumb_url;
                    const imgEl = thumbnailGrid.querySelector(`.thumbnail-item[data-path="${CSS.escape(fresh.path)}"] img`);
                    if (imgEl) {
                        imgEl.src = `/thumbs/${fresh.thumb_url.split('/').pop()}`;
                        delete imgEl.dataset.pending;
                    }
                }
            });
            if (!rawFiles.some(f => !f.thumb_url)) {
                clearInterval(searchPollInterval);
                searchPollInterval = null;
            }
        } catch (e) { /* ignore poll errors */ }
    }, 3000);
}

function clearSearch(clearInput = true) {
    isSearchMode = false;
    filterText = '';
    clearTimeout(searchDebounceTimer);
    clearInterval(searchPollInterval);
    searchPollInterval = null;
    if (clearInput) {
        const inp = document.getElementById('filter-input');
        if (inp) inp.value = '';
    }
    const clearBtn = document.getElementById('filter-clear-btn');
    if (clearBtn) clearBtn.style.display = 'none';
    renderQuickSearchChips();

    currentFolder = null;
    selectedFolders = [];
    selectedFiles = [];
    rawFiles = [];
    allFiles = [];
    visibleFiles = [];
    document.querySelectorAll('.folder-item').forEach(el => el.classList.remove('active'));
    thumbnailGrid.innerHTML = '';
    currentFolderEl.textContent = 'Select a folder';
    currentFolderEl.title = '';
    fileCountEl.textContent = '';
    metadataForm.style.display = 'none';
    metadataContent.style.display = 'flex';
    metadataContent.innerHTML = '<div class="no-selection"><p>Select files to view and edit metadata</p></div>';
    hideSortBar();
    setStatus('', false);
    updateActionButtonStates();
}

function renderSearchResults() {
    thumbnailGrid.innerHTML = '';
    visibleFiles = [];

    if (allFiles.length === 0) {
        thumbnailGrid.innerHTML = '<div class="no-results">No matching files found</div>';
        fileCountEl.textContent = '0 results';
        updateSelectAllBtn();
        return;
    }

    const fragment = document.createDocumentFragment();
    let lastFolder = null;

    allFiles.forEach((file, i) => {
        if (file.folder !== lastFolder) {
            lastFolder = file.folder;
            const header = document.createElement('div');
            header.className = 'search-folder-header';
            header.textContent = file.folder.split('/').pop() || file.folder;
            header.title = file.folder;
            header.addEventListener('click', () => selectFolder(file.folder));
            fragment.appendChild(header);
        }

        const itemEl = document.createElement('div');
        itemEl.className = 'thumbnail-item';
        itemEl.dataset.path = file.path;
        itemEl.dataset.index = i;
        if (fileIsDirty(file)) itemEl.classList.add('dirty');
        if (selectedFiles.includes(file.path)) itemEl.classList.add('selected');

        const imgSrc = file.thumb_url ? `/thumbs/${file.thumb_url.split('/').pop()}` : '';
        itemEl.innerHTML = `
            <img src="${imgSrc || ''}" alt="${file.filename}" loading="lazy" draggable="false">
            <div class="filename">${file.filename}</div>
        `;

        itemEl.draggable = true;
        itemEl.addEventListener('click', (e) => {
            markUserWorking();
            handleThumbnailClick(e, file.path, i);
        });
        itemEl.addEventListener('dblclick', () => openInFinder(file.path));

        fragment.appendChild(itemEl);
        visibleFiles.push(file);
    });

    thumbnailGrid.appendChild(fragment);
    applyDirtyClassesToVisibleThumbnails();
    fileCountEl.textContent = `${allFiles.length} result${allFiles.length !== 1 ? 's' : ''}`;
    updateSelectAllBtn();
}

// Poll background status endpoint and update status bar
function pollBackgroundStatus() {
    fetch('/api/background-status')
        .then(res => res.json())
        .then(data => {
            if (data.metadata_loading || data.thumbs_loading) {
                setBackgroundStatus(data.status_text, true);

                // Show real progress — prefer whichever bulk job is currently active
                const bgBar = document.getElementById('status-bg-progress')?.querySelector('.progress-bar');
                if (bgBar) {
                    let pct = null;
                    if (data.bulk_meta_active && data.bulk_meta_total > 0) {
                        pct = Math.round((data.bulk_meta_done / data.bulk_meta_total) * 100);
                    } else if (data.bulk_thumb_active && data.bulk_thumb_total > 0) {
                        pct = Math.round((data.bulk_thumb_done / data.bulk_thumb_total) * 100);
                    }
                    if (pct !== null) {
                        bgBar.classList.add('has-value');
                        bgBar.style.setProperty('--progress', pct + '%');
                    } else {
                        bgBar.classList.remove('has-value');
                        bgBar.style.removeProperty('--progress');
                    }
                }
            } else {
                setBackgroundStatus('Idle', false);
                const bgBar = document.getElementById('status-bg-progress')?.querySelector('.progress-bar');
                if (bgBar) {
                    bgBar.classList.remove('has-value');
                    bgBar.style.removeProperty('--progress');
                }
            }
        })
        .catch(() => {
            // Silently ignore polling errors
        });
}

// Trigger the server-side bulk metadata loader (idempotent — safe to call anytime)
function triggerBulkMetadataLoad() {
    fetch('/api/metadata/load-all-missing', { method: 'POST' })
        .then(r => r.json())
        .then(d => {
            if (d.started) console.log('[bulk-meta] Started background metadata load for all missing folders');
        })
        .catch(() => {});
}

// Trigger the server-side bulk thumbnail generator (idempotent — safe to call anytime)
function triggerBulkThumbnailGeneration() {
    fetch('/api/thumbnails/generate-all-missing', { method: 'POST' })
        .then(r => r.json())
        .then(d => {
            if (d.started) console.log('[bulk-thumb] Started background thumbnail generation');
        })
        .catch(() => {});
}

// DOM Elements
const folderList = document.getElementById('folder-list');
const thumbnailGrid = document.getElementById('thumbnail-grid');
const currentFolderEl = document.getElementById('current-folder');
const fileCountEl = document.getElementById('file-count');
const metadataContent = document.getElementById('metadata-content');
const metadataForm = document.getElementById('metadata-form');
const selectedCountEl = document.getElementById('selected-count');
const metaTitle = document.getElementById('meta-title');
const metaDescription = document.getElementById('meta-description');
const metaKeywords = document.getElementById('meta-keywords');
const keywordAppend = document.getElementById('keyword-append');
const statusBar = document.getElementById('status-bar');
const statusText = document.getElementById('status-text');
const statusProgress = document.getElementById('status-progress');

// Status bar helpers
function setStatus(message, isLoading = false) {
    statusText.textContent = message;
    statusProgress.style.display = isLoading ? 'block' : 'none';
}

function clearStatus() {
    setStatus('Ready', false);
}

function setBackgroundStatus(message, isLoading = false) {
    const bgText = document.getElementById('status-bg-text');
    const bgProgress = document.getElementById('status-bg-progress');
    bgText.textContent = `Background: ${message}`;
    bgProgress.style.display = isLoading ? 'block' : 'none';
}

function clearBackgroundStatus() {
    setBackgroundStatus('Idle', false);
}

function setJobsStatus(message, isLoading = false, progress = null) {
    const text = document.getElementById('status-jobs-text');
    const wrap = document.getElementById('status-jobs-progress');
    const bar = wrap?.querySelector('.progress-bar');
    if (!text || !wrap || !bar) return;
    text.textContent = `${window.innerWidth < 700 ? '' : 'Jobs: '}${compactJobMessage(message)}`;
    wrap.style.display = isLoading ? 'block' : 'none';
    if (progress !== null && progress !== undefined) {
        bar.classList.add('has-value');
        bar.style.setProperty('--progress', `${Math.max(0, Math.min(100, progress))}%`);
    } else {
        bar.classList.remove('has-value');
        bar.style.removeProperty('--progress');
    }
}

function compactJobMessage(message) {
    if (window.innerWidth >= 700) return message;
    return message
        .replace(/Metadata generation/g, 'Meta')
        .replace(/Art metadata generation/g, 'Art meta')
        .replace(/ file\(s\)/g, '')
        .replace(/ files\)/g, ')')
        .replace(/ failed/g, ' fail')
        .replace(/complete/g, 'done');
}

function parseJobTimestamp(value) {
    if (!value) return null;
    const time = Date.parse(value);
    return Number.isFinite(time) ? time : null;
}

function formatShortElapsed(ms) {
    if (!Number.isFinite(ms) || ms < 0) return '';
    const seconds = Math.floor(ms / 1000);
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    return `${hours}h ${minutes % 60}m`;
}

function currentJobElapsed(job) {
    const started = parseJobTimestamp(job?.updated_at || job?.started_at);
    if (!started || job?.status !== 'running') return '';
    return formatShortElapsed(Date.now() - started);
}

function summarizeJob(job) {
    const label = job.label || job.type || 'Job';
    const total = job.total || 0;
    const done = job.done || 0;
    const failed = job.failed || 0;
    const elapsed = currentJobElapsed(job);
    const elapsedText = elapsed ? ` (${elapsed})` : '';
    const current = job.current_label ? ` — ${job.current_label}${elapsedText}` : elapsedText;
    if (total > 0 && ['queued', 'running'].includes(job.status)) {
        return `${label}: ${done + failed}/${total}${failed ? `, ${failed} failed` : ''}${current}`;
    }
    if (job.status === 'done') return `${label}: complete`;
    if (job.status === 'failed') return `${label}: ${done} done, ${failed} failed`;
    if (job.status === 'cancelled') return `${label}: cancelled`;
    return `${label}: ${job.status}`;
}

// Reflects AI generation progress where the user is actually looking —
// the thumbnail being worked on and the metadata panel — instead of only
// the small status text at the bottom of the page. Driven entirely by job
// state (activeJobs), so it self-corrects on every SSE tick regardless of
// how a job was started (single-file button, Cmd+G, queued batch).
function updateGeneratingIndicators() {
    // 'queued' counts too — a file waiting behind an already-running batch
    // is not done yet, and the button/banner should stay busy for it.
    const genJobs = [...activeJobs.values()].filter(
        j => j.type === 'metadata_generate' && ['queued', 'running'].includes(j.status)
    );
    const runningGenJob = genJobs.find(j => j.status === 'running');

    document.querySelectorAll('.thumbnail-item.generating-file').forEach(el => {
        el.classList.remove('generating-file');
    });
    if (runningGenJob && runningGenJob.current_label) {
        document.querySelectorAll('.thumbnail-item').forEach(el => {
            const path = el.dataset.path || '';
            if (path.split('/').pop() === runningGenJob.current_label) {
                el.classList.add('generating-file');
            }
        });
    }

    const genBtn = document.getElementById('gen-meta-btn');
    const banner = document.getElementById('gen-status-banner');
    const bannerText = document.getElementById('gen-status-text');
    const allJobPaths = new Set(genJobs.flatMap(j => (j.payload && j.payload.paths) || []));
    const selectionIsGenerating = genJobs.length > 0 && selectedFiles.some(p => allJobPaths.has(p));

    if (genBtn) {
        genBtn.classList.toggle('generating', selectionIsGenerating);
        genBtn.disabled = selectionIsGenerating;
    }
    if (banner) {
        banner.style.display = selectionIsGenerating ? 'flex' : 'none';
        if (selectionIsGenerating && bannerText) {
            if (runningGenJob) {
                const doneCount = (runningGenJob.done || 0) + (runningGenJob.failed || 0);
                const total = runningGenJob.total || allJobPaths.size;
                const progressSuffix = total > 1 ? ` (${doneCount}/${total})` : '';
                bannerText.textContent = `Generating metadata with AI${progressSuffix} — usually 5–30s, longer for video or a busy local model…`;
            } else {
                bannerText.textContent = 'Queued for AI generation — waiting for the current batch to reach this file…';
            }
        }
    }
}

function renderJobsStatus() {
    updateGeneratingIndicators();
    const jobs = sortedJobsForDisplay();
    const running = jobs.filter(j => ['queued', 'running'].includes(j.status));
    if (running.length === 0) {
        const latest = jobs.find(j => ['done', 'failed', 'cancelled'].includes(j.status));
        if (latest) {
            setJobsStatus(summarizeJob(latest), false, latest.progress);
        } else {
            setJobsStatus('Idle', false);
        }
        document.getElementById('cancel-batch-btn').style.display = 'none';
        selectedJobId = null;
        return;
    }

    const primary = running[0];
    selectedJobId = primary.id;
    setJobsStatus(
        running.length > 1 ? `${summarizeJob(primary)} | +${running.length - 1} more` : summarizeJob(primary),
        true,
        primary.progress
    );
    document.getElementById('cancel-batch-btn').style.display = 'inline-block';
}

function sortedJobsForDisplay() {
    return [...activeJobs.values()].sort((a, b) => {
        const rank = { running: 0, queued: 1, done: 2, failed: 2, cancelled: 2 };
        const ar = rank[a.status] ?? 3;
        const br = rank[b.status] ?? 3;
        if (ar !== br) return ar - br;
        if (ar <= 1) return String(a.created_at || '').localeCompare(String(b.created_at || ''));
        return String(b.updated_at || b.finished_at || b.created_at || '').localeCompare(String(a.updated_at || a.finished_at || a.created_at || ''));
    });
}

async function openJobLog(jobId = null) {
    await refreshActiveJobs();
    const jobs = sortedJobsForDisplay();
    const job = jobId ? activeJobs.get(jobId) : (selectedJobId ? activeJobs.get(selectedJobId) : jobs[0]);
    if (!job) return;

    const log = document.getElementById('export-log');
    const title = document.getElementById('export-log-title');
    const body = document.getElementById('export-log-body');
    if (!log || !title || !body) return;

    if (log.parentElement !== document.body) document.body.appendChild(log);
    title.textContent = 'Job Queue';
    body.textContent = '';
    log.style.display = 'flex';
    log.dataset.jobId = job.id;

    try {
        body.textContent += renderJobQueueList(jobs, job.id);
        body.textContent += '\n\n';
        const r = await fetch(`/api/jobs/${encodeURIComponent(job.id)}/events`);
        const d = await r.json();
        body.textContent += `Events — ${job.label || job.type || job.id}\n`;
        body.textContent += '----------------------------------------\n';
        (d.events || []).forEach(ev => {
            const prefix = ev.level && ev.level !== 'info' ? `[${ev.level}] ` : '';
            body.textContent += `${prefix}${ev.message}\n`;
        });
        body.scrollTop = body.scrollHeight;
    } catch (e) {
        body.textContent = `Could not load job log: ${e.message}`;
    }
}

function renderJobQueueList(jobs, activeJobId) {
    const lines = ['Queue'];
    const interestingJobs = jobs.filter(job => ['queued', 'running', 'done', 'failed', 'cancelled'].includes(job.status)).slice(0, 8);
    interestingJobs.forEach((job, index) => {
        const marker = job.id === activeJobId ? '>' : ' ';
        const icon = job.status === 'done' ? 'done' : job.status === 'failed' ? 'fail' : job.status === 'cancelled' ? 'cancel' : job.status === 'running' ? 'run' : 'wait';
        lines.push(`${marker} ${icon} ${index + 1}. ${summarizeJob(job)}`);
        if (job.id === activeJobId) lines.push(...renderJobFileQueue(job));
    });
    return lines.join('\n');
}

function renderJobFileQueue(job) {
    const paths = job.payload?.paths || [];
    if (!paths.length) {
        const folderPath = job.payload?.folder_path;
        return folderPath ? [`    folder: ${folderPath.split('/').pop()}`] : [];
    }
    const doneCount = (job.done || 0) + (job.failed || 0);
    const currentName = job.current_label || '';
    const currentIndex = currentName ? paths.findIndex(path => path.split('/').pop() === currentName) : -1;
    const visibleIndexes = new Set();
    for (let i = Math.max(0, doneCount - 3); i < Math.min(paths.length, doneCount + 8); i++) visibleIndexes.add(i);
    if (currentIndex >= 0) {
        for (let i = Math.max(0, currentIndex - 3); i < Math.min(paths.length, currentIndex + 8); i++) visibleIndexes.add(i);
    }
    return [...visibleIndexes].sort((a, b) => a - b).map(i => {
        const name = paths[i].split('/').pop();
        const isCurrent = name === currentName && job.status === 'running';
        const tick = i < doneCount ? '[x]' : isCurrent ? '[>]' : '[ ]';
        return `    ${tick} ${i + 1}/${paths.length} ${name}`;
    });
}

function markFilesDirty(paths, refresh = true) {
    paths.forEach(path => {
        dirtyPathSet.add(path);
        const file = allFiles.find(f => f.path === path);
        if (file) file.is_dirty = true;
        const thumbEl = document.querySelector(`.thumbnail-item[data-path="${CSS.escape(path)}"]`);
        if (thumbEl) thumbEl.classList.add('dirty');
    });
    document.getElementById('dirty-indicator').style.display = 'inline';
    if (refresh) loadDirtyCount();
}

function isMetadataWriteJob(job) {
    return ['metadata_generate', 'art_metadata_generate'].includes(job?.type);
}

function isExportRefreshJob(job) {
    return ['export_csv', 'export_upload'].includes(job?.type);
}

function rememberAppliedJob(jobId) {
    if (!jobId) return;
    appliedJobIds.add(jobId);
    localStorage.setItem('totag_applied_job_ids', JSON.stringify([...appliedJobIds].slice(-100)));
}

async function refreshAfterJob(job) {
    if (isExportRefreshJob(job)) {
        if (activeTab === 'export') await loadExportFolders();
        rememberAppliedJob(job.id);
        return;
    }

    const payload = job.payload || {};
    const result = job.result || {};
    const paths = result.generated_paths || (job.status === 'done' ? (payload.paths || []) : []);
    if (paths.length) await refreshMetadataSummaries(paths);
    if (paths.length) markFilesDirty(paths);
    if (selectedFiles.some(p => paths.includes(p))) {
        await loadMetadata(true);
    }
    rememberAppliedJob(job.id);
}

async function refreshLiveDirtyForJob(job) {
    if (!isMetadataWriteJob(job)) return;
    const generatedPaths = job.result?.generated_paths || [];
    if (!generatedPaths.length) return;

    const markedPaths = liveDirtyJobPaths.get(job.id) || new Set();
    const newPaths = generatedPaths.filter(path => !markedPaths.has(path));
    if (!newPaths.length) return;

    newPaths.forEach(path => markedPaths.add(path));
    liveDirtyJobPaths.set(job.id, markedPaths);
    await refreshMetadataSummaries(newPaths);
    markFilesDirty(newPaths, false);
    if (selectedFiles.some(p => newPaths.includes(p))) {
        await loadMetadata(true);
    }
}

function subscribeJob(jobId) {
    if (!jobId || activeJobStreams.has(jobId)) return;
    const es = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/stream`);
    activeJobStreams.set(jobId, es);
    es.onmessage = async (event) => {
        if (event.data === '[DONE]') {
            es.close();
            activeJobStreams.delete(jobId);
            const job = activeJobs.get(jobId);
            if (job) await refreshAfterJob(job);
            renderJobsStatus();
            rerenderExportTableForJobs();
            return;
        }
        try {
            const payload = JSON.parse(event.data);
            if (payload.job) {
                activeJobs.set(payload.job.id, payload.job);
                await refreshLiveDirtyForJob(payload.job);
                renderJobsStatus();
                rerenderExportTableForJobs();
                const log = document.getElementById('export-log');
                const body = document.getElementById('export-log-body');
                if (log?.style.display !== 'none' && body && log.dataset.jobId === payload.job.id) {
                    const ev = payload.event;
                    if (ev?.message) {
                        const prefix = ev.level && ev.level !== 'info' ? `[${ev.level}] ` : '';
                        body.textContent += `${prefix}${ev.message}\n`;
                        body.scrollTop = body.scrollHeight;
                    }
                }
            }
        } catch (e) {
            console.warn('Job stream parse error', e);
        }
    };
    es.onerror = () => {
        es.close();
        activeJobStreams.delete(jobId);
        renderJobsStatus();
        rerenderExportTableForJobs();
    };
}

async function refreshActiveJobs() {
    try {
        const r = await fetch('/api/jobs/active');
        const d = await r.json();
        activeJobs = new Map((d.jobs || []).map(job => [job.id, job]));
        for (const job of activeJobs.values()) {
            if (['queued', 'running'].includes(job.status)) subscribeJob(job.id);
            if (['done', 'failed'].includes(job.status) && (isMetadataWriteJob(job) || isExportRefreshJob(job)) && !appliedJobIds.has(job.id)) {
                await refreshAfterJob(job);
            }
        }
        renderJobsStatus();
        rerenderExportTableForJobs();
    } catch (e) {
        // Keep old job display if a poll fails.
    }
}

async function startServerJob(endpoint, payload) {
    const r = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (!r.ok || data.error) throw new Error(data.error || `Job start failed (${r.status})`);
    if (data.job) activeJobs.set(data.job.id, data.job);
    subscribeJob(data.job_id || data.job?.id);
    renderJobsStatus();
    rerenderExportTableForJobs();
    return data;
}

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    // Auto-collapse sidebar on narrow screens (mobile portrait). Deferred one
    // frame: some embedding containers (a resizable preview pane, a PWA
    // shell) report a narrower width at DOMContentLoaded than their settled
    // size a moment later, which would otherwise wrongly freeze the sidebar
    // collapsed on a normal desktop-sized window.
    requestAnimationFrame(() => {
        if (window.innerWidth < 768) {
            document.getElementById('sidebar')?.classList.add('collapsed');
        }
    });

    loadModels();
    loadLocations();
    loadQuickSearches();
    loadFolders();
    loadDirtyCount();
    setupEventListeners();
    setupInfiniteScroll();
    // Grid-level drag handling for thumbnail→thumbnail metadata copy
    thumbnailGrid.addEventListener('dragover', (e) => {
        if (!draggedFilePaths.length) return;
        e.preventDefault();
        const item = e.target.closest('.thumbnail-item');
        document.querySelectorAll('.thumbnail-item.copy-target').forEach(el => el.classList.remove('copy-target'));
        if (item && item.dataset.path !== draggedFilePaths[0]) item.classList.add('copy-target');
    });
    thumbnailGrid.addEventListener('dragleave', (e) => {
        if (!thumbnailGrid.contains(e.relatedTarget)) {
            document.querySelectorAll('.thumbnail-item.copy-target').forEach(el => el.classList.remove('copy-target'));
        }
    });
    thumbnailGrid.addEventListener('drop', async (e) => {
        e.preventDefault();
        document.querySelectorAll('.thumbnail-item.copy-target').forEach(el => el.classList.remove('copy-target'));
        const item = e.target.closest('.thumbnail-item');
        if (!item || !draggedFilePaths.length) return;
        const targetPath = item.dataset.path;
        const sourcePath = draggedFilePaths[0];
        if (!targetPath || sourcePath === targetPath) return;
        draggedFilePaths = [];
        await copyMetadataFromTo(sourcePath, targetPath);
    });
    // Global drag handler — catches any drag starting within a thumbnail item
    document.addEventListener('dragstart', (e) => {
        const item = e.target.closest('.thumbnail-item');
        if (!item) return;
        const path = item.dataset.path;
        if (!path) return;
        draggedFilePaths = selectedFiles.includes(path) ? [...selectedFiles] : [path];
        e.dataTransfer.setData('text/plain', '1');
        e.dataTransfer.effectAllowed = 'move';
    });
    document.addEventListener('dragend', () => { if (draggedFilePaths.length) { draggedFilePaths = []; } });
    // Start idle timer after initial load to enable background pre-fetch
    setTimeout(() => {
        startIdleTimer();
    }, 1000);
    // Start polling background status every second
    setInterval(pollBackgroundStatus, 1000);
    refreshActiveJobs();
    setInterval(refreshActiveJobs, 3000);
    // Auto-trigger bulk metadata + thumbnail generation for everything missing
    setTimeout(() => {
        triggerBulkMetadataLoad();
        triggerBulkThumbnailGeneration();
    }, 3000);
});

// Event Listeners
function setupEventListeners() {
    document.getElementById('refresh-btn').addEventListener('click', refreshDatabase);
    document.getElementById('sync-btn').addEventListener('click', syncAllChanges);
    document.getElementById('read-meta-btn').addEventListener('click', reReadMetadata);
    document.getElementById('apply-btn').addEventListener('click', applyMetadataChanges);
    document.getElementById('keyword-append-btn').addEventListener('click', appendKeywords);
    document.getElementById('guess-location-btn').addEventListener('click', guessLocation);
    keywordAppend.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ',') {
            e.preventDefault();
            const val = keywordAppend.value.replace(/,/g, '').trim();
            if (val) { addPendingTag(val); keywordAppend.value = ''; }
        } else if (e.key === 'Backspace' && keywordAppend.value === '' && pendingKeywords.length > 0) {
            pendingKeywords.pop();
            renderTagChips();
        }
    });
    keywordAppend.addEventListener('input', () => {
        const val = keywordAppend.value;
        if (val.endsWith(',')) {
            const tag = val.slice(0, -1).trim();
            if (tag) { addPendingTag(tag); }
            keywordAppend.value = '';
        }
    });
    document.getElementById('open-finder-btn').addEventListener('click', () => openInFinder());
    document.getElementById('undo-btn').addEventListener('click', undoChanges);
    document.getElementById('gen-meta-btn').addEventListener('click', generateMetadataWithAI);
    document.querySelector('.status-jobs-section')?.addEventListener('click', () => openJobLog());

    document.getElementById('location-override').addEventListener('change', (e) => {
        addRecentLocation(e.target.value);
    });

    document.getElementById('model-selector').addEventListener('change', (e) => {
        fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model: e.target.value })
        }).catch(() => {});
    });

    document.getElementById('cancel-batch-btn').addEventListener('click', () => {
        if (selectedJobId) {
            fetch(`/api/jobs/${encodeURIComponent(selectedJobId)}/cancel`, { method: 'POST' }).catch(() => {});
        }
        batchCancelRequested = true;
    });

    // Delete key — confirm then delete selected files
    document.addEventListener('keydown', async (e) => {
        if (e.key !== 'Delete' && e.key !== 'Backspace') return;
        const tag = document.activeElement.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
        if (e.key === 'Backspace') return; // reserve Backspace for text fields
        if (selectedFiles.length === 0) return;
        e.preventDefault();

        const count = selectedFiles.length;
        const label = count === 1
            ? `Delete "${selectedFiles[0].split('/').pop()}"?`
            : `Delete ${count} files?`;
        showConfirmDialog(label, 'This will permanently delete the file(s) from disk.', async () => {
            await deleteSelectedFiles();
        });
    });

    // Auto-update metadata preview on input change (marks form as dirty)
    metaTitle.addEventListener('input', debounce(updateMetadataPreview, 500));
    metaDescription.addEventListener('input', debounce(updateMetadataPreview, 500));
    metaKeywords.addEventListener('input', debounce(updateMetadataPreview, 500));

    // Track user activity for idle pre-fetch
    document.addEventListener('click', markUserWorking);
    document.addEventListener('keydown', markUserWorking);
    document.addEventListener('scroll', markUserWorking);

    // Arrow key gallery navigation
    document.addEventListener('keydown', (e) => {
        const tag = document.activeElement.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
        if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key)) return;
        if (allFiles.length === 0) return;
        e.preventDefault();

        const currentIndex = selectedFiles.length === 1
            ? allFiles.findIndex(f => f.path === selectedFiles[0])
            : lastSelectedIndex;
        if (currentIndex === -1 && allFiles.length > 0) {
            handleThumbnailClick({ shiftKey: false, ctrlKey: false, metaKey: false }, allFiles[0].path, 0);
            return;
        }

        // Compute columns from grid layout — read actual CSS grid columns to avoid
        // offsetWidth/scrollbar mismatch in multi-folder mode
        const grid = document.getElementById('thumbnail-grid');
        let cols = 1;
        if (grid) {
            const tplCols = window.getComputedStyle(grid).gridTemplateColumns;
            if (tplCols && tplCols !== 'none') {
                cols = tplCols.trim().split(/\s+/).length;
            }
        }

        let newIndex = currentIndex;
        if (e.key === 'ArrowRight') newIndex = currentIndex + 1;
        else if (e.key === 'ArrowLeft') newIndex = currentIndex - 1;
        else if (e.key === 'ArrowDown') newIndex = currentIndex + cols;
        else if (e.key === 'ArrowUp') newIndex = currentIndex - cols;

        newIndex = Math.max(0, Math.min(allFiles.length - 1, newIndex));
        if (newIndex === currentIndex) return;

        handleThumbnailClick(
            { shiftKey: e.shiftKey, ctrlKey: false, metaKey: false },
            allFiles[newIndex].path,
            newIndex
        );

        // Scroll selected item into view
        const thumbEl = document.querySelector(`.thumbnail-item[data-path="${CSS.escape(allFiles[newIndex].path)}"]`);
        if (thumbEl) thumbEl.scrollIntoView({ block: 'nearest' });
    });

    // Cmd+A / Cmd+D / Cmd+I / Cmd+G shortcuts
    document.addEventListener('keydown', async (e) => {
        if (!e.metaKey && !e.ctrlKey) return;
        const tag = document.activeElement.tagName;
        const inTextField = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';

        if (e.key === 'a' || e.key === 'A') {
            if (inTextField) return;
            e.preventDefault();
            if (allFiles.length === 0) return;
            selectedFiles = allFiles.map(f => f.path);
            lastSelectedIndex = allFiles.length - 1;
            updateThumbnailSelection();
            loadMetadata();

        } else if (e.key === 'd' || e.key === 'D') {
            if (inTextField) return;
            e.preventDefault();
            selectedFiles = [];
            // Return focus to last single selection if available
            if (lastSelectedIndex >= 0 && lastSelectedIndex < allFiles.length) {
                selectedFiles = [allFiles[lastSelectedIndex].path];
            }
            updateThumbnailSelection();
            loadMetadata();
            if (selectedFiles.length === 1) {
                const thumbEl = document.querySelector(`.thumbnail-item[data-path="${CSS.escape(selectedFiles[0])}"]`);
                if (thumbEl) thumbEl.scrollIntoView({ block: 'nearest' });
            }

        } else if (e.key === 'i' || e.key === 'I') {
            if (inTextField) return;
            e.preventDefault();
            if (allFiles.length === 0) return;
            selectedFiles = allFiles.map(f => f.path).filter(p => !selectedFiles.includes(p));
            lastSelectedIndex = selectedFiles.length > 0
                ? allFiles.findIndex(f => f.path === selectedFiles[selectedFiles.length - 1])
                : -1;
            updateThumbnailSelection();
            loadMetadata();

        } else if (e.key === 'g' || e.key === 'G') {
            if (inTextField) return;
            e.preventDefault();
            if (selectedFiles.length === 0) return;
            if (selectedFiles.length === 1) {
                generateMetadataWithAI(); // immediate path for single file
            } else {
                const contextHint = getMetadataContextHint();
                if (contextHint !== null) enqueueForGeneration([...selectedFiles], contextHint);
            }

        } else if (e.key === 'u' || e.key === 'U') {
            if (inTextField) return;
            e.preventDefault();
            if (allFiles.length === 0) return;
            selectedFiles = allFiles.filter(f => !f.is_dirty).map(f => f.path);
            lastSelectedIndex = selectedFiles.length > 0
                ? allFiles.findIndex(f => f.path === selectedFiles[selectedFiles.length - 1])
                : -1;
            updateThumbnailSelection();
            loadMetadata();

        } else if (e.key === 's' || e.key === 'S') {
            e.preventDefault();
            if (selectedFiles.length === 0) return;
            applyMetadataChanges();
        }
    });
}

// Debounce helper
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

// Idle pre-fetch helpers
function markUserWorking() {
    isUserWorking = true;
    if (idleTimer) {
        clearTimeout(idleTimer);
        idleTimer = null;
    }
    // Reset working flag after 3 seconds of no activity
    setTimeout(() => {
        isUserWorking = false;
        startIdleTimer();
    }, 3000);
}

function startIdleTimer() {
    if (idleTimer) clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
        if (!isUserWorking) {
            debugLog('Idle timer fired, starting prefetch...');
            preFetchNextFolder();
        }
    }, IDLE_DELAY);
}

// Track which folders have been checked for missing data
let checkedFolders = new Set();
let prefetchComplete = false; // Flag to track if initial prefetch sweep is done

async function preFetchNextFolder() {
    // Start from first root if no folder selected, otherwise use current folder's root
    let searchRoot = currentFolder;
    if (!searchRoot || searchRoot === "") {
        searchRoot = FOLDER_ROOTS[0];
        debugLog('No folder selected, using first root:', searchRoot);
    }

    debugLog('preFetchNextFolder called, currentFolder:', currentFolder, 'searchRoot:', searchRoot);

    const currentRoot = FOLDER_ROOTS.find(root => searchRoot.startsWith(root));
    debugLog('searchRoot:', searchRoot, 'currentRoot:', currentRoot);

    if (!currentRoot) {
        debugLog('No current root found, exiting');
        return;
    }

    // Find folders under this root
    const allFolderItems = document.querySelectorAll('.folder-item[data-folder]');
    debugLog(`Found ${allFolderItems.length} folder items with data-folder attribute`);

    // Track how many folders we're checking to avoid overwhelming the server
    let foldersToCheck = 0;
    const MAX_CONSECUTIVE_FOLDERS = 5; // Process up to 5 folders, then pause

    for (const item of allFolderItems) {
        const folderPath = item.dataset.folder;
        if (folderPath.startsWith(currentRoot) && !checkedFolders.has(folderPath)) {
            checkedFolders.add(folderPath);
            foldersToCheck++;

            // Pause after processing N folders to avoid blocking UI
            if (foldersToCheck > MAX_CONSECUTIVE_FOLDERS) {
                debugLog(`Pausing after ${MAX_CONSECUTIVE_FOLDERS} folders, will continue...`);
                setTimeout(() => {
                    preFetchNextFolder();
                }, 500);
                return;
            }

            const countEl = item.querySelector('.folder-count');
            const count = parseInt(countEl?.textContent || '0');

            debugLog(`Checking folder: ${folderPath}, count: ${count}`);

            // Skip empty folders
            if (count === 0) {
                debugLog(`Skipping ${folderPath} - empty folder`);
                continue;
            }

            // Check if folder needs metadata/thumbnails by sampling files
            debugLog(`Sampling ${folderPath} for missing data...`);
            setBackgroundStatus('Checking...', true);

            try {
                const response = await fetch(`/api/files?folder=${encodeURIComponent(folderPath)}`);
                if (!response.ok) continue;

                const files = await response.json();
                if (!files || files.length === 0) continue;

                // Sample files to check status (larger sample for better accuracy)
                const sampleSize = Math.min(20, files.length);
                let needsMetadata = 0;
                let needsThumbnails = 0;

                // Sample from different parts of the array for better coverage
                const step = Math.max(1, Math.floor(files.length / sampleSize));
                for (let i = 0; i < files.length && needsMetadata < sampleSize; i += step) {
                    if (!files[i].has_metadata) needsMetadata++;
                    if (!files[i].thumb_url) needsThumbnails++;
                }

                // Estimate for full folder
                const estMetadata = Math.round((needsMetadata / sampleSize) * files.length);
                const estThumbnails = Math.round((needsThumbnails / sampleSize) * files.length);

                debugLog(`${folderPath}: ~${estMetadata}/${files.length} need metadata, ~${estThumbnails} need thumbnails`);

                // If more than 10% need data, trigger background load (lowered from 20%)
                if (estMetadata > files.length * 0.1 || estThumbnails > files.length * 0.1) {
                    debugLog(`Triggering background load for ${folderPath}`);

                    await fetch('/api/load-folder', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ folder: folderPath, refresh: false }),
                    });

                    debugLog(`Started background load for: ${folderPath}`);
                } else {
                    debugLog(`Skipping ${folderPath} - most data already loaded`);
                }
            } catch (error) {
                debugLog(`Error checking ${folderPath}: ${error.message}`);
            }

            setBackgroundStatus('Idle', false);
        }
    }

    // All folders in current root processed, move to next root
    debugLog('No more folders to check in current root');
    const currentRootIndex = FOLDER_ROOTS.indexOf(currentRoot);
    if (currentRootIndex < FOLDER_ROOTS.length - 1) {
        const nextRoot = FOLDER_ROOTS[currentRootIndex + 1];
        debugLog(`Moving to next root: ${nextRoot}`);
        preFetchNextFolderFromRoot(nextRoot);
    } else {
        debugLog('All roots processed - prefetch complete. Resetting in 60s...');
        prefetchComplete = true;
        setTimeout(() => {
            checkedFolders.clear();
            prefetchComplete = false;
            debugLog('Cleared checked folders set');
        }, 60000);
    }
}

async function preFetchNextFolderFromRoot(root) {
    debugLog(`preFetchNextFolderFromRoot called for: ${root}`);
    const allFolderItems = document.querySelectorAll('.folder-item[data-folder]');

    // Track how many folders we're checking to avoid overwhelming the server
    let foldersToCheck = 0;
    const MAX_CONSECUTIVE_FOLDERS = 5; // Process up to 5 folders, then pause

    for (const item of allFolderItems) {
        const folderPath = item.dataset.folder;
        if (folderPath.startsWith(root) && !checkedFolders.has(folderPath)) {
            checkedFolders.add(folderPath);
            foldersToCheck++;

            // Pause after processing N folders to avoid blocking UI
            if (foldersToCheck > MAX_CONSECUTIVE_FOLDERS) {
                debugLog(`Pausing after ${MAX_CONSECUTIVE_FOLDERS} folders, will continue...`);
                setTimeout(() => {
                    preFetchNextFolderFromRoot(root);
                }, 500);
                return;
            }

            const countEl = item.querySelector('.folder-count');
            const count = parseInt(countEl?.textContent || '0');

            debugLog(`Checking folder: ${folderPath}, count: ${count}`);

            // Skip empty folders
            if (count === 0) {
                debugLog(`Skipping ${folderPath} - empty folder`);
                continue;
            }

            // Check if folder needs metadata/thumbnails by sampling files
            debugLog(`Sampling ${folderPath} for missing data...`);
            setBackgroundStatus('Checking...', true);

            try {
                const response = await fetch(`/api/files?folder=${encodeURIComponent(folderPath)}`);
                if (!response.ok) continue;

                const files = await response.json();
                if (!files || files.length === 0) continue;

                // Sample files to check status (larger sample for better accuracy)
                const sampleSize = Math.min(20, files.length);
                let needsMetadata = 0;
                let needsThumbnails = 0;

                // Sample from different parts of the array for better coverage
                const step = Math.max(1, Math.floor(files.length / sampleSize));
                for (let i = 0; i < files.length && needsMetadata < sampleSize; i += step) {
                    if (!files[i].has_metadata) needsMetadata++;
                    if (!files[i].thumb_url) needsThumbnails++;
                }

                // Estimate for full folder
                const estMetadata = Math.round((needsMetadata / sampleSize) * files.length);
                const estThumbnails = Math.round((needsThumbnails / sampleSize) * files.length);

                debugLog(`${folderPath}: ~${estMetadata}/${files.length} need metadata, ~${estThumbnails} need thumbnails`);

                // If more than 10% need data, trigger background load (lowered from 20%)
                if (estMetadata > files.length * 0.1 || estThumbnails > files.length * 0.1) {
                    debugLog(`Triggering background load for ${folderPath}`);

                    await fetch('/api/load-folder', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ folder: folderPath, refresh: false }),
                    });

                    debugLog(`Started background load for: ${folderPath}`);
                } else {
                    debugLog(`Skipping ${folderPath} - most data already loaded`);
                }
            } catch (error) {
                debugLog(`Error checking ${folderPath}: ${error.message}`);
            }

            setBackgroundStatus('Idle', false);
        }
    }

    // All folders in root processed, move to next root
    debugLog(`No more folders to check in root: ${root}`);
    const currentRootIndex = FOLDER_ROOTS.indexOf(root);
    if (currentRootIndex < FOLDER_ROOTS.length - 1) {
        const nextRoot = FOLDER_ROOTS[currentRootIndex + 1];
        debugLog(`Moving to next root: ${nextRoot}`);
        setTimeout(() => {
            if (!isUserWorking) {
                preFetchNextFolderFromRoot(nextRoot);
            }
        }, 500);
    } else {
        debugLog('All roots processed - prefetch complete');
    }
}

// The actual configured folder roots, kept in sync with the backend —
// refreshed every time renderFolderTree() runs (i.e. every /api/folders
// load), never hardcoded, since Watched Folders are per-install.
let FOLDER_ROOTS = [];

// Load folder tree
function renderFolderTree(roots) {
    FOLDER_ROOTS = roots.map(r => r.root);
    folderList.innerHTML = '';
    if (!roots.length) {
        const empty = document.createElement('div');
        empty.style.cssText = 'padding:1.5rem 1rem;text-align:center;';
        empty.innerHTML = `
            <p style="font-size:0.82rem;color:var(--text-secondary);line-height:1.5;margin:0 0 0.75rem;">No folders yet. Add a folder on this machine to start browsing and tagging your photos/videos.</p>
            <button class="btn-secondary" onclick="openFolderRootsModal()">+ Add Folder</button>
        `;
        folderList.appendChild(empty);
        return;
    }
    roots.forEach(root => {
            // Add root header (clickable to toggle)
            const rootEl = document.createElement('div');
            rootEl.className = 'folder-item folder-root';
            rootEl.innerHTML = `
                <span class="folder-toggle">▶</span>
                <span class="folder-name">${root.root}</span>
                <span class="folder-count">${root.total}</span>
            `;
            folderList.appendChild(rootEl);

            // Add scan button for each root
            const scanBtn = document.createElement('button');
            scanBtn.className = 'btn-icon root-scan-btn';
            scanBtn.title = 'Scan this folder';
            scanBtn.innerHTML = `
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/>
                    <circle cx="10" cy="14" r="3"/>
                    <line x1="12.12" y1="16.12" x2="15" y2="19"/>
                </svg>
            `;
            scanBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                scanRootFolder(root.root);
            });
            rootEl.appendChild(scanBtn);

            // Create container for subfolders (hidden by default)
            const subfolderContainer = document.createElement('div');
            subfolderContainer.className = 'subfolder-list';
            subfolderContainer.style.display = 'none';

            // Add subfolders
            root.folders.forEach(folder => {
                const folderEl = document.createElement('div');
                folderEl.className = 'folder-item';
                folderEl.dataset.folder = folder.folder;
                // Display only the subfolder name (not full path) for cleaner look
                const subfolderName = folder.folder.split('/').pop();

                // Check if this is an editorial folder (_ED anywhere in name)
                const isEditorial = subfolderName.includes('_ED');
                if (isEditorial) {
                    folderEl.classList.add('editorial');
                }

                folderEl.innerHTML = `
                    <span class="dirty-dot${folder.dirty_count > 0 ? ' visible' : ''}"></span>
                    <span class="folder-name">${subfolderName}</span>
                    <span class="folder-count">${folder.count}</span>
                `;
                folderEl.addEventListener('click', (e) => {
                    markUserWorking();
                    handleFolderClick(e, folder.folder);
                });
                folderEl.addEventListener('dragover', (e) => {
                    e.preventDefault();
                    e.dataTransfer.dropEffect = 'move';
                    folderEl.classList.add('drop-target');
                });
                folderEl.addEventListener('dragleave', () => folderEl.classList.remove('drop-target'));
                folderEl.addEventListener('drop', async (e) => {
                    e.preventDefault();
                    folderEl.classList.remove('drop-target');
                    if (!draggedFilePaths.length) return;
                    const paths = [...draggedFilePaths];
                    draggedFilePaths = [];
                    await confirmAndMoveFiles(paths, folder.folder);
                });
                subfolderContainer.appendChild(folderEl);

                // Mark folders with data as already prefetched
                if (folder.count > 0) {
                    prefetchedFolders.add(folder.folder);
                }
            });

            folderList.appendChild(subfolderContainer);

            // Toggle on root click
            rootEl.addEventListener('click', (e) => {
                e.stopPropagation();
                const toggle = rootEl.querySelector('.folder-toggle');
                const isExpanded = subfolderContainer.style.display !== 'none';
                subfolderContainer.style.display = isExpanded ? 'none' : 'block';
                toggle.textContent = '▶';
                rootEl.classList.toggle('expanded', !isExpanded);
            });
        });
}

async function loadFolders() {
    try {
        const response = await fetch('/api/folders');
        const roots = await response.json();
        renderFolderTree(roots);
    } catch (error) {
        console.error('Error loading folders:', error);
    }
}

// ─── Watched Folders (folder roots) settings ─────────────────────────────────

let folderRootsEditData = null; // [{path, exists}] — working copy while modal is open

async function openFolderRootsModal() {
    try {
        const res = await fetch('/api/folder-roots');
        const data = await res.json();
        folderRootsEditData = (data.folders || []).map(f => ({ ...f }));
    } catch (e) {
        console.error('Failed to load folder roots', e);
        folderRootsEditData = [];
    }
    renderFolderRootsModalBody();
    document.getElementById('folder-roots-modal-overlay').style.display = 'flex';
    document.getElementById('folder-roots-new-path').value = '';
}

function closeFolderRootsModal() {
    document.getElementById('folder-roots-modal-overlay').style.display = 'none';
    folderRootsEditData = null;
}

function renderFolderRootsModalBody() {
    const body = document.getElementById('folder-roots-modal-body');
    body.innerHTML = '';
    if (!folderRootsEditData.length) {
        body.innerHTML = '<div style="padding:0.75rem;font-size:0.8rem;color:var(--text-secondary);">No folders yet — add one below.</div>';
        return;
    }
    folderRootsEditData.forEach((f, i) => {
        const row = document.createElement('div');
        row.className = 'loc-entry-row';

        const statusDot = document.createElement('span');
        statusDot.textContent = f.exists ? '●' : '○';
        statusDot.title = f.exists ? 'Reachable' : 'Not found right now (e.g. an unmounted external drive)';
        statusDot.style.cssText = `flex-shrink:0;color:${f.exists ? '#4ade80' : '#f59e0b'};`;

        const pathLabel = document.createElement('span');
        pathLabel.textContent = f.path;
        pathLabel.style.cssText = 'flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:0.8rem;';
        pathLabel.title = f.path;

        const delBtn = document.createElement('button');
        delBtn.className = 'loc-del-btn';
        delBtn.textContent = '✕';
        delBtn.title = 'Remove';
        delBtn.onclick = () => { folderRootsEditData.splice(i, 1); renderFolderRootsModalBody(); };

        row.append(statusDot, pathLabel, delBtn);
        body.appendChild(row);
    });
}

function _addFolderRootPath(path) {
    path = (path || '').trim().replace(/\/+$/, '');
    if (!path) return;
    if (folderRootsEditData.some(f => f.path === path)) {
        setStatus('That folder is already in the list', false);
        return;
    }
    folderRootsEditData.push({ path, exists: null });
    renderFolderRootsModalBody();
}

function addFolderRootEntry() {
    const input = document.getElementById('folder-roots-new-path');
    _addFolderRootPath(input.value);
    input.value = '';
}

async function browseForFolder() {
    const btn = document.getElementById('folder-roots-browse-btn');
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = 'Waiting for Finder…';
    try {
        const res = await fetch('/api/pick-folder', { method: 'POST' });
        const data = await res.json();
        if (data.cancelled) {
            // User closed the dialog without choosing — not an error.
        } else if (data.error) {
            setStatus(`Folder picker error: ${data.error}`, false);
        } else if (data.path) {
            _addFolderRootPath(data.path);
        }
    } catch (e) {
        setStatus(`Folder picker error: ${e.message}`, false);
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

async function saveFolderRoots() {
    const saveBtn = document.getElementById('folder-roots-save-btn');
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = 'Saving…'; }
    try {
        const res = await fetch('/api/folder-roots', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ folders: folderRootsEditData.map(f => f.path) }),
        });
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || `Save failed (${res.status})`);
        closeFolderRootsModal();
        const scannedCount = Object.keys(data.scanned || {}).length;
        setStatus(scannedCount ? `Saved — scanned ${scannedCount} new folder(s)` : 'Folder list saved', false);
        setTimeout(() => clearStatus(), 3000);
        loadFolders();
    } catch (e) {
        setStatus(`Error saving folders: ${e.message}`, false);
    } finally {
        if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = 'Save'; }
    }
}

// ─── Stock Site Credentials ───────────────────────────────────────────────────

let credentialsEditData = null; // {SiteName: {host, user, pass, has_pass, sftp, tls, remote_dir}}

async function openCredentialsModal() {
    try {
        const res = await fetch('/api/microstock-credentials');
        const data = await res.json();
        credentialsEditData = {};
        for (const [site, cfg] of Object.entries(data.sites || {})) {
            credentialsEditData[site] = { ...cfg, pass: '' }; // pass always starts blank
        }
    } catch (e) {
        console.error('Failed to load credentials', e);
        credentialsEditData = {};
    }
    renderCredentialsModalBody();
    document.getElementById('credentials-modal-overlay').style.display = 'flex';
}

function closeCredentialsModal() {
    document.getElementById('credentials-modal-overlay').style.display = 'none';
    credentialsEditData = null;
}

function renderCredentialsModalBody() {
    const body = document.getElementById('credentials-modal-body');
    body.innerHTML = '';
    Object.keys(credentialsEditData).sort().forEach(site => {
        const cfg = credentialsEditData[site];
        const isConfigured = !!(cfg.host || cfg.user || cfg.has_pass);

        const card = document.createElement('div');
        card.className = 'loc-group';

        const header = document.createElement('div');
        header.className = 'loc-group-header';
        header.innerHTML = `<span class="loc-group-name" style="border:0;">${escapeHtml(site)}</span>`;
        const statusDot = document.createElement('span');
        statusDot.textContent = isConfigured ? '● configured' : '○ not set';
        statusDot.style.cssText = `font-size:0.68rem;color:${isConfigured ? '#4ade80' : '#6b7280'};white-space:nowrap;`;
        header.appendChild(statusDot);
        const clearBtn = document.createElement('button');
        clearBtn.className = 'loc-del-group-btn';
        clearBtn.textContent = 'Clear';
        clearBtn.title = `Remove saved credentials for ${site}`;
        clearBtn.onclick = () => {
            credentialsEditData[site] = { host: '', user: '', pass: '', has_pass: false, sftp: false, tls: null, remote_dir: '' };
            renderCredentialsModalBody();
        };
        header.appendChild(clearBtn);
        card.appendChild(header);

        const row1 = document.createElement('div');
        row1.className = 'loc-entry-row';
        const hostInput = document.createElement('input');
        hostInput.className = 'loc-label';
        hostInput.placeholder = 'Host (e.g. ftp.example.com)';
        hostInput.value = cfg.host || '';
        hostInput.oninput = e => { cfg.host = e.target.value; };
        const userInput = document.createElement('input');
        userInput.className = 'loc-value';
        userInput.placeholder = 'Username';
        userInput.value = cfg.user || '';
        userInput.oninput = e => { cfg.user = e.target.value; };
        row1.append(hostInput, userInput);
        card.appendChild(row1);

        const row2 = document.createElement('div');
        row2.className = 'loc-entry-row';
        const passInput = document.createElement('input');
        passInput.className = 'loc-value';
        passInput.type = 'password';
        passInput.placeholder = cfg.has_pass ? 'Leave blank to keep existing password' : 'Password';
        passInput.value = cfg.pass || '';
        passInput.oninput = e => { cfg.pass = e.target.value; };
        const remoteDirInput = document.createElement('input');
        remoteDirInput.className = 'loc-value';
        remoteDirInput.placeholder = 'Remote subfolder (optional)';
        remoteDirInput.value = cfg.remote_dir || '';
        remoteDirInput.oninput = e => { cfg.remote_dir = e.target.value; };
        row2.append(passInput, remoteDirInput);
        card.appendChild(row2);

        const row3 = document.createElement('div');
        row3.style.cssText = 'display:flex;gap:1rem;padding:4px 2px 10px;font-size:0.74rem;color:var(--text-secondary);';
        const sftpLabel = document.createElement('label');
        sftpLabel.style.cssText = 'display:flex;align-items:center;gap:0.3rem;cursor:pointer;';
        sftpLabel.innerHTML = `<input type="checkbox" style="width:auto;margin:0;" ${cfg.sftp ? 'checked' : ''}> Force SFTP`;
        sftpLabel.querySelector('input').onchange = e => { cfg.sftp = e.target.checked; };
        const tlsLabel = document.createElement('label');
        tlsLabel.style.cssText = 'display:flex;align-items:center;gap:0.3rem;cursor:pointer;';
        tlsLabel.innerHTML = `<input type="checkbox" style="width:auto;margin:0;" ${cfg.tls ? 'checked' : ''}> Force FTPS/TLS`;
        tlsLabel.querySelector('input').onchange = e => { cfg.tls = e.target.checked; };
        row3.append(sftpLabel, tlsLabel);
        card.appendChild(row3);

        body.appendChild(card);
    });
}

async function saveCredentials() {
    const saveBtn = document.getElementById('credentials-save-btn');
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = 'Saving…'; }
    try {
        const res = await fetch('/api/microstock-credentials', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sites: credentialsEditData }),
        });
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || `Save failed (${res.status})`);
        closeCredentialsModal();
        setStatus(`Saved — configured sites: ${(data.configured_sites || []).join(', ') || 'none'}`, false);
        setTimeout(() => clearStatus(), 4000);
    } catch (e) {
        setStatus(`Error saving credentials: ${e.message}`, false);
    } finally {
        if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = 'Save'; }
    }
}

// Load the Pending Sync virtual folder
async function selectPendingSync() {
    collapseOnMobile();
    if (backgroundPollInterval) {
        clearInterval(backgroundPollInterval);
        backgroundPollInterval = null;
    }

    isPendingSyncView = true;
    isSearchMode = false;
    filterText = '';
    clearTimeout(searchDebounceTimer);
    const _psFi = document.getElementById('filter-input');
    if (_psFi) _psFi.value = '';
    const _psCb = document.getElementById('filter-clear-btn');
    if (_psCb) _psCb.style.display = 'none';
    selectedFolders = [];
    currentFolder = null;
    selectedFiles = [];
    lastSelectedIndex = -1;
    lastSelectedFolderIndex = -1;
    visibleFiles = [];
    currentPage = 0;
    isLoadingMore = false;
    isRendering = false;
    renderQueue = [];

    document.querySelectorAll('.folder-item[data-folder]').forEach(el => el.classList.remove('active'));
    document.getElementById('pending-sync-tab').classList.add('active');

    currentFolderEl.textContent = 'Pending Sync';
    thumbnailGrid.innerHTML = '<div class="loading">Loading...</div>';
    fileCountEl.textContent = '';
    allFiles = [];
    metadataForm.style.display = 'none';
    metadataContent.style.display = 'flex';
    metadataContent.innerHTML = '<div class="no-selection"><p>Select files to view and edit metadata</p></div>';

    setStatus('Loading pending files...', true);

    try {
        const response = await fetch('/api/dirty');
        const dirty = await response.json();
        setRawFiles(dirty);
        applyFilter();
        fileCountEl.textContent = `${allFiles.length} files`;
        updateActionButtonStates();
        if (allFiles.length === 0) {
            thumbnailGrid.innerHTML = '';
            setStatus('No pending files', false);
        } else {
            hideSortBar();
            renderThumbnailsProgressive();
            setStatus(`${allFiles.length} files pending sync`, false);
        }
    } catch (err) {
        setStatus(`Error loading pending files: ${err.message}`, false);
    }
}

// Remove files from the pending sync grid after sync or undo
function removeFilesFromPendingView(paths) {
    if (!isPendingSyncView) return;
    const pathSet = new Set(paths);

    rawFiles = rawFiles.filter(f => !pathSet.has(f.path));
    allFiles = allFiles.filter(f => !pathSet.has(f.path));
    visibleFiles = visibleFiles.filter(f => !pathSet.has(f.path));

    paths.forEach(path => {
        const thumbEl = document.querySelector(`.thumbnail-item[data-path="${CSS.escape(path)}"]`);
        if (thumbEl) thumbEl.remove();
    });

    selectedFiles = selectedFiles.filter(p => !pathSet.has(p));
    fileCountEl.textContent = `${allFiles.length} files`;

    if (allFiles.length === 0) {
        thumbnailGrid.innerHTML = '';
        metadataForm.style.display = 'none';
        metadataContent.style.display = 'flex';
        metadataContent.innerHTML = '<div class="no-selection"><p>All files synced</p></div>';
    }

    updateActionButtonStates();
}

// Select folder and load thumbnails
function handleFolderClick(e, folderPath) {
    // Leave pending sync view when a real folder is selected
    isPendingSyncView = false;
    document.getElementById('pending-sync-tab').classList.remove('active');

    const allFolderEls = [...document.querySelectorAll('.folder-item[data-folder]')];
    const clickedIndex = allFolderEls.findIndex(el => el.dataset.folder === folderPath);

    if (e.metaKey || e.ctrlKey) {
        // Cmd/Ctrl: toggle
        const idx = selectedFolders.indexOf(folderPath);
        if (idx > -1) {
            selectedFolders.splice(idx, 1);
        } else {
            selectedFolders.push(folderPath);
        }
        lastSelectedFolderIndex = clickedIndex;
    } else if (e.shiftKey && lastSelectedFolderIndex !== -1) {
        // Shift: range select
        const start = Math.min(lastSelectedFolderIndex, clickedIndex);
        const end = Math.max(lastSelectedFolderIndex, clickedIndex);
        selectedFolders = allFolderEls.slice(start, end + 1).map(el => el.dataset.folder);
    } else {
        // Normal click: single select
        selectedFolders = [folderPath];
        lastSelectedFolderIndex = clickedIndex;
    }

    updateFolderSelection();

    if (selectedFolders.length === 1) {
        selectFolder(selectedFolders[0]);
    } else if (selectedFolders.length > 1) {
        selectMultipleFolders(selectedFolders);
    }
}

function updateFolderSelection() {
    document.querySelectorAll('.folder-item[data-folder]').forEach(el => {
        el.classList.toggle('active', selectedFolders.includes(el.dataset.folder));
    });
}

async function selectMultipleFolders(folders) {
    collapseOnMobile();
    if (backgroundPollInterval) {
        clearInterval(backgroundPollInterval);
        backgroundPollInterval = null;
    }

    currentFolder = null;
    selectedFiles = [];
    lastSelectedIndex = -1;
    visibleFiles = [];
    currentPage = 0;
    isLoadingMore = false;
    isRendering = false;
    renderQueue = [];

    currentFolderEl.textContent = `${folders.length} folders selected`;
    thumbnailGrid.innerHTML = '<div class="loading">Loading...</div>';
    allFiles = [];
    metadataForm.style.display = 'none';
    metadataContent.style.display = 'flex';
    metadataContent.innerHTML = '<div class="no-selection"><p>Select files to view and edit metadata</p></div>';

    setStatus(`Loading ${folders.length} folders...`, true);

    try {
        const response = await fetch('/api/load-folders', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ folders }),
        });
        const data = await response.json();
        setRawFiles(data.files || []);
        applyFilter();
        fileCountEl.textContent = `${allFiles.length} files`;
        updateActionButtonStates();
        showSortBar();
        applySort();
        selectFirstFile();
        loadDirtyCount();
        setStatus(`Loaded ${allFiles.length} files from ${folders.length} folders`, false);
    } catch (err) {
        setStatus(`Error loading folders: ${err.message}`, false);
    }
}

async function selectFolder(folder) {
    collapseOnMobile();
    // Clear any existing polling interval from previous folder
    if (backgroundPollInterval) {
        clearInterval(backgroundPollInterval);
        backgroundPollInterval = null;
    }

    currentFolder = folder;
    selectedFolders = [folder];
    selectedFiles = [];
    lastSelectedIndex = -1;
    visibleFiles = [];
    currentPage = 0;
    isLoadingMore = false;
    isRendering = false;
    renderQueue = [];

    // Update UI
    document.querySelectorAll('.folder-item').forEach(el => el.classList.remove('active'));
    const activeEl = document.querySelector(`[data-folder="${folder}"]`);
    if (activeEl) activeEl.classList.add('active');

    currentFolderEl.textContent = folder.split('/').pop() || folder;
    currentFolderEl.title = folder;

    // Clear search/filter state and previous thumbnails AND data immediately to avoid confusion
    isSearchMode = false;
    filterText = '';
    clearTimeout(searchDebounceTimer);
    const filterInputEl = document.getElementById('filter-input');
    if (filterInputEl) filterInputEl.value = '';
    const filterClearBtn = document.getElementById('filter-clear-btn');
    if (filterClearBtn) filterClearBtn.style.display = 'none';
    thumbnailGrid.innerHTML = '<div class="loading">Loading...</div>';
    fileCountEl.textContent = '';
    rawFiles = [];
    allFiles = [];
    updateSelectAllBtn();
    visibleFiles = [];
    metadataForm.style.display = 'none';
    metadataContent.style.display = 'flex';
    metadataContent.innerHTML = '<div class="no-selection"><p>Select files to view and edit metadata</p></div>';

    setStatus(`Loading ${folder}...`, true);

    try {
        // Load folder with metadata and thumbnails on-demand
        const response = await fetch(`/api/load-folder`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ folder: folder }),
        });
        const data = await response.json();
        setRawFiles(data.files || data);  // Handle both new and old response format
        applyFilter();
        const metadataLoading = data.metadata_loading || false;
        const thumbsLoading = data.thumbs_loading || false;

        fileCountEl.textContent = `${allFiles.length} files`;

        // Update undo button state now that allFiles is populated
        updateActionButtonStates();

        // Start idle pre-fetch timer after folder loads
        startIdleTimer();

        // Show background status based on what's happening
        if (metadataLoading && thumbsLoading) {
            setBackgroundStatus('Loading metadata & generating thumbnails...', true);
        } else if (metadataLoading) {
            setBackgroundStatus('Loading metadata...', true);
        } else if (thumbsLoading) {
            setBackgroundStatus('Generating thumbnails...', true);
        }

        // Render first batch of thumbnails
        showSortBar();
        applySort();
        selectFirstFile();
        loadDirtyCount();
        setStatus(`Loaded ${allFiles.length} files`, false);
        setTimeout(() => clearStatus(), 2000);

        // Poll for background task completion
        if (metadataLoading || thumbsLoading) {
            // Check every 2 seconds for up to 120 seconds
            let checkCount = 0;
            const pollingFolder = folder;  // Capture the folder we're polling for

            backgroundPollInterval = setInterval(() => {
                checkCount++;
                fetch(`/api/load-folder`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ folder: pollingFolder }),
                }).then(r => r.json()).then(d => {
                    // Always update status if we're still on the same folder
                    if (currentFolder !== pollingFolder) {
                        // User switched folders - stop polling for old folder
                        clearInterval(backgroundPollInterval);
                        backgroundPollInterval = null;
                        return;
                    }

                    const stillLoadingMeta = d.metadata_loading || false;
                    const stillLoadingThumbs = d.thumbs_loading || false;

                    // Count how many thumbnails are now available
                    const thumbsLoaded = d.files.filter(f => f.thumb_url).length;
                    const totalFiles = d.files.length;

                    if ((!stillLoadingMeta && !stillLoadingThumbs) || checkCount > 60) {
                        clearInterval(backgroundPollInterval);
                        backgroundPollInterval = null;
                        setBackgroundStatus('Idle', false);
                        // Final update to ensure all thumbnails are shown
                        if (d.files) {
                            mergeFileUpdates(d.files);
                            syncDirtyFlagsForFiles(rawFiles);
                            syncDirtyFlagsForFiles(allFiles);
                            updateThumbnails();
                            applyDirtyClassesToVisibleThumbnails();
                            updateActionButtonStates();
                        }
                    } else {
                        // Update status with progress
                        if (stillLoadingThumbs) {
                            setBackgroundStatus(`Generating thumbnails... (${thumbsLoaded}/${totalFiles})`, true);
                        } else if (stillLoadingMeta) {
                            setBackgroundStatus('Loading metadata...', true);
                        }
                    }

                    // Update thumbnails in place as they're generated
                    if (d.files) {
                        mergeFileUpdates(d.files);
                        syncDirtyFlagsForFiles(rawFiles);
                        syncDirtyFlagsForFiles(allFiles);
                        updateThumbnails();
                        applyDirtyClassesToVisibleThumbnails();
                        updateActionButtonStates();
                    }
                }).catch(() => {
                    // On error, check if we should still be polling
                    if (currentFolder === pollingFolder) {
                        // Still on same folder, keep polling
                    } else {
                        // Switched folders, stop polling
                        clearInterval(backgroundPollInterval);
                        backgroundPollInterval = null;
                    }
                });
            }, 2000);
        } else {
            // No background tasks, set status to idle
            setBackgroundStatus('Idle', false);
        }
    } catch (error) {
        console.error('Error loading files:', error);
        thumbnailGrid.innerHTML = '<div class="loading">Error loading files</div>';
        setStatus('Error loading files', false);
    }
}

// Render thumbnails one by one for better perceived performance
function renderThumbnailsOneByOne() {
    console.log(`renderThumbnailsOneByOne: ${allFiles.length} files to render`);

    // Clear the loading indicator
    thumbnailGrid.innerHTML = '';
    visibleFiles = [];
    fileCountEl.textContent = `0/${allFiles.length} files`;

    // Start rendering first thumbnail
    renderNextThumbnail();
}

function renderNextThumbnail() {
    if (visibleFiles.length >= FILES_PER_BATCH || visibleFiles.length >= allFiles.length) {
        // First batch complete, update count and let infinite scroll handle the rest
        console.log(`First batch complete: ${visibleFiles.length} files rendered`);
        return;
    }

    const i = visibleFiles.length;
    const file = allFiles[i];

    const itemEl = document.createElement('div');
    itemEl.className = 'thumbnail-item';
    itemEl.dataset.path = file.path;
    itemEl.dataset.index = i;

    if (fileIsDirty(file)) {
        itemEl.classList.add('dirty');
    }

    if (selectedFiles.includes(file.path)) {
        itemEl.classList.add('selected');
    }

    const imgSrc = file.thumb_url ? `/thumbs/${file.thumb_url.split('/').pop()}` : '';
    const imgEl = document.createElement('img');

    // Mark thumbnails without source as pending
    if (!imgSrc) {
        imgEl.dataset.pending = 'true';
        imgEl.style.background = '#0f3460';
        // Use loading placeholder
        imgEl.src = '/static/images/loading.png';
    } else {
        imgEl.src = imgSrc;
    }

    imgEl.alt = file.filename;
    imgEl.loading = 'lazy';

    imgEl.onerror = function() {
        // Thumbnail not ready yet - hide and mark as pending
        this.style.display = 'none';
        this.dataset.pending = 'true';
    };

    const filenameEl = document.createElement('div');
    filenameEl.className = 'filename';
    filenameEl.textContent = file.filename;

    itemEl.appendChild(imgEl);
    itemEl.appendChild(filenameEl);

    itemEl.draggable = true;
    itemEl.addEventListener('click', (e) => {
        markUserWorking();
        handleThumbnailClick(e, file.path, i);
    });
    itemEl.addEventListener('dblclick', () => openInFinder(file.path));

    thumbnailGrid.appendChild(itemEl);
    visibleFiles.push(file);

    // Update file count progressively
    fileCountEl.textContent = `${visibleFiles.length}/${allFiles.length} files`;

    // Schedule next thumbnail with small delay for visual effect
    setTimeout(() => {
        renderNextThumbnail();
    }, 10);
}

// Update thumbnails in place when new thumbnails are generated
function updateThumbnails() {
    const items = thumbnailGrid.querySelectorAll('.thumbnail-item');
    let updated = 0;

    items.forEach(itemEl => {
        const path = itemEl.dataset.path;
        const file = allFiles.find(f => f.path === path);
        if (!file) return;

        const imgEl = itemEl.querySelector('img');
        if (!imgEl) return;

        // If file now has a thumbnail and this image is marked as pending, update it
        if (file.thumb_url && imgEl.dataset.pending === 'true') {
            imgEl.src = `/thumbs/${file.thumb_url.split('/').pop()}`;
            imgEl.style.display = 'block';
            imgEl.style.background = 'transparent';
            delete imgEl.dataset.pending;
            updated++;
        }
    });

    if (updated > 0) {
        console.log(`Updated ${updated} thumbnails`);
    }
}

// Render thumbnails progressively with infinite scroll
function renderThumbnailsProgressive(append = false) {
    if (isSearchMode) { renderSearchResults(); return; }
    console.log(`renderThumbnailsProgressive: append=${append}, visibleFiles=${visibleFiles.length}, allFiles=${allFiles.length}`);

    if (!append) {
        thumbnailGrid.innerHTML = '';
        visibleFiles = [];
        currentPage = 0;
    }

    updateSelectAllBtn();

    // Calculate how many files to render
    const startIdx = visibleFiles.length;
    const endIdx = Math.min(startIdx + FILES_PER_BATCH, allFiles.length);

    console.log(`Rendering files ${startIdx} to ${endIdx} (total: ${allFiles.length})`);

    if (startIdx >= allFiles.length) {
        // No more files to render
        console.log('No more files to render');
        return;
    }

    // Render next batch
    const fragment = document.createDocumentFragment();
    for (let i = startIdx; i < endIdx; i++) {
        const file = allFiles[i];
        const itemEl = document.createElement('div');
        itemEl.className = 'thumbnail-item';
        itemEl.dataset.path = file.path;
        itemEl.dataset.index = i;

        if (fileIsDirty(file)) {
            itemEl.classList.add('dirty');
        }
        if (selectedFiles.includes(file.path)) {
            itemEl.classList.add('selected');
        }

        const imgSrc = file.thumb_url ? `/thumbs/${file.thumb_url.split('/').pop()}` : '';
        itemEl.innerHTML = `
            <img src="${imgSrc || ''}" alt="${file.filename}" loading="lazy" draggable="false"${!imgSrc ? ' data-pending="true"' : ''}>
            <div class="filename">${file.filename}</div>
        `;

        itemEl.draggable = true;
        itemEl.addEventListener('click', (e) => {
            markUserWorking();
            handleThumbnailClick(e, file.path, i);
        });
        itemEl.addEventListener('dblclick', () => openInFinder(file.path));

        fragment.appendChild(itemEl);
        visibleFiles.push(file);
    }

    thumbnailGrid.appendChild(fragment);
    applyDirtyClassesToVisibleThumbnails();
    currentPage++;

    // Update file count display
    fileCountEl.textContent = `${visibleFiles.length}/${allFiles.length} files`;
    console.log(`Updated file count: ${visibleFiles.length}/${allFiles.length}`);
}

// Setup infinite scroll
function setupInfiniteScroll() {
    thumbnailGrid.addEventListener('scroll', () => {
        const { scrollTop, scrollHeight, clientHeight } = thumbnailGrid;
        const scrollBottom = scrollHeight - scrollTop - clientHeight;

        // Load more when within 200px of bottom
        if (scrollBottom < 200 && !isLoadingMore) {
            if (visibleFiles.length < allFiles.length) {
                console.log(`Scroll triggered load more: scrollBottom=${Math.round(scrollBottom)}, visible=${visibleFiles.length}, total=${allFiles.length}`);
                isLoadingMore = true;
                // Render next batch of 40 files
                renderNextBatch();
                isLoadingMore = false;
            }
        }
    });
    console.log('Infinite scroll setup complete');
}

// Render next batch of 40 files (for infinite scroll)
function renderNextBatch() {
    const startIdx = visibleFiles.length;
    const endIdx = Math.min(startIdx + FILES_PER_BATCH, allFiles.length);

    console.log(`Rendering batch: ${startIdx} to ${endIdx}`);

    for (let i = startIdx; i < endIdx; i++) {
        const file = allFiles[i];

        const itemEl = document.createElement('div');
        itemEl.className = 'thumbnail-item';
        itemEl.dataset.path = file.path;
        itemEl.dataset.index = i;

        if (fileIsDirty(file)) {
            itemEl.classList.add('dirty');
        }

        if (selectedFiles.includes(file.path)) {
            itemEl.classList.add('selected');
        }

        const imgSrc = file.thumb_url ? `/thumbs/${file.thumb_url.split('/').pop()}` : '';
        const imgEl = document.createElement('img');

        if (!imgSrc) {
            imgEl.dataset.pending = 'true';
            imgEl.style.background = '#0f3460';
            // Use loading placeholder
            imgEl.src = '/static/images/loading.png';
        } else {
            imgEl.src = imgSrc;
        }

        imgEl.alt = file.filename;
        imgEl.loading = 'lazy';

        imgEl.onerror = function() {
            this.style.display = 'none';
            this.dataset.pending = 'true';
        };

        const filenameEl = document.createElement('div');
        filenameEl.className = 'filename';
        filenameEl.textContent = file.filename;

        itemEl.appendChild(imgEl);
        itemEl.appendChild(filenameEl);

        itemEl.addEventListener('click', (e) => {
            markUserWorking();
            handleThumbnailClick(e, file.path, i);
        });
        itemEl.addEventListener('dblclick', () => openInFinder(file.path));

        thumbnailGrid.appendChild(itemEl);
        visibleFiles.push(file);
    }

    fileCountEl.textContent = `${visibleFiles.length}/${allFiles.length} files`;
    applyDirtyClassesToVisibleThumbnails();
    console.log(`Batch complete: ${visibleFiles.length}/${allFiles.length} files rendered`);
}

// Handle thumbnail click
function handleThumbnailClick(event, path, index) {
    // Flush unsaved form changes before switching selection
    if (formHasChanges && selectedFiles.length > 0) {
        flushPendingChanges([...selectedFiles], metaTitle.value, metaDescription.value, metaKeywords.value);
        formHasChanges = false;
    }

    const clickedIndex = index !== undefined ? index : allFiles.findIndex(f => f.path === path);

    if (event.shiftKey && lastSelectedIndex !== -1) {
        // Shift+click: select range from last selected to current
        const start = Math.min(lastSelectedIndex, clickedIndex);
        const end = Math.max(lastSelectedIndex, clickedIndex);

        // Clear selection and select range (use visibleFiles — matches display order in all views)
        selectedFiles = [];
        for (let i = start; i <= end; i++) {
            if (visibleFiles[i] && !selectedFiles.includes(visibleFiles[i].path)) {
                selectedFiles.push(visibleFiles[i].path);
            }
        }
    } else if (event.ctrlKey || event.metaKey || multiSelectMode) {
        // Cmd/Ctrl+click or multi-select mode: toggle single item
        const index = selectedFiles.indexOf(path);
        if (index > -1) {
            selectedFiles.splice(index, 1);
        } else {
            selectedFiles.push(path);
        }
        lastSelectedIndex = clickedIndex;
    } else {
        // Single select: clear and select this one
        selectedFiles = [path];
        lastSelectedIndex = clickedIndex;
    }

    updateThumbnailSelection();
    loadMetadata();
}

// Update thumbnail selection UI
function updateThumbnailSelection() {
    document.querySelectorAll('.thumbnail-item').forEach(item => {
        const path = item.dataset.path;
        item.classList.toggle('selected', selectedFiles.includes(path));
    });
    updateSelectAllBtn();
}

function updateSelectAllBtn() {
    const hasFiles = allFiles.length > 0;
    const display = hasFiles ? 'inline-block' : 'none';
    ['select-all-btn', 'deselect-all-btn', 'multi-select-btn'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.style.display = display;
    });
}

function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    sidebar.classList.toggle('collapsed');
}

function collapseOnMobile() {
    if (window.innerWidth < 768) {
        document.getElementById('sidebar')?.classList.add('collapsed');
    }
}

function toggleMultiSelectMode() {
    multiSelectMode = !multiSelectMode;
    const btn = document.getElementById('multi-select-btn');
    btn.classList.toggle('active', multiSelectMode);
    btn.textContent = multiSelectMode ? '⊞ Multi ✓' : '⊞ Multi';
}

function selectAll() {
    selectedFiles = allFiles.map(f => f.path);
    lastSelectedIndex = selectedFiles.length > 0 ? allFiles.length - 1 : -1;
    updateThumbnailSelection();
    loadMetadata();
}

async function selectEmptyMetadata() {
    await refreshMetadataSummaries(allFiles.map(f => f.path));
    syncMissingMetadataFlags(allFiles);
    syncMissingMetadataFlags(rawFiles);
    selectedFiles = allFiles.filter(fileMissingMetadata).map(f => f.path);
    lastSelectedIndex = selectedFiles.length > 0
        ? allFiles.findIndex(f => f.path === selectedFiles[selectedFiles.length - 1])
        : -1;
    updateThumbnailSelection();
    loadMetadata();
    setStatus(`Selected ${selectedFiles.length} empty metadata file${selectedFiles.length !== 1 ? 's' : ''}`, false);
    setTimeout(() => clearStatus(), 2500);
}

function deselectAll() {
    selectedFiles = [];
    lastSelectedIndex = -1;
    updateThumbnailSelection();
    loadMetadata();
}

// Load metadata for selected files
// @param {boolean} resetOriginalMetadata - If true, reset originalMetadata for change tracking
async function loadMetadata(resetOriginalMetadata = true) {
    if (selectedFiles.length === 0) {
        metadataRequestSeq += 1;
        metadataForm.style.display = 'none';
        metadataContent.style.display = 'flex';
        metadataContent.innerHTML = '<div class="no-selection"><p>Select files to view and edit metadata</p></div>';
        return;
    }

    const requestSeq = ++metadataRequestSeq;
    const pathsSnapshot = [...selectedFiles];

    try {
        const params = new URLSearchParams();
        pathsSnapshot.forEach(path => params.append('paths', path));

        const response = await fetch(`/api/metadata?${params}`);
        const data = await response.json();

        if (requestSeq !== metadataRequestSeq || !samePathList(selectedFiles, pathsSnapshot)) {
            return;
        }

        displayMetadata(data, resetOriginalMetadata, pathsSnapshot);
    } catch (error) {
        console.error('Error loading metadata:', error);
    }
}

function samePathList(a, b) {
    if (a.length !== b.length) return false;
    return a.every((path, index) => path === b[index]);
}

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// Display metadata in form
// @param {boolean} resetOriginalMetadata - If true, reset originalMetadata for change tracking
function displayMetadata(data, resetOriginalMetadata = true, pathsForDisplay = selectedFiles) {
    metadataContent.style.display = 'none';
    metadataForm.style.display = 'block';
    updateGeneratingIndicators(); // selection just changed — reflect it immediately, don't wait for the next job tick

    if (data.count === 1) {
        const filePath = pathsForDisplay[0] || '';
        const filename = filePath.split('/').pop();
        const noExt = filename.replace(/\.[^.]+$/, '');
        const dashBase = noExt.split('-')[0];           // drop -ARW-DXO, -CR3_DXO etc.
        const shortName = dashBase.replace(/_[A-Za-z].*$/, ''); // drop _raw_nr etc. but keep _3135
        const fileObj = allFiles.find(f => f.path === filePath);
        const folder = fileObj ? (fileObj.folder || filePath.substring(0, filePath.lastIndexOf('/'))) : '';
        const isED = folder.includes('_ED');
        selectedCountEl.innerHTML = shortName + (isED ? ' <span class="ed-badge">ED</span>' : '');
    } else {
        selectedCountEl.textContent = `${data.count} files selected`;
    }

    // Show Gen AI button for images and videos (videos use thumbnail frame)
    const genMetaBtn = document.getElementById('gen-meta-btn');
    const hasImages = pathsForDisplay.some(p => /\.(jpg|jpeg|png|heic|mp4|mov)$/i.test(p));
    genMetaBtn.style.display = hasImages ? 'inline-block' : 'none';
    genMetaBtn.title = pathsForDisplay.length > 1
        ? `Generate AI metadata for ${pathsForDisplay.length} images`
        : 'Generate stock metadata with AI';

    // Update allFiles with fresh is_dirty data from API
    pathsForDisplay.forEach(path => {
        const fileIndex = allFiles.findIndex(f => f.path === path);
        if (fileIndex !== -1) {
            allFiles[fileIndex].is_dirty = data.is_dirty;
        }
    });

    console.log('[displayMetadata] is_dirty from API:', data.is_dirty, 'selectedFiles:', pathsForDisplay);

    // Display capture date (EXIF) — single file only, format: YYYY:MM:DD HH:MM:SS
    const captureDateEl = document.getElementById('capture-date');
    if (data.capture_date && data.count === 1) {
        try {
            // EXIF format: "2025:04:04 10:46:05" → "2025-04-04T10:46"
            const parts = data.capture_date.slice(0, 10).replace(/:/g, '-') + 'T' + data.capture_date.slice(11, 16);
            const capDate = new Date(parts);
            captureDateEl.textContent = '📷 ' + formatDateCompact(capDate);
        } catch {
            captureDateEl.textContent = '📷 ' + data.capture_date.slice(0, 10);
        }
    } else {
        captureDateEl.textContent = '';
    }

    // Display modified date
    const modifiedDateEl = document.getElementById('modified-date');
    const gpsBadgeEl = document.getElementById('gps-badge');
    const dirtyIndicatorEl = document.getElementById('dirty-indicator');

    if (data.file_mtime_mixed) {
        modifiedDateEl.textContent = 'Mixed';
    } else if (data.file_mtime) {
        const modDate = new Date(data.file_mtime * 1000);
        modifiedDateEl.textContent = formatDateCompact(modDate);
    } else {
        modifiedDateEl.textContent = '';
    }

    if (gpsBadgeEl) {
        const hasGps = data.count === 1 && data.gps && Number.isFinite(Number(data.gps.lat)) && Number.isFinite(Number(data.gps.lon));
        if (hasGps) {
            const lat = Number(data.gps.lat);
            const lon = Number(data.gps.lon);
            gpsBadgeEl.href = `https://www.google.com/maps?q=${encodeURIComponent(`${lat},${lon}`)}`;
            gpsBadgeEl.title = `Open embedded GPS location in Google Maps (${lat.toFixed(6)}, ${lon.toFixed(6)})`;
            gpsBadgeEl.style.display = 'inline-flex';
        } else {
            gpsBadgeEl.removeAttribute('href');
            gpsBadgeEl.title = 'No embedded GPS location';
            gpsBadgeEl.style.display = 'none';
        }
    }

    // Show dirty indicator if any selected files are dirty
    const hasDirtyFiles = data.is_dirty;
    dirtyIndicatorEl.style.display = hasDirtyFiles ? 'inline' : 'none';

    // Title
    if (data.title_mixed) {
        metaTitle.value = '';
        metaTitle.placeholder = 'Mixed values';
    } else {
        metaTitle.value = data.title || '';
        metaTitle.placeholder = 'Title';
    }

    // Description
    if (data.description_mixed) {
        metaDescription.value = '';
        metaDescription.placeholder = 'Mixed values';
    } else {
        metaDescription.value = data.description || '';
        metaDescription.placeholder = 'Description';
    }

    // Keywords - convert array to comma-separated string
    if (data.keywords_mixed) {
        metaKeywords.value = '';
        metaKeywords.placeholder = 'Mixed values';
    } else {
        metaKeywords.value = data.keywords ? data.keywords.join(', ') : '';
        metaKeywords.placeholder = 'Keywords';
    }

    // Only reset originalMetadata when explicitly requested (initial load or after sync)
    // This prevents background refreshes from resetting change tracking
    if (resetOriginalMetadata) {
        console.log('displayMetadata: Resetting originalMetadata to', {
            title: metaTitle.value,
            description: metaDescription.value,
            keywords: metaKeywords.value,
        });

        originalMetadata = {
            title: metaTitle.value,
            description: metaDescription.value,
            keywords: metaKeywords.value,
        };

        formHasChanges = false;
    } else {
        console.log('displayMetadata: Keeping originalMetadata, just updating form values');
        // When not resetting, update form values but preserve originalMetadata for change detection
        // This means originalMetadata still has values from before the edit
    }

    // Single-file preview below Apply button
    const previewWrap = document.getElementById('single-preview-wrap');
    const previewImg = document.getElementById('single-preview-img');
    if (data.count === 1) {
        const filePath = pathsForDisplay[0];
        const isVideo = /\.(mp4|mov|avi|mkv)$/i.test(filePath);
        if (isVideo) {
            const fileObj = allFiles.find(f => f.path === filePath);
            if (fileObj && fileObj.thumb_url) {
                previewImg.src = `/thumbs/${fileObj.thumb_url.split('/').pop()}`;
                previewWrap.style.display = 'block';
            } else {
                previewWrap.style.display = 'none';
            }
        } else {
            previewImg.src = `/api/preview?path=${encodeURIComponent(filePath)}`;
            previewWrap.style.display = 'block';
        }
    } else {
        previewImg.src = '';
        previewWrap.style.display = 'none';
    }

    // Update apply and undo button states based on dirty status
    updateActionButtonStates();
}

// Update both apply and undo button states together based on dirty status
function updateActionButtonStates() {
    const hasDirtyFiles = selectedFiles.some(path => {
        const file = allFiles.find(f => f.path === path);
        return (file && file.is_dirty) || dirtyPathSet.has(path);
    });
    const hasSelection = selectedFiles.length > 0;

    const applyBtn = document.getElementById('apply-btn');
    applyBtn.disabled = !hasDirtyFiles && !formHasChanges;
    const undoBtn = document.getElementById('undo-btn');
    undoBtn.disabled = !hasDirtyFiles && !formHasChanges;
}

// Update metadata preview (on input change) - marks files as dirty in DB
async function updateMetadataPreview() {
    // Mark form as having changes
    formHasChanges = true;
    updateActionButtonStates();

    // Mark selected files as dirty in local state (UI dirty indicator)
    selectedFiles.forEach(path => {
        dirtyPathSet.add(path);
        const file = allFiles.find(f => f.path === path);
        if (file && !file.is_dirty) {
            file.is_dirty = true;
            // Add dirty indicator to thumbnail
            const thumbEl = document.querySelector(`.thumbnail-item[data-path="${CSS.escape(path)}"]`);
            if (thumbEl && !thumbEl.classList.contains('dirty')) {
                thumbEl.classList.add('dirty');
            }
        }
    });

    // Update dirty indicator dot next to modified date
    const dirtyIndicatorEl = document.getElementById('dirty-indicator');
    dirtyIndicatorEl.style.display = 'inline';

    loadDirtyCount();

    // Update database to mark files as dirty (don't sync to disk yet)
    // This allows undo/reload to recover original values
    debouncedMarkDirty();
}

// Immediately save pending form changes for a specific set of files/values
async function flushPendingChanges(paths, title, description, keywordsStr) {
    if (!paths || paths.length === 0) return;
    const keywords = keywordsStr
        ? (() => { const seen = new Set(); return keywordsStr.split(/[\n,]/).map(k => k.trim()).filter(k => k && !seen.has(k.toLowerCase()) && seen.add(k.toLowerCase())); })()
        : null;
    try {
        await fetch('/api/metadata', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ paths, title: title || null, description: description || null, keywords }),
        });
    } catch (error) {
        console.error('Error flushing pending changes:', error);
    }
}

// Debounced function to mark files as dirty in database
const debouncedMarkDirty = debounce(async () => {
    if (selectedFiles.length === 0 || !formHasChanges) return;

    await flushPendingChanges(selectedFiles, metaTitle.value, metaDescription.value, metaKeywords.value);
    console.log('Database marked as dirty for:', selectedFiles);
}, 1000);

// Apply metadata changes - save to DB, write to disk, clear dirty
async function applyMetadataChanges() {
    if (selectedFiles.length === 0) {
        console.log('applyMetadataChanges: No files selected');
        return;
    }

    // Snapshot selection now — user may click away while async writes are in progress
    const filesToSync = [...selectedFiles];

    // Check if any selected files are dirty (in DB or local state)
    const hasDirtyFiles = filesToSync.some(path => {
        const file = allFiles.find(f => f.path === path);
        return (file && file.is_dirty) || dirtyPathSet.has(path);
    });

    console.log('applyMetadataChanges: State check', {
        filesToSync,
        originalMetadata,
        currentFormValues: {
            title: metaTitle.value,
            description: metaDescription.value,
            keywords: metaKeywords.value,
        },
        formHasChanges,
        hasDirtyFiles,
    });

    // If no dirty files and no form changes, nothing to do
    if (!hasDirtyFiles && !formHasChanges) {
        console.log('applyMetadataChanges: No dirty files and no form changes');
        clearStatus();
        return;
    }

    // Show status in main status bar
    setStatus('Writing metadata to disk...', true);

    try {
        // Step 1: Only write form values to DB if the user actually edited the form.
        // If formHasChanges is false, DB already has the correct per-file metadata
        // (e.g. AI-generated) — skip the PUT to avoid overwriting all files with the
        // same form values.
        if (formHasChanges) {
            const keywordsStr = metaKeywords.value;
            const keywords = keywordsStr
                ? (() => { const seen = new Set(); return keywordsStr.split(/[\n,]/).map(k => k.trim()).filter(k => k && !seen.has(k.toLowerCase()) && seen.add(k.toLowerCase())); })()
                : null;
            const payload = {
                paths: filesToSync,
                title: metaTitle.value || null,
                description: metaDescription.value || null,
                keywords: keywords,
            };
            const dbResponse = await fetch('/api/metadata', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const dbResult = await dbResponse.json();
            if (!dbResult.success) throw new Error('Failed to update database');
        }

        // Step 2: Sync to disk. When no form edits, only sync files that are
        // actually dirty (skip clean files to avoid unnecessary exiftool writes).
        const pathsToSync = formHasChanges
            ? filesToSync
            : filesToSync.filter(p => { const f = allFiles.find(x => x.path === p); return (f && f.is_dirty) || dirtyPathSet.has(p); });

        let syncedCount = 0, failedCount = 0;
        for (let i = 0; i < pathsToSync.length; i++) {
            const path = pathsToSync[i];
            setStatus(`Syncing ${i + 1}/${pathsToSync.length}...`, true);
            try {
                const r = await fetch('/api/sync', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ paths: [path] }),
                });
                const res = await r.json();
                if (res.synced > 0) {
                    syncedCount++;
                    const file = allFiles.find(f => f.path === path);
                    dirtyPathSet.delete(path);
                    if (file) {
                        file.is_dirty = false;
                        const thumbEl = document.querySelector(`.thumbnail-item[data-path="${CSS.escape(path)}"]`);
                        if (thumbEl) thumbEl.classList.remove('dirty');
                    }
                } else {
                    failedCount++;
                }
            } catch (e) {
                failedCount++;
            }
        }

        if (failedCount === 0) {
            originalMetadata = {
                title: metaTitle.value,
                description: metaDescription.value,
                keywords: metaKeywords.value,
            };
            formHasChanges = false;
            updateActionButtonStates();
            setStatus(`✓ Synced ${syncedCount}/${pathsToSync.length} file(s) to disk`, false);
            setTimeout(() => clearStatus(), 3000);
            loadDirtyCount();
            if (isPendingSyncView) {
                removeFilesFromPendingView(pathsToSync);
            } else {
                loadMetadata(false);
            }
        } else {
            setStatus(`⚠ Synced ${syncedCount}, failed: ${failedCount}`, false);
            setTimeout(() => clearStatus(), 3000);
            loadDirtyCount();
        }
    } catch (error) {
        console.error('Error applying metadata:', error);
        setStatus('✗ Error writing metadata', false);
    }
}

// Undo changes for selected files - re-read metadata from disk
async function undoChanges() {
    if (selectedFiles.length === 0) {
        setStatus('No files selected', false);
        return;
    }

    setStatus(`Undoing changes for ${selectedFiles.length} file(s)...`, true);

    try {
        // Update each file individually - re-read from disk
        for (const path of selectedFiles) {
            // Read metadata from disk using the backend
            const params = new URLSearchParams();
            params.append('paths', path);

            const response = await fetch(`/api/metadata?${params}`);
            const data = await response.json();

            // Update database with fresh metadata from disk (clear dirty flag)
            const payload = {
                paths: [path],
                title: data.title || null,
                description: data.description || null,
                keywords: data.keywords || null,
            };

            await fetch('/api/metadata', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });

            // Clear dirty flag for this file
            await fetch('/api/sync', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ paths: [path] }),
            });
        }

        // Update local state - mark files as not dirty
        selectedFiles.forEach(path => {
            dirtyPathSet.delete(path);
            const file = allFiles.find(f => f.path === path);
            if (file) file.is_dirty = false;

            // Remove dirty indicator from thumbnail in place
            const thumbEl = document.querySelector(`.thumbnail-item[data-path="${CSS.escape(path)}"]`);
            if (thumbEl && thumbEl.classList.contains('dirty')) {
                thumbEl.classList.remove('dirty');
            }
        });

        // Refresh UI
        const undoneFiles = [...selectedFiles];
        loadDirtyCount();
        if (isPendingSyncView) {
            removeFilesFromPendingView(undoneFiles);
        } else {
            loadMetadata();
        }
        updateActionButtonStates();

        setStatus(`Undo complete for ${undoneFiles.length} file(s)`, false);
        setTimeout(() => clearStatus(), 2000);
    } catch (error) {
        console.error('Error undoing changes:', error);
        setStatus('Error undoing changes', false);
    }
}

// Re-read metadata from disk for current folder and clear dirty status
async function reReadMetadata() {
    if (!currentFolder) return;

    try {
        // Call API to re-scan current folder with metadata
        const response = await fetch('/api/load-folder', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                folder: currentFolder,
                refresh: true  // Flag to indicate we want to re-read metadata
            }),
        });

        const data = await response.json();
        const files = data.files || [];
        const totalFiles = data.total_files || files.length;
        const metadataLoading = data.metadata_loading || false;

        // Update allFiles with fresh data
        allFiles = files || [];

        // Re-fetch metadata for selected files to update the form
        // Skip if user has been editing to avoid overwriting their changes
        if (selectedFiles.length > 0 && !formHasChanges) {
            loadMetadata(false);
        }

        // Reload dirty count (dirty status cleared on server)
        loadDirtyCount();

        // Re-render thumbnails to update dirty indicators
        if (allFiles.length > 0) {
            renderThumbnailsOneByOne();
        }

        // If metadata is loading in background, poll for completion
        if (metadataLoading) {
            let checkCount = 0;
            const pollingFolder = currentFolder;

            setStatus(`Re-reading metadata... (0/${totalFiles})`, true);

            const refreshPollInterval = setInterval(() => {
                // Don't update if user has navigated to a different folder
                if (currentFolder !== pollingFolder) {
                    clearInterval(refreshPollInterval);
                    return;
                }

                checkCount++;

                // Poll progress endpoint for accurate count
                fetch(`/api/refresh-progress?folder=${encodeURIComponent(pollingFolder)}`)
                    .then(r => r.json())
                    .then(progress => {
                        // Double-check we're still on the same folder
                        if (currentFolder !== pollingFolder) {
                            return;
                        }

                        const { processed, total } = progress;

                        // Check if still loading via main endpoint
                        fetch(`/api/load-folder`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ folder: pollingFolder }),
                        }).then(d2 => d2.json()).then(d => {
                            const stillLoadingMeta = d.metadata_loading || false;

                            if (!stillLoadingMeta || checkCount > 60 || processed === total) {
                                clearInterval(refreshPollInterval);
                                setStatus(`Re-read metadata for ${total || files.length} files`, false);
                                setTimeout(() => clearStatus(), 2000);
                                // Final update to ensure all data is shown
                                if (d.files) {
                                    allFiles = d.files;
                                    renderThumbnailsOneByOne();
                                }
                            } else {
                                // Update progress from actual scan progress
                                setStatus(`Re-reading metadata... (${processed}/${total})`, true);
                            }
                        }).catch(() => {});
                    }).catch(() => {});
            }, 500);  // Poll every 500ms for smoother progress
        } else {
            setStatus(`Re-read metadata for ${allFiles.length} files`, false);
            setTimeout(() => clearStatus(), 2000);
        }
    } catch (error) {
        console.error('Error re-reading metadata:', error);
        setStatus('Error re-reading metadata', false);
    }
}

// Append keywords to selection
async function guessLocation() {
    if (selectedFiles.length === 0) return;
    const btn = document.getElementById('guess-location-btn');
    btn.disabled = true;
    btn.textContent = 'Guessing…';
    try {
        const path = selectedFiles[0];
        const resp = await fetch('/api/guess-landmarks', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path, location_override: getLocationOverride() }),
        });
        const data = await resp.json();
        if (data.error) { setStatus(`Guess failed: ${data.error}`, false); return; }
        const landmarks = data.landmarks || [];
        if (landmarks.length === 0) {
            setStatus('No landmarks identified', false);
        } else {
            landmarks.forEach(l => addPendingTag(l, true));
            setStatus(`Found ${landmarks.length} landmark(s) — review tags then click Add to selection`, false);
        }
    } catch (e) {
        setStatus(`Guess error: ${e.message}`, false);
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:middle;margin-right:3px"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>Guess Location';
    }
}

async function appendKeywords() {
    if (selectedFiles.length === 0) return;

    // Flush any text still in the input as a tag
    const inputVal = keywordAppend.value.replace(/,/g, '').trim();
    if (inputVal) { addPendingTag(inputVal); keywordAppend.value = ''; }

    const newKeywords = pendingKeywords.map(t => t.text).filter(k => k);
    if (newKeywords.length === 0) return;

    try {
        // Fetch and update each file individually
        for (const path of selectedFiles) {
            // Get this file's current keywords
            const params = new URLSearchParams();
            params.append('paths', path);

            const response = await fetch(`/api/metadata?${params}`);
            const data = await response.json();

            // Get existing keywords for this file
            let existingKeywords = data.keywords || [];

            // Add new keywords (skip duplicates, case-insensitive)
            const existingLower = existingKeywords.map(k => k.toLowerCase());
            const mergedKeywords = [...existingKeywords];
            for (const newKw of newKeywords) {
                if (!existingLower.includes(newKw.toLowerCase())) {
                    mergedKeywords.push(newKw);
                    existingLower.push(newKw.toLowerCase());
                }
            }

            // Update this file's keywords
            const payload = {
                paths: [path],
                keywords: mergedKeywords,
            };

            await fetch('/api/metadata', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });

            // Update local state - mark file as dirty
            const file = allFiles.find(f => f.path === path);
            dirtyPathSet.add(path);
            if (file) {
                file.is_dirty = true;
            }
        }

        // Update thumbnail dirty indicators
        selectedFiles.forEach(path => {
            const thumbEl = document.querySelector(`.thumbnail-item[data-path="${CSS.escape(path)}"]`);
            if (thumbEl && !thumbEl.classList.contains('dirty')) {
                thumbEl.classList.add('dirty');
            }
        });

        // Clear tags and input
        pendingKeywords = [];
        renderTagChips();
        keywordAppend.value = '';

        // Reload form to show updated keywords (reset originalMetadata to new values)
        // Files are already dirty in DB, so Apply Changes will write to disk
        await loadMetadata(true);
        loadDirtyCount();

        setStatus(`Added ${newKeywords.length} keyword(s) to ${selectedFiles.length} file(s)`, false);
        setTimeout(() => clearStatus(), 2000);
    } catch (error) {
        console.error('Error appending keywords:', error);
    }
}

// Sync all dirty files
async function syncAllChanges() {
    const syncBtn = document.getElementById('sync-btn');
    syncBtn.disabled = true;

    setStatus(`Loading pending files...`, true);

    try {
        const dirtyResp = await fetch('/api/dirty');
        const dirtyFiles = await dirtyResp.json();
        const total = dirtyFiles.length;

        if (total === 0) {
            setStatus('Nothing to sync', false);
            setTimeout(() => clearStatus(), 2000);
            return;
        }

        setBackgroundStatus(`Writing ${total} file(s)...`, true);
        let syncedCount = 0, failedCount = 0;

        for (let i = 0; i < dirtyFiles.length; i++) {
            const path = dirtyFiles[i].path;
            setStatus(`Syncing ${i + 1}/${total}...`, true);
            try {
                const r = await fetch('/api/sync', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ paths: [path] }),
                });
                const res = await r.json();
                if (res.synced > 0) {
                    syncedCount++;
                    dirtyCount = Math.max(0, dirtyCount - 1);
                    const pendingSyncCount = document.getElementById('pending-sync-count');
                    if (pendingSyncCount) pendingSyncCount.textContent = dirtyCount;
                    document.getElementById('pending-sync-tab')?.classList.toggle('has-pending', dirtyCount > 0);
                    const file = allFiles.find(f => f.path === path);
                    dirtyPathSet.delete(path);
                    if (file) {
                        file.is_dirty = false;
                        const thumbEl = document.querySelector(`.thumbnail-item[data-path="${CSS.escape(path)}"]`);
                        if (thumbEl) thumbEl.classList.remove('dirty');
                    }
                } else {
                    failedCount++;
                }
            } catch (e) {
                failedCount++;
            }
        }

        if (failedCount === 0) {
            setStatus(`✓ Synced ${syncedCount} file(s) to disk`, false);
        } else {
            setStatus(`⚠ Synced ${syncedCount}, failed: ${failedCount}`, false);
        }
        setTimeout(() => clearStatus(), 3000);
        loadDirtyCount();
        setBackgroundStatus('Idle', false);
    } catch (error) {
        console.error('Error syncing:', error);
        setStatus('✗ Error syncing files', false);
        setBackgroundStatus('Idle', false);
    } finally {
        syncBtn.disabled = false;
    }
}

// A single-file edit should become pending as soon as the user leaves the
// field.  `change` fires only after a real user edit and focus loss, so it
// does not run while metadata is being populated programmatically.
async function saveSingleMetadataOnBlur() {
    if (selectedFiles.length !== 1) return;

    const titleInput = document.getElementById('meta-title');
    const descriptionInput = document.getElementById('meta-description');
    const keywordsInput = document.getElementById('meta-keywords');
    if (!titleInput || !descriptionInput || !keywordsInput) return;

    const path = selectedFiles[0];
    const keywords = keywordsInput.value
        .split(',')
        .map(keyword => keyword.trim())
        .filter(Boolean);

    try {
        const response = await fetch('/api/metadata', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                paths: [path],
                title: titleInput.value.trim(),
                description: descriptionInput.value.trim(),
                keywords,
            }),
        });
        if (!response.ok) throw new Error('metadata update failed');

        dirtyPathSet.add(path);
        const file = allFiles.find(item => item.path === path);
        if (file) {
            file.is_dirty = true;
            file.title = titleInput.value.trim();
            file.keywords = keywords;
        }
        const thumbEl = document.querySelector(`.thumbnail-item[data-path="${CSS.escape(path)}"]`);
        if (thumbEl) thumbEl.classList.add('dirty');
        document.getElementById('dirty-indicator').style.display = 'inline';
        loadDirtyCount();
    } catch (error) {
        console.error('Error auto-saving single-file metadata:', error);
        setStatus('Could not save metadata change', false);
    }
}

document.getElementById('meta-title')?.addEventListener('change', saveSingleMetadataOnBlur);
document.getElementById('meta-description')?.addEventListener('change', saveSingleMetadataOnBlur);
document.getElementById('meta-keywords')?.addEventListener('change', saveSingleMetadataOnBlur);

// Load dirty count
async function loadDirtyCount() {
    try {
        const response = await fetch('/api/dirty');
        const dirty = await response.json();
        dirtyCount = dirty.length;
        dirtyPathSet = new Set(dirty.map(f => f.path));

        // Update folder dirty dots
        const dirtyFolders = new Set(dirty.map(f => f.folder));
        document.querySelectorAll('.folder-item[data-folder] .dirty-dot').forEach(dot => {
            const folderPath = dot.closest('[data-folder]').dataset.folder;
            dot.classList.toggle('visible', dirtyFolders.has(folderPath));
        });

        // Keep the visible thumbnail orange state in sync with the DB.
        // This catches server-side jobs that finish while the page is refreshing
        // or before the current folder thumbnails have finished rendering.
        syncDirtyFlagsForFiles(rawFiles);
        syncDirtyFlagsForFiles(allFiles);
        syncDirtyFlagsForFiles(visibleFiles);
        applyDirtyClassesToVisibleThumbnails();

        if (selectedFiles.length > 0) {
            const selectedDirty = selectedFiles.some(path => dirtyPathSet.has(path));
            document.getElementById('dirty-indicator').style.display = selectedDirty ? 'inline' : 'none';
        }

        // Update pending sync tab count and highlight
        const pendingSyncTab = document.getElementById('pending-sync-tab');
        const pendingSyncCount = document.getElementById('pending-sync-count');
        if (pendingSyncCount) pendingSyncCount.textContent = dirtyCount;
        if (pendingSyncTab) pendingSyncTab.classList.toggle('has-pending', dirtyCount > 0);

        const syncBtn = document.getElementById('sync-btn');
        const syncDirtyCount = document.getElementById('sync-dirty-count');
        if (syncBtn) {
            syncBtn.classList.toggle('has-dirty', dirtyCount > 0);
            syncBtn.title = dirtyCount > 0
                ? `Sync ${dirtyCount} pending change${dirtyCount === 1 ? '' : 's'} to disk`
                : 'Sync All Changes to disk';
        }
        if (syncDirtyCount) {
            syncDirtyCount.textContent = dirtyCount > 99 ? '99+' : String(dirtyCount);
            syncDirtyCount.style.display = dirtyCount > 0 ? 'inline-block' : 'none';
        }
    } catch (error) {
        console.error('Error loading dirty count:', error);
    }
}

// Refresh database - scan all configured roots
async function refreshDatabase() {
    const refreshBtn = document.getElementById('refresh-btn');
    refreshBtn.disabled = true;

    // Clear pre-fetch cache on full refresh
    prefetchedFolders.clear();

    setStatus('Scanning folders...', true);

    const roots = FOLDER_ROOTS;
    if (!roots.length) {
        setStatus('No watched folders configured — add one first', false);
        refreshBtn.disabled = false;
        setTimeout(() => clearStatus(), 5000);
        return;
    }

    let totalCount = 0;
    let scannedRoots = [];

    try {
        // Scan each root folder progressively
        for (const root of roots) {
            setStatus(`Scanning: ${root}...`, true);

            const response = await fetch('/api/scan', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ folder: root }),
            });

            const result = await response.json();

            if (result.success && result.count > 0) {
                totalCount += result.count;
                scannedRoots.push(result);
                setStatus(`Scanned ${result.count} files from ${root}`, false);
                // Small delay to let user see progress
                await new Promise(r => setTimeout(r, 200));
            }
        }

        if (totalCount > 0) {
            setStatus(`Total: ${totalCount} files scanned`, false);
            loadFolders();
            if (currentFolder) {
                selectFolder(currentFolder);
            }
            loadDirtyCount();
        } else {
            setStatus('No files found in configured folders', false);
        }

        // Clear status after 5 seconds
        setTimeout(() => clearStatus(), 5000);
    } catch (error) {
        console.error('Error refreshing:', error);
        setStatus('Error during refresh', false);
    } finally {
        refreshBtn.disabled = false;
    }
}

// Scan a single root folder
async function scanRootFolder(rootPath) {
    setStatus(`Scanning: ${rootPath}...`, true);

    try {
        const response = await fetch('/api/scan', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ folder: rootPath }),
        });

        const result = await response.json();

        if (result.success) {
            setStatus(`Scanned ${result.count} files from ${rootPath}`, false);
            loadFolders();
            if (currentFolder && currentFolder.startsWith(rootPath)) {
                selectFolder(currentFolder);  // Refresh current folder
            }
            setTimeout(() => clearStatus(), 3000);
        } else {
            setStatus('Scan completed', false);
            setTimeout(() => clearStatus(), 2000);
        }
    } catch (error) {
        console.error('Error scanning:', error);
        setStatus('Error scanning folder', false);
    }
}

// Open file in Finder
async function openInFinder(path) {
    console.log('[openInFinder] called with path:', path, 'selectedFiles:', selectedFiles);

    // If no path provided, use first selected file
    if (!path && selectedFiles.length > 0) {
        path = selectedFiles[0];
    }

    console.log('[openInFinder] using path:', path);

    if (!path) {
        console.error('No file selected to open in Finder');
        return;
    }

    try {
        const response = await fetch('/api/open-finder', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path }),
        });

        const result = await response.json();
        console.log('[openInFinder] result:', result);

        if (!result.success) {
            console.error('Error opening in Finder:', result.error);
        }
    } catch (error) {
        console.error('Error opening in Finder:', error);
    }
}

// =============================================================================
// Export / Upload Tab
// =============================================================================

let exportFolders = [];
let exportConfiguredSites = [];
let exportCategories = [];
let activeExportSSE = null;

// Tab switching
document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const tab = btn.dataset.tab;
        activeTab = tab;
        document.getElementById('metadata-panel').style.display = tab === 'metadata' ? '' : 'none';
        document.getElementById('export-panel').style.display  = tab === 'export' ? '' : 'none';

        document.getElementById('meta-panel-title').textContent = 'Metadata';
        if (tab === 'metadata') loadFolders(); // reload normal folder tree

        if (tab === 'export' && exportFolders.length === 0) {
            fetch('/api/export/mount-drives', { method: 'POST' }).catch(() => {})
                .finally(() => loadExportFolders());
        }

        if (filterText.trim().length >= 2) runSearch(filterText.trim());
    });
});

document.getElementById('export-scan-btn').addEventListener('click', loadExportFolders);
document.getElementById('export-mount-btn').addEventListener('click', async () => {
    const btn = document.getElementById('export-mount-btn');
    btn.textContent = '⏳ Mounting...';
    btn.disabled = true;
    try {
        await fetch('/api/export/mount-drives', { method: 'POST' });
        await loadExportFolders();
    } finally {
        btn.textContent = '⏏ Mount Drives';
        btn.disabled = false;
    }
});
document.getElementById('export-log-close').addEventListener('click', () => {
    document.getElementById('export-log').style.display = 'none';
    if (activeExportSSE) { activeExportSSE.close(); activeExportSSE = null; }
});

async function loadExportFolders() {
    const imageTbody = document.getElementById('export-folder-list-image');
    const clipTbody  = document.getElementById('export-folder-list-clip');
    const loadingRow = '<tr><td colspan="4" style="text-align:center;color:#64748b;padding:24px;">Loading...</td></tr>';
    imageTbody.innerHTML = loadingRow;
    clipTbody.innerHTML = loadingRow;
    try {
        const res  = await fetch('/api/export/folders');
        const data = await res.json();
        exportFolders    = data.folders    || [];
        exportCategories = data.categories || [];
        const configured = data.configured_sites || [];
        exportConfiguredSites = configured;
        const mountBtn = document.getElementById('export-mount-btn');
        if (mountBtn) mountBtn.style.display = (data.network_drives || []).length ? 'inline-flex' : 'none';
        renderExportTable(configured);
    } catch (e) {
        const err = `<tr><td colspan="4" style="color:#f87171;padding:12px;">Error: ${e.message}</td></tr>`;
        imageTbody.innerHTML = err;
        clipTbody.innerHTML = err;
    }
}

function rerenderExportTableForJobs() {
    if (activeTab === 'export' && exportFolders.length) {
        renderExportTable(exportConfiguredSites);
    }
}

function activeFolderLockJob(folderPath) {
    if (!folderPath) return null;
    return [...activeJobs.values()].find(job => {
        if (!['queued', 'running'].includes(job.status)) return false;
        if (job.type !== 'export_upload') return false;
        return job.payload?.folder_path === folderPath || job.payload?.folder === folderPath;
    }) || null;
}

function exportRowJobProgress(job) {
    if (!job) return 0;
    if (typeof job.progress === 'number') return Math.max(0, Math.min(100, job.progress));
    const total = job.total || 0;
    if (!total) return job.status === 'running' ? 5 : 0;
    return Math.max(0, Math.min(100, Math.round((((job.done || 0) + (job.failed || 0)) / total) * 100)));
}

function exportRowJobLabel(job) {
    if (!job) return '';
    const action = 'Uploading';
    const elapsed = currentJobElapsed(job);
    const elapsedText = elapsed ? ` (${elapsed})` : '';
    const current = job.current_label ? ` - ${job.current_label}${elapsedText}` : elapsedText;
    return `${action}${current}`;
}

function renderExportRow(folder, idx, configuredSites) {
    const selectedCategory = document.getElementById(`cat-${idx}`)?.value || 'AI_AUTO';
    const typeIcon  = folder.type === 'clips' ? '🎞' : '🖼';
    const typeTitle = folder.type === 'clips' ? 'video clips' : 'images';
    const typeCls   = folder.type === 'clips' ? 'clip' : 'image';
    const edBadge   = folder.is_editorial ? '<span class="folder-ed-badge">ED</span>' : '';
    const metaPct   = folder.file_count ? Math.round((folder.metadata_count / folder.file_count) * 100) : 0;
    const csvDot    = folder.has_csvs
        ? '<span class="csv-indicator csv-ready" title="CSV ready"></span>'
        : '<span class="csv-indicator csv-missing" title="No CSV yet"></span>';
    const uploadStatus = folder.site_upload_status || {};
    const recSites = folder.recommended_sites || [];
    const lockJob = activeFolderLockJob(folder.path);
    const rowLocked = !!lockJob;
    const disabledAttr = rowLocked ? 'disabled' : '';
    const hasCredentials = recSites.some(s => configuredSites.includes(s));
    const uploadDisabledAttr = rowLocked || !hasCredentials ? 'disabled' : '';
    const uploadTitle = !hasCredentials
        ? 'No FTP/SFTP credentials configured for this folder\'s sites — set them under 🔑 Site Credentials first'
        : 'Upload';
    const sites = recSites
        .map(s => {
            if (uploadStatus[s] === 'full')
                return `<span style="color:#22c55e" title="Fully uploaded">✔</span> ${s}`;
            if (uploadStatus[s] === 'partial')
                return `<span style="color:#facc15" title="Partially uploaded">✔</span> ${s}`;
            if (configuredSites.includes(s))
                return `<span style="color:#4f6ef7" title="Credentials configured">●</span> ${s}`;
            return `<span style="color:#475569" title="No credentials">○</span> ${s}`;
        })
        .join(' &nbsp; ');
    const rowPct = exportRowJobProgress(lockJob);
    const rowJobHtml = rowLocked
        ? `<div class="folder-meta export-row-job">
                <span>${exportRowJobLabel(lockJob)} (${Math.round(rowPct)}%)</span>
                <span class="export-row-progress"><span style="width:${rowPct}%;"></span></span>
            </div>`
        : '';

    const categoryOptions = (selectedValue = 'AI_AUTO') => ['AI Auto', ...exportCategories]
        .map(c => {
            const value = c === 'AI Auto' ? 'AI_AUTO' : c;
            return `<option value="${value}" ${value === selectedValue ? 'selected' : ''}>${c}</option>`;
        })
        .join('');

    return `<tr data-idx="${idx}" class="${rowLocked ? 'export-row-busy' : ''}">
        <td>
            <div class="folder-name">${csvDot}<span class="export-type-icon export-type-${typeCls}" title="${typeTitle}">${typeIcon}</span> ${edBadge} ${folder.name}</div>
            <div class="folder-meta">${folder.path}</div>
            <div class="folder-meta" style="margin-top:3px;">${sites}</div>
            ${rowJobHtml}
        </td>
        <td>
            <div>${metaPct}% (${folder.metadata_count}/${folder.file_count})</div>
            <div style="height:4px;background:#1e2130;border-radius:2px;margin-top:4px;width:80px;">
                <div style="height:100%;width:${metaPct}%;background:#4f6ef7;border-radius:2px;"></div>
            </div>
        </td>
        <td><select class="cat-select" id="cat-${idx}" ${disabledAttr}>${categoryOptions(selectedCategory)}</select></td>
        <td>
            <div class="export-actions">
                <button class="btn-secondary" onclick="startGenerateCSV(${idx})" ${disabledAttr} style="font-size:0.75rem;padding:4px 8px;" title="Generate CSV">CSV</button>
                ${folder.has_csvs ? `<button class="btn-secondary" onclick="downloadCSV(${idx},'shutterstock')" ${disabledAttr} style="font-size:0.75rem;padding:4px 8px;">↓ SS</button>` : ''}
                ${folder.has_csvs && folder.is_editorial ? `<button class="btn-secondary" onclick="downloadCSV(${idx},'pond5')" ${disabledAttr} style="font-size:0.75rem;padding:4px 8px;">↓ P5</button>` : ''}
                <button class="btn-secondary" onclick="startUpload(${idx})" ${uploadDisabledAttr} style="font-size:0.75rem;padding:4px 8px;background:#1a3a2a;border-color:#166534;color:#4ade80;${!hasCredentials ? 'opacity:0.45;cursor:not-allowed;' : ''}" title="${uploadTitle}">⬆</button>
            </div>
        </td>
    </tr>`;
}

function renderExportTable(configuredSites) {
    const imageTbody = document.getElementById('export-folder-list-image');
    const clipTbody  = document.getElementById('export-folder-list-clip');
    const imagePanel = document.getElementById('export-images-panel');
    const clipPanel  = document.getElementById('export-clips-panel');
    const noCredBanner = document.getElementById('export-no-credentials-banner');

    if (!imageTbody || !clipTbody) return;

    if (noCredBanner) noCredBanner.style.display = configuredSites.length ? 'none' : 'block';

    const noDataRow = '<tr><td colspan="4" style="text-align:center;color:#64748b;padding:24px;">No folders found in database.</td></tr>';
    const folders = exportFolders.map((f, idx) => ({ folder: f, idx }));
    const imageRows = folders.filter(x => x.folder.type !== 'clips');
    const clipRows  = folders.filter(x => x.folder.type === 'clips');

    imageTbody.innerHTML = imageRows.length ? imageRows.map(x => renderExportRow(x.folder, x.idx, configuredSites)).join('') : noDataRow;
    clipTbody.innerHTML = clipRows.length ? clipRows.map(x => renderExportRow(x.folder, x.idx, configuredSites)).join('') : noDataRow;

    imagePanel.style.display = 'block';
    clipPanel.style.display = 'block';
}

function scrollExportPanelTo(panelId) {
    const wrap = document.getElementById('export-table-wrap');
    const target = document.getElementById(panelId);
    if (!wrap || !target) return;

    const wrapRect = wrap.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();
    const nextTop = wrap.scrollTop + (targetRect.top - wrapRect.top) - 50;

    wrap.scrollTo({
        top: Math.max(0, nextTop),
        behavior: 'smooth'
    });

    document.querySelectorAll('.export-quick-link').forEach(btn => {
        btn.classList.remove('is-active');
    });
    const activeBtn = document.querySelector(`.export-quick-link[data-export-target="${panelId}"]`);
    if (activeBtn) activeBtn.classList.add('is-active');
}

function openExportLog(title) {
    const log = document.getElementById('export-log');
    document.getElementById('export-log-title').textContent = title;
    document.getElementById('export-log-body').textContent = '';
    log.style.display = 'flex';
}

function appendExportLog(text) {
    const body = document.getElementById('export-log-body');
    body.textContent += text + '\n';
    body.scrollTop = body.scrollHeight;
}

async function startGenerateCSV(idx) {
    const folder = exportFolders[idx];
    if (!folder) return;
    const catEl    = document.getElementById(`cat-${idx}`);
    const category = catEl ? catEl.value : 'AI_AUTO';

    setStatus(`Starting CSV generation for ${folder.name}...`, true);
    try {
        const data = await startServerJob('/api/jobs/export-csv', {
            folder_path: folder.path,
            category,
        });
        setStatus('CSV generation job started', false);
        openJobLog(data.job_id || data.job?.id);
    } catch (e) {
        setStatus(`CSV generation error: ${e.message}`, false);
        alert(`CSV generation error: ${e.message}`);
    }
}

function downloadCSV(idx, site) {
    const folder = exportFolders[idx];
    if (!folder) return;
    const url = `/api/export/download?folder=${encodeURIComponent(folder.path)}&site=${site}`;
    window.open(url, '_blank');
}

// Generic confirm dialog
function showConfirmDialog(title, message, onConfirm) {
    document.getElementById('confirm-dialog-title').textContent = title;
    document.getElementById('confirm-dialog-message').textContent = message;
    document.getElementById('confirm-dialog-overlay').style.display = 'flex';
    document.getElementById('confirm-dialog-yes').onclick = () => {
        hideConfirmDialog();
        onConfirm();
    };
}
function hideConfirmDialog() {
    document.getElementById('confirm-dialog-overlay').style.display = 'none';
}

async function startUpload(idx) {
    const folder = exportFolders[idx];
    if (!folder) return;

    setStatus(`Starting upload for ${folder.name}...`, true);
    try {
        const data = await startServerJob('/api/jobs/export-upload', {
            folder_path: folder.path,
        });
        setStatus('Upload job started', false);
        openJobLog(data.job_id || data.job?.id);
    } catch (e) {
        setStatus(`Upload error: ${e.message}`, false);
        alert(`Upload error: ${e.message}`);
    }
}

function getLocationOverride() {
    const sel = document.getElementById('location-override');
    return sel ? sel.value : '';
}

let recentLocations = [];
let locationsData = [];      // [{group, entries:[{label,value}]}] — loaded from server
let locationsEditData = null; // working copy while modal is open
let modelsData = [];          // [{label,value}] — loaded from server
let modelsEditData = null;    // working copy while modal is open
let activeModelId = '';       // currently active model ID from server
let pendingKeywords = []; // {text, isGuess}

function addPendingTag(text, isGuess = false) {
    if (!text || pendingKeywords.find(t => t.text.toLowerCase() === text.toLowerCase())) return;
    pendingKeywords.push({ text, isGuess });
    renderTagChips();
}

function renderTagChips() {
    const container = document.getElementById('tag-chips');
    if (!container) return;
    container.innerHTML = '';
    pendingKeywords.forEach((tag, i) => {
        const chip = document.createElement('span');
        chip.className = 'tag-chip' + (tag.isGuess ? ' tag-chip-guess' : '');
        chip.innerHTML = `${tag.text}<button class="tag-chip-remove" title="Remove">&times;</button>`;
        chip.querySelector('.tag-chip-remove').addEventListener('click', (e) => {
            e.stopPropagation();
            pendingKeywords.splice(i, 1);
            renderTagChips();
        });
        container.appendChild(chip);
    });
}

async function loadModels() {
    try {
        const [modRes, cfgRes] = await Promise.all([
            fetch('/api/models'),
            fetch('/api/config'),
        ]);
        const modData = await modRes.json();
        modelsData = modData.models || modData;
        const cfg = await cfgRes.json();
        activeModelId = cfg.model;
        renderModelDropdown(cfg.model);

        // If the server's active model was deleted from the list (e.g. after
        // editing the model list), the dropdown silently falls back to
        // showing its first option as "selected" with no server-side change
        // — every generation call would keep using the stale, no-longer-
        // listed model underneath. Detect that and push the correction.
        const sel = document.getElementById('model-selector');
        if (sel && sel.value && sel.value !== cfg.model) {
            activeModelId = sel.value;
            fetch('/api/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model: sel.value })
            }).catch(() => {});
        }
    } catch(e) {
        console.error('Failed to load models', e);
    }
}

function renderModelDropdown(activeModel) {
    const sel = document.getElementById('model-selector');
    if (!sel) return;
    const current = activeModel ?? sel.value;
    sel.innerHTML = '';
    for (const m of modelsData) {
        const opt = document.createElement('option');
        opt.value = m.value;
        opt.textContent = m.label;
        sel.appendChild(opt);
    }
    if (current && [...sel.options].some(o => o.value === current)) sel.value = current;
}

function openModelsModal() {
    modelsEditData = JSON.parse(JSON.stringify(modelsData));
    renderModelsModalBody();
    loadLlmEndpoint();
    loadGoogleMapsKey();
    document.getElementById('models-modal-overlay').style.display = 'flex';
}

async function loadLlmEndpoint() {
    const status = document.getElementById('llm-endpoint-status');
    try {
        const res = await fetch('/api/llm-config');
        const data = await res.json();
        document.getElementById('llm-base-url-input').value = data.base_url || '';
        document.getElementById('llm-base-url-input').placeholder = data.default_base_url || 'https://api.openai.com';
        document.getElementById('llm-api-key-input').placeholder = data.has_api_key ? 'Key saved — leave blank to keep it' : 'API key';
        document.getElementById('llm-api-key-input').value = '';
        if (status) status.textContent = data.has_api_key ? '🔑 Key configured' : '⚠️ No API key set yet';
    } catch (e) {
        if (status) status.textContent = `Error loading endpoint: ${e.message}`;
    }
}

async function saveLlmEndpoint() {
    const btn = document.getElementById('llm-endpoint-save-btn');
    const status = document.getElementById('llm-endpoint-status');
    const baseUrl = document.getElementById('llm-base-url-input').value.trim();
    const apiKey = document.getElementById('llm-api-key-input').value.trim();
    btn.disabled = true;
    btn.textContent = 'Saving…';
    try {
        const res = await fetch('/api/llm-config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ base_url: baseUrl, api_key: apiKey }),
        });
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || `Save failed (${res.status})`);
        document.getElementById('llm-api-key-input').value = '';
        document.getElementById('llm-api-key-input').placeholder = data.has_api_key ? 'Key saved — leave blank to keep it' : 'API key';
        if (status) status.textContent = data.has_api_key ? '✔ Saved — 🔑 Key configured' : '✔ Saved — ⚠️ No API key set yet';
    } catch (e) {
        if (status) status.textContent = `Error: ${e.message}`;
    } finally {
        btn.disabled = false;
        btn.textContent = 'Save Endpoint';
    }
}

async function loadGoogleMapsKey() {
    const status = document.getElementById('google-maps-key-status');
    const input = document.getElementById('google-maps-key-input');
    try {
        const res = await fetch('/api/google-maps-key');
        const data = await res.json();
        input.value = '';
        input.placeholder = data.has_api_key ? 'Key saved — leave blank to keep it' : 'API key';
        if (status) status.textContent = data.has_api_key ? '🔑 Key configured' : 'Not set — OSM-based location keywords still work';
    } catch (e) {
        if (status) status.textContent = `Error loading key: ${e.message}`;
    }
}

async function saveGoogleMapsKey() {
    const btn = document.getElementById('google-maps-key-save-btn');
    const status = document.getElementById('google-maps-key-status');
    const input = document.getElementById('google-maps-key-input');
    const apiKey = input.value.trim();
    if (!apiKey) {
        if (status) status.textContent = 'Nothing to save — field is blank';
        return;
    }
    btn.disabled = true;
    btn.textContent = 'Saving…';
    try {
        const res = await fetch('/api/google-maps-key', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_key: apiKey }),
        });
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || `Save failed (${res.status})`);
        input.value = '';
        input.placeholder = data.has_api_key ? 'Key saved — leave blank to keep it' : 'API key';
        if (status) status.textContent = data.has_api_key ? '✔ Saved — 🔑 Key configured' : '✔ Saved';
    } catch (e) {
        if (status) status.textContent = `Error: ${e.message}`;
    } finally {
        btn.disabled = false;
        btn.textContent = 'Save Key';
    }
}

async function clearGoogleMapsKey() {
    const status = document.getElementById('google-maps-key-status');
    const input = document.getElementById('google-maps-key-input');
    try {
        const res = await fetch('/api/google-maps-key', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ clear: true }),
        });
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || `Clear failed (${res.status})`);
        input.value = '';
        input.placeholder = 'API key';
        if (status) status.textContent = 'Cleared — OSM-based location keywords still work';
    } catch (e) {
        if (status) status.textContent = `Error: ${e.message}`;
    }
}

function closeModelsModal() {
    document.getElementById('models-modal-overlay').style.display = 'none';
    modelsEditData = null;
}

function renderModelsModalBody() {
    const body = document.getElementById('models-modal-body');
    body.innerHTML = '';
    modelsEditData.forEach((m, i) => {
        const row = document.createElement('div');
        row.className = 'loc-entry-row';

        const labelInput = document.createElement('input');
        labelInput.className = 'loc-label';
        labelInput.value = m.label;
        labelInput.placeholder = 'Label (e.g. Gemma4 26B)';
        labelInput.oninput = e => { modelsEditData[i].label = e.target.value; };

        const valueInput = document.createElement('input');
        valueInput.className = 'loc-value';
        valueInput.value = m.value;
        valueInput.placeholder = 'Model ID';
        valueInput.oninput = e => { modelsEditData[i].value = e.target.value; };

        const delBtn = document.createElement('button');
        delBtn.className = 'loc-del-btn';
        delBtn.textContent = '✕';
        delBtn.onclick = () => { modelsEditData.splice(i, 1); renderModelsModalBody(); };

        const activeBadge = document.createElement('span');
        activeBadge.textContent = '✔ Active';
        activeBadge.style.cssText = 'font-size:0.7rem;color:#4ade80;white-space:nowrap;display:' + (m.value === activeModelId ? 'inline' : 'none') + ';';

        const defaultBtn = document.createElement('button');
        defaultBtn.className = 'btn-secondary';
        defaultBtn.style.cssText = 'font-size:0.7rem;padding:0.1rem 0.4rem;white-space:nowrap;';
        defaultBtn.textContent = '★ Default';
        defaultBtn.title = 'Set as startup default';
        defaultBtn.onclick = async () => {
            await fetch('/api/models/default', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({model: m.value}) });
            activeModelId = m.value;
            renderModelsModalBody();
            renderModelDropdown(m.value);
        };

        row.append(labelInput, valueInput, activeBadge, defaultBtn, delBtn);
        body.appendChild(row);
    });
}

function addModelEntry() {
    modelsEditData.push({label: '', value: ''});
    renderModelsModalBody();
    const body = document.getElementById('models-modal-body');
    if (body) body.scrollTop = body.scrollHeight;
}

async function saveModels() {
    const saveBtn = document.getElementById('models-save-btn');
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = 'Saving…'; }
    try {
        const res = await fetch('/api/models', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({models: modelsEditData, default: activeModelId})
        });
        if (res.ok) {
            modelsData = modelsEditData;
            renderModelDropdown();
            closeModelsModal();
        }
    } catch(e) {
        console.error('Failed to save models', e);
    }
    if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = 'Save'; }
}

async function loadLocations() {
    try {
        const res = await fetch('/api/locations');
        locationsData = await res.json();
        renderLocationDropdown();
    } catch(e) {
        console.error('Failed to load locations', e);
    }
}

function renderLocationDropdown() {
    const sel = document.getElementById('location-override');
    if (!sel) return;
    const current = sel.value;
    sel.innerHTML = '<option value="">Auto</option>';
    for (const group of locationsData) {
        const og = document.createElement('optgroup');
        og.label = '— ' + group.group;
        for (const entry of group.entries) {
            const opt = document.createElement('option');
            opt.value = entry.value;
            opt.textContent = entry.label;
            og.appendChild(opt);
        }
        sel.appendChild(og);
    }
    if (current && [...sel.options].some(o => o.value === current)) sel.value = current;
}

function openLocationsModal() {
    locationsEditData = JSON.parse(JSON.stringify(locationsData));
    renderLocationsModalBody();
    document.getElementById('locations-modal-overlay').style.display = 'flex';
}

function closeLocationsModal() {
    document.getElementById('locations-modal-overlay').style.display = 'none';
    locationsEditData = null;
}

function renderLocationsModalBody() {
    const body = document.getElementById('locations-modal-body');
    body.innerHTML = '';
    locationsEditData.forEach((group, gi) => {
        const groupEl = document.createElement('div');
        groupEl.className = 'loc-group';

        const header = document.createElement('div');
        header.className = 'loc-group-header';

        const nameInput = document.createElement('input');
        nameInput.className = 'loc-group-name';
        nameInput.value = group.group;
        nameInput.oninput = e => { locationsEditData[gi].group = e.target.value; };

        const addBtn = document.createElement('button');
        addBtn.className = 'loc-add-entry-btn';
        addBtn.textContent = '+ Add';
        addBtn.onclick = () => { locationsEditData[gi].entries.push({label: '', value: ''}); renderLocationsModalBody(); };

        const delGroupBtn = document.createElement('button');
        delGroupBtn.className = 'loc-del-group-btn';
        delGroupBtn.textContent = '✕ Group';
        delGroupBtn.onclick = () => { locationsEditData.splice(gi, 1); renderLocationsModalBody(); };

        header.append(nameInput, addBtn, delGroupBtn);
        groupEl.appendChild(header);

        const entries = document.createElement('div');
        entries.className = 'loc-entries';
        group.entries.forEach((entry, ei) => {
            const row = document.createElement('div');
            row.className = 'loc-entry-row';

            const labelInput = document.createElement('input');
            labelInput.className = 'loc-label';
            labelInput.value = entry.label;
            labelInput.placeholder = 'Label';
            labelInput.oninput = e => { locationsEditData[gi].entries[ei].label = e.target.value; };

            const valueInput = document.createElement('input');
            valueInput.className = 'loc-value';
            valueInput.value = entry.value;
            valueInput.placeholder = 'e.g. Kyoto, Japan';
            valueInput.oninput = e => { locationsEditData[gi].entries[ei].value = e.target.value; };

            const delBtn = document.createElement('button');
            delBtn.className = 'loc-del-btn';
            delBtn.textContent = '✕';
            delBtn.onclick = () => { locationsEditData[gi].entries.splice(ei, 1); renderLocationsModalBody(); };

            row.append(labelInput, valueInput, delBtn);
            entries.appendChild(row);
        });
        groupEl.appendChild(entries);
        body.appendChild(groupEl);
    });
}

function addLocationsGroup() {
    locationsEditData.push({group: 'New Group', entries: []});
    renderLocationsModalBody();
    // Scroll to bottom so the new group is visible
    const body = document.getElementById('locations-modal-body');
    if (body) body.scrollTop = body.scrollHeight;
}

async function saveLocations() {
    const saveBtn = document.getElementById('locations-save-btn');
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = 'Saving…'; }
    try {
        const sorted = locationsEditData.map(g => ({
            ...g,
            entries: [...g.entries].sort((a, b) => a.label.toLowerCase().localeCompare(b.label.toLowerCase()))
        }));
        const res = await fetch('/api/locations', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(sorted)
        });
        if (res.ok) {
            locationsData = sorted;
            renderLocationDropdown();
            closeLocationsModal();
        }
    } catch(e) {
        console.error('Failed to save locations', e);
    }
    if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = 'Save'; }
}

// =============================================================================
// Quick Search Chips
// =============================================================================

let quickSearchData = [];
let quickSearchEditData = [];

async function loadQuickSearches() {
    try {
        const r = await fetch('/api/quick-searches');
        quickSearchData = await r.json();
        renderQuickSearchChips();
    } catch {}
}

function renderQuickSearchChips() {
    const container = document.getElementById('quick-search-chips');
    if (!container) return;
    container.innerHTML = '';
    const current = document.getElementById('filter-input')?.value.trim().toLowerCase();
    quickSearchData.forEach(item => {
        const chip = document.createElement('button');
        chip.className = 'quick-search-chip' + (current === item.query.toLowerCase() ? ' active' : '');
        chip.textContent = item.label;
        chip.title = item.query;
        chip.addEventListener('click', () => {
            const input = document.getElementById('filter-input');
            input.value = item.query;
            setFilter(item.query);
            renderQuickSearchChips();
        });
        container.appendChild(chip);
    });
}

function openQuickSearchModal() {
    quickSearchEditData = JSON.parse(JSON.stringify(quickSearchData));
    renderQuickSearchModalBody();
    document.getElementById('quick-search-modal-overlay').style.display = 'flex';
}

function closeQuickSearchModal() {
    document.getElementById('quick-search-modal-overlay').style.display = 'none';
}

function renderQuickSearchModalBody() {
    const body = document.getElementById('quick-search-modal-body');
    body.innerHTML = '';
    quickSearchEditData.forEach((item, i) => {
        const row = document.createElement('div');
        row.className = 'loc-entry-row';

        const labelInput = document.createElement('input');
        labelInput.className = 'loc-label';
        labelInput.value = item.label;
        labelInput.placeholder = 'Abbr';
        labelInput.oninput = e => { quickSearchEditData[i].label = e.target.value; };

        const queryInput = document.createElement('input');
        queryInput.className = 'loc-value';
        queryInput.value = item.query;
        queryInput.placeholder = 'Search query';
        queryInput.oninput = e => { quickSearchEditData[i].query = e.target.value; };

        const delBtn = document.createElement('button');
        delBtn.className = 'loc-del-btn';
        delBtn.textContent = '✕';
        delBtn.onclick = () => { quickSearchEditData.splice(i, 1); renderQuickSearchModalBody(); };

        row.append(labelInput, queryInput, delBtn);
        body.appendChild(row);
    });
}

function addQuickSearchEntry() {
    quickSearchEditData.push({ label: '', query: '' });
    renderQuickSearchModalBody();
    const body = document.getElementById('quick-search-modal-body');
    if (body) body.scrollTop = body.scrollHeight;
}

async function saveQuickSearches() {
    try {
        const r = await fetch('/api/quick-searches', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(quickSearchEditData),
        });
        if (r.ok) {
            quickSearchData = quickSearchEditData;
            renderQuickSearchChips();
            closeQuickSearchModal();
        }
    } catch(e) { console.error('Failed to save quick searches', e); }
}

function addRecentLocation(value) {
    if (!value) return;
    const sel = document.getElementById('location-override');
    const label = sel ? (sel.options[sel.selectedIndex]?.text || value) : value;
    recentLocations = recentLocations.filter(r => r.value !== value);
    recentLocations.unshift({ value, label });
    if (recentLocations.length > 5) recentLocations.pop();
    renderRecentLocations();
}

function renderRecentLocations() {
    const container = document.getElementById('recent-locations');
    if (!container) return;
    container.innerHTML = '';
    if (recentLocations.length === 0) return;
    recentLocations.forEach(({ value, label }) => {
        const pill = document.createElement('button');
        pill.className = 'recent-loc-pill';
        pill.title = value;
        pill.textContent = label.replace(/, .*$/, ''); // show just the city name
        pill.onclick = () => {
            const sel = document.getElementById('location-override');
            if (sel) { sel.value = value; }
        };
        container.appendChild(pill);
    });
    const clear = document.createElement('button');
    clear.className = 'recent-locs-clear';
    clear.title = 'Clear recent locations';
    clear.textContent = '✕';
    clear.onclick = () => { recentLocations = []; renderRecentLocations(); };
    container.appendChild(clear);
}

// Coalesces concurrent callers into one in-flight request instead of firing a
// fresh fetch per call. Queueing several metadata-generation batches in quick
// succession used to open one /api/llm-health request per batch; if the LLM
// endpoint was slow to answer (e.g. busy serving the very generation jobs
// this is checking on), those could pile up and exhaust the browser's ~6
// concurrent-connections-per-host limit, silently stalling every other
// same-origin fetch (thumbnail clicks, metadata reload) until one freed up —
// while SSE job streams, on their own already-open connection, kept ticking.
let _llmHealthCheckPromise = null;

async function ensureLlmHealthy() {
    if (_llmHealthCheckPromise) return _llmHealthCheckPromise;
    _llmHealthCheckPromise = (async () => {
        // Hard client-side timeout — belt-and-suspenders alongside the
        // backend's own 3s timeout on the outbound LLM call, so this can
        // never sit pending forever and hold a connection slot regardless
        // of what's happening server-side.
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 8000);
        let res;
        try {
            res = await fetch('/api/llm-health', { signal: controller.signal });
        } catch (e) {
            if (e.name === 'AbortError') throw new Error('LLM health check timed out (8s) — request never got a response.');
            throw e;
        } finally {
            clearTimeout(timeoutId);
        }
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) {
            const message = data.message || `LLM health check failed (${res.status})`;
            const base = data.base_url ? `\n${data.base_url}` : '';
            throw new Error(`${message}${base}`);
        }
        return data;
    })();
    try {
        return await _llmHealthCheckPromise;
    } finally {
        _llmHealthCheckPromise = null;
    }
}

async function generateMetadataWithAI() {
    if (selectedFiles.length === 0) return;
    const contextHint = getMetadataContextHint();
    const btn = document.getElementById('gen-meta-btn');
    btn.disabled = true;
    btn.classList.add('generating');
    const count = selectedFiles.length;
    setStatus('Checking LLM server...', true);

    try {
        await ensureLlmHealthy();
        setStatus(`Starting AI metadata job for ${count} file${count !== 1 ? 's' : ''}...`, true);
        await startServerJob('/api/jobs/metadata-generate', {
            paths: [...selectedFiles],
            location_override: getLocationOverride(),
            context_hint: contextHint,
        });
        setStatus('Metadata job started', false);
        // Leave the button disabled/pulsing — updateGeneratingIndicators()
        // takes over from here and clears it once the job actually
        // finishes, instead of this only covering the moment the job was
        // queued (which used to make the button look done almost
        // instantly even though generation was still running).
    } catch (err) {
        setStatus(`AI generation error: ${err.message}`, false);
        alert('AI generation error: ' + err.message);
        btn.disabled = false;
        btn.classList.remove('generating');
    }
}

function getMetadataContextHint() {
    const input = document.getElementById('context-hint-input');
    return input ? input.value.trim() : '';
}

function enqueueForGeneration(paths, contextHint = '') {
    if (contextHint === '') contextHint = getMetadataContextHint();
    setStatus('Checking LLM server...', true);
    ensureLlmHealthy()
        .then(() => startServerJob('/api/jobs/metadata-generate', {
            paths,
            location_override: getLocationOverride(),
            context_hint: contextHint,
        }))
        .catch(err => setStatus(`AI generation error: ${err.message}`, false));
}

async function batchGenerateMetadata() {
    const paths = batchQueue.map(item => item.path);
    batchQueue = [];
    if (paths.length) enqueueForGeneration(paths);
}

async function copyMetadataFromTo(sourcePath, targetPath) {
    if (sourcePath === targetPath) return;
    const srcName = sourcePath.split('/').pop();
    const tgtName = targetPath.split('/').pop();
    const confirmed = confirm(`Copy metadata from:\n"${srcName}"\nto:\n"${tgtName}"?\n\nThis will overwrite the target's title, description, and keywords.`);
    if (!confirmed) return;

    // Fetch source metadata
    const params = new URLSearchParams();
    params.append('paths', sourcePath);
    const r = await fetch(`/api/metadata?${params}`);
    const data = await r.json();

    // Apply to target
    await fetch('/api/metadata', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            paths: [targetPath],
            title: data.title || null,
            description: data.description || null,
            keywords: data.keywords || null,
        }),
    });

    // Mark target dirty in local state + thumbnail
    const file = allFiles.find(f => f.path === targetPath);
    dirtyPathSet.add(targetPath);
    if (file) file.is_dirty = true;
    const thumbEl = document.querySelector(`.thumbnail-item[data-path="${CSS.escape(targetPath)}"]`);
    if (thumbEl) thumbEl.classList.add('dirty');
    loadDirtyCount();
    setStatus(`Metadata copied from ${srcName} to ${tgtName}`, false);
    setTimeout(() => clearStatus(), 3000);
}

async function confirmAndMoveFiles(paths, targetFolder) {
    const targetName = targetFolder.split('/').pop();
    const confirmed = confirm(`Move ${paths.length} file(s) to "${targetName}"?\n\nThis cannot be undone.`);
    if (!confirmed) return;

    setStatus(`Moving ${paths.length} file(s) to ${targetName}…`, true);
    try {
        const r = await fetch('/api/move-files', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ paths, target_folder: targetFolder }),
        });
        const result = await r.json();

        if (result.moved && result.moved.length > 0) {
            const movedPaths = new Set(result.moved.map(m => m.old_path));
            allFiles = allFiles.filter(f => !movedPaths.has(f.path));
            visibleFiles = visibleFiles.filter(f => !movedPaths.has(f.path));
            movedPaths.forEach(p => {
                document.querySelector(`.thumbnail-item[data-path="${CSS.escape(p)}"]`)?.remove();
            });
            selectedFiles = selectedFiles.filter(p => !movedPaths.has(p));
            fileCountEl.textContent = `${allFiles.length} files`;
            metadataForm.style.display = 'none';
            metadataContent.style.display = 'flex';
            loadDirtyCount();
            setStatus(`Moved ${result.moved.length} file(s) to ${targetName}`, false);
            setTimeout(() => clearStatus(), 3000);
        }
        if (result.failed && result.failed.length > 0) {
            setStatus(`⚠ ${result.failed.length} file(s) failed to move`, false);
        }
    } catch (e) {
        setStatus(`Move error: ${e.message}`, false);
    }
}

async function deleteSelectedFiles() {
    const paths = [...selectedFiles];
    setStatus(`Deleting ${paths.length} file(s)...`, true);

    try {
        const response = await fetch('/api/delete-files', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ paths })
        });
        const data = await response.json();

        // Remove from allFiles, visibleFiles, selectedFiles and DOM
        data.deleted.forEach(path => {
            allFiles = allFiles.filter(f => f.path !== path);
            visibleFiles = visibleFiles.filter(f => f.path !== path);
            const thumbEl = document.querySelector(`.thumbnail-item[data-path="${CSS.escape(path)}"]`);
            if (thumbEl) thumbEl.remove();
        });
        selectedFiles = [];
        lastSelectedIndex = -1;
        fileCountEl.textContent = `${visibleFiles.length}/${allFiles.length} files`;

        // Clear metadata panel
        metadataForm.style.display = 'none';
        metadataContent.style.display = 'flex';
        metadataContent.innerHTML = '<div class="no-selection"><p>Select files to view and edit metadata</p></div>';

        const msg = data.failed.length > 0
            ? `Deleted ${data.deleted.length}, failed: ${data.failed.length}`
            : `Deleted ${data.deleted.length} file(s)`;
        setStatus(msg, false);
    } catch (err) {
        setStatus(`Delete error: ${err.message}`, false);
    }
}


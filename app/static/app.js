// YT Uploader Modern Client Engine

(function () {
  'use strict';

  // 1. Form submit confirmation handler
  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (form.matches('[data-confirm]') && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
    }
  });

  // 2. Resilient Copy-to-Clipboard (Works on HTTPS, localhost, and HTTP VPS IPs)
  function copyTextToClipboard(text, triggerEl) {
    function showFeedback() {
      if (!triggerEl) return;
      triggerEl.classList.add('copied');
      var originalTitle = triggerEl.getAttribute('data-original-title') || triggerEl.getAttribute('title') || '';
      triggerEl.setAttribute('data-original-title', originalTitle);
      triggerEl.setAttribute('title', 'Copied to clipboard!');

      // Create floating badge if not present
      var badge = triggerEl.querySelector('.copy-tooltip');
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'copy-tooltip';
        badge.textContent = 'Copied!';
        triggerEl.appendChild(badge);
      }
      badge.classList.add('show');

      setTimeout(function () {
        triggerEl.classList.remove('copied');
        if (originalTitle) triggerEl.setAttribute('title', originalTitle);
        badge.classList.remove('show');
      }, 1600);
    }

    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(showFeedback).catch(function () {
        fallbackCopy(text, showFeedback);
      });
    } else {
      fallbackCopy(text, showFeedback);
    }
  }

  function fallbackCopy(text, callback) {
    try {
      var textArea = document.createElement('textarea');
      textArea.value = text;
      textArea.style.position = 'fixed';
      textArea.style.top = '-9999px';
      textArea.style.left = '-9999px';
      textArea.setAttribute('readonly', '');
      document.body.appendChild(textArea);
      textArea.select();
      var successful = document.execCommand('copy');
      document.body.removeChild(textArea);
      if (successful && callback) callback();
    } catch (err) {
      console.warn('Copy fallback failed:', err);
    }
  }

  document.addEventListener('click', function (event) {
    var copyBtn = event.target.closest('[data-copy]');
    if (copyBtn) {
      event.preventDefault();
      event.stopPropagation();
      var text = copyBtn.getAttribute('data-copy');
      if (text) copyTextToClipboard(text, copyBtn);
    }
  });

  // 3. Queue Real-Time Search & Tab Filtering
  function initQueueFiltering() {
    var searchInput = document.getElementById('queueSearchInput');
    var clearBtn = document.getElementById('clearSearchBtn');
    var filterTabs = document.querySelectorAll('#queueFilterTabs .tab-btn');
    var jobRows = document.querySelectorAll('.job-row');
    var showingCount = document.getElementById('showingCount');
    var noResultsMsg = document.getElementById('noResultsMsg');
    var tableWrap = document.getElementById('queueTableWrap');

    if (!jobRows.length || !filterTabs.length) return;

    var activeFilter = 'all';

    // Update tab count badges from live DOM rows
    function updateTabCounts() {
      var counts = {
        all: jobRows.length,
        queued: 0,
        active: 0,
        completed: 0,
        needs_review: 0,
        failed: 0
      };

      jobRows.forEach(function (row) {
        var status = row.getAttribute('data-status') || '';
        var approved = row.getAttribute('data-approved') === 'true';

        if (status === 'queued' && !approved) {
          counts.queued++;
        } else if (['downloading', 'uploading'].indexOf(status) !== -1) {
          counts.active++;
        } else if (status === 'completed') {
          counts.completed++;
        } else if (status === 'needs_review') {
          counts.needs_review++;
        } else if (['failed', 'retry', 'skipped'].indexOf(status) !== -1) {
          counts.failed++;
        }
      });

      var badgeAll = document.getElementById('badgeAll');
      var badgeQueued = document.getElementById('badgeQueued');
      var badgeActive = document.getElementById('badgeActive');
      var badgeCompleted = document.getElementById('badgeCompleted');
      var badgeNeedsReview = document.getElementById('badgeNeedsReview');
      var badgeFailed = document.getElementById('badgeFailed');

      if (badgeAll) badgeAll.textContent = counts.all;
      if (badgeQueued) badgeQueued.textContent = counts.queued;
      if (badgeActive) badgeActive.textContent = counts.active;
      if (badgeCompleted) badgeCompleted.textContent = counts.completed;
      if (badgeNeedsReview) badgeNeedsReview.textContent = counts.needs_review;
      if (badgeFailed) badgeFailed.textContent = counts.failed;
    }

    function rowMatchesFilter(row, filter) {
      if (filter === 'all') return true;
      var status = row.getAttribute('data-status') || '';
      var approved = row.getAttribute('data-approved') === 'true';

      if (filter === 'queued') {
        return status === 'queued' && !approved;
      }
      if (filter === 'active') {
        return ['downloading', 'uploading'].indexOf(status) !== -1;
      }
      if (filter === 'completed') {
        return status === 'completed';
      }
      if (filter === 'needs_review') {
        return status === 'needs_review';
      }
      if (filter === 'failed') {
        return ['failed', 'retry', 'skipped'].indexOf(status) !== -1;
      }
      return true;
    }

    function rowMatchesSearch(row, query) {
      if (!query) return true;
      var title = (row.getAttribute('data-title') || '').toLowerCase();
      var videoId = (row.getAttribute('data-videoid') || '').toLowerCase();
      var ytid = (row.getAttribute('data-ytid') || '').toLowerCase();
      var channel = (row.getAttribute('data-channel') || '').toLowerCase();

      return title.indexOf(query) !== -1 ||
             videoId.indexOf(query) !== -1 ||
             ytid.indexOf(query) !== -1 ||
             channel.indexOf(query) !== -1;
    }

    function applyFilterAndSearch() {
      var query = searchInput ? searchInput.value.trim().toLowerCase() : '';
      if (clearBtn) {
        clearBtn.style.display = query ? 'flex' : 'none';
      }

      var visible = 0;
      jobRows.forEach(function (row) {
        var matches = rowMatchesFilter(row, activeFilter) && rowMatchesSearch(row, query);
        row.style.display = matches ? '' : 'none';
        if (matches) visible++;
      });

      if (showingCount) {
        showingCount.textContent = visible;
      }

      if (noResultsMsg) {
        noResultsMsg.style.display = (visible === 0) ? 'block' : 'none';
      }
      if (tableWrap) {
        tableWrap.style.display = (visible === 0) ? 'none' : 'block';
      }
    }

    filterTabs.forEach(function (tab) {
      tab.addEventListener('click', function () {
        filterTabs.forEach(function (t) { t.classList.remove('active'); });
        tab.classList.add('active');
        activeFilter = tab.getAttribute('data-filter') || 'all';
        applyFilterAndSearch();
      });
    });

    if (searchInput) {
      searchInput.addEventListener('input', applyFilterAndSearch);
      searchInput.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
          searchInput.value = '';
          applyFilterAndSearch();
          searchInput.blur();
        }
      });
    }

    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        if (searchInput) {
          searchInput.value = '';
          searchInput.focus();
        }
        applyFilterAndSearch();
      });
    }

    // Keyboard shortcut: '/' to focus search
    document.addEventListener('keydown', function (e) {
      if (e.key === '/' && searchInput && document.activeElement !== searchInput) {
        var tagName = document.activeElement ? document.activeElement.tagName : '';
        if (tagName !== 'INPUT' && tagName !== 'TEXTAREA') {
          e.preventDefault();
          searchInput.focus();
        }
      }
    });

    updateTabCounts();
    applyFilterAndSearch();
  }

  // 4. Live Auto-Refresh Engine with Form-Interaction Protection
  function initAutoRefresh() {
    var select = document.getElementById('autoRefreshSelect');
    var timerEl = document.getElementById('refreshTimer');
    var dotEl = document.getElementById('refreshDot');

    if (!select) return;

    var storageKey = 'yt_uploader_refresh_seconds';
    var savedInterval = parseInt(localStorage.getItem(storageKey) || '0', 10);
    if (!isNaN(savedInterval)) {
      select.value = savedInterval.toString();
    }

    var countdown = parseInt(select.value, 10);
    var timerId = null;

    function renderTimer() {
      if (!timerEl) return;
      var val = parseInt(select.value, 10);
      if (val === 0) {
        timerEl.textContent = '';
        if (dotEl) dotEl.className = 'refresh-indicator';
      } else {
        timerEl.textContent = countdown + 's';
        if (dotEl) dotEl.className = 'refresh-indicator active';
      }
    }

    function isUserBusy() {
      var el = document.activeElement;
      if (!el) return false;
      var tag = el.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
        return true;
      }
      var openDetails = document.querySelector('details[open]');
      if (openDetails) return true;
      return false;
    }

    function tick() {
      var val = parseInt(select.value, 10);
      if (val <= 0) return;

      countdown--;
      if (countdown <= 0) {
        if (isUserBusy()) {
          // Postpone reload by 6s so typing is not interrupted
          countdown = 6;
          if (timerEl) timerEl.textContent = 'waiting...';
          return;
        }
        window.location.reload();
        return;
      }
      renderTimer();
    }

    function startTimer() {
      if (timerId) clearInterval(timerId);
      var val = parseInt(select.value, 10);
      countdown = val;
      renderTimer();
      if (val > 0) {
        timerId = setInterval(tick, 1000);
      }
    }

    select.addEventListener('change', function () {
      localStorage.setItem(storageKey, select.value);
      startTimer();
    });

    startTimer();
  }

  // 5. Event Log Severity Filter
  function initLogsFiltering() {
    var tabContainer = document.getElementById('logFilterTabs');
    if (!tabContainer) return;

    var tabs = tabContainer.querySelectorAll('.tab-btn');
    var events = document.querySelectorAll('.event-item');
    var emptyMsg = document.getElementById('logsFilterEmpty');

    if (!tabs.length || !events.length) return;

    tabs.forEach(function (tab) {
      tab.addEventListener('click', function () {
        tabs.forEach(function (t) { t.classList.remove('active'); });
        tab.classList.add('active');
        var severity = tab.getAttribute('data-severity') || 'all';

        var visible = 0;
        events.forEach(function (ev) {
          var evSev = ev.getAttribute('data-severity') || '';
          var match = (severity === 'all') || (evSev === severity);
          ev.style.display = match ? 'flex' : 'none';
          if (match) visible++;
        });

        if (emptyMsg) {
          emptyMsg.style.display = (visible === 0) ? 'block' : 'none';
        }
      });
    });
  }

  // Initialize all interactive modules on DOMContentLoaded
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      initQueueFiltering();
      initAutoRefresh();
      initLogsFiltering();
    });
  } else {
    initQueueFiltering();
    initAutoRefresh();
    initLogsFiltering();
  }
})();

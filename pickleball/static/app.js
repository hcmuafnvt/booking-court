(function () {
	'use strict';

	const contentEl = document.getElementById('page-content');
	const spinnerEl = document.getElementById('spinner');
	const stat1ValueEl = document.getElementById('stat1-value');
	const stat2ValueEl = document.getElementById('stat2-value');
	const stat1LabelEl = document.getElementById('stat1-label');
	const stat2LabelEl = document.getElementById('stat2-label');
	const stat1IconEl = document.getElementById('stat1-icon');
	const stat2IconEl = document.getElementById('stat2-icon');

	let currentPage = null;

	// ── Spinner ──────────────────────────────────────────────
	function showSpinner() {
		contentEl.innerHTML = '';
		spinnerEl.style.display = 'flex';
	}

	function hideSpinner() {
		spinnerEl.style.display = 'none';
	}

	// ── Stat cards ───────────────────────────────────────────
	function updateStats(htmlString) {
		const tmp = document.createElement('div');
		tmp.innerHTML = htmlString;
		const data = tmp.querySelector('.page-data');
		if (!data) return;

		stat1ValueEl.textContent = data.dataset.stat1;
		stat2ValueEl.textContent = data.dataset.stat2;
		stat1LabelEl.textContent = data.dataset.stat1Label;
		stat2LabelEl.textContent = data.dataset.stat2Label;

		if (stat1IconEl) {
			stat1IconEl.className = 'ph ' + data.dataset.stat1Icon;
		}
		if (stat2IconEl) {
			stat2IconEl.className = 'ph ' + data.dataset.stat2Icon;
		}
	}

	// ── Active nav state ─────────────────────────────────────
	function setActive(page) {
		document.querySelectorAll('[data-page]').forEach(function (el) {
			el.classList.toggle('active', el.dataset.page === page);
		});
	}

	// ── Load page via AJAX ───────────────────────────────────
	function loadPage(page, pushHistory) {
		if (page === currentPage) return;
		currentPage = page;

		if (pushHistory !== false) {
			history.pushState({ page: page }, '', '/' + page);
		}

		showSpinner();
		setActive(page);

		fetch('/api/' + page)
			.then(function (res) {
				if (!res.ok) throw new Error('HTTP ' + res.status);
				return res.text();
			})
			.then(function (html) {
				hideSpinner();
				updateStats(html);
				contentEl.innerHTML = html;
			})
			.catch(function (err) {
				hideSpinner();
				contentEl.innerHTML =
					'<div style="padding:32px;text-align:center;color:#ff7a59">' +
					'<i class="ph ph-warning" style="font-size:32px"></i>' +
					'<p style="margin-top:8px">Failed to load data. Please try again.</p>' +
					'</div>';
				console.error(err);
			});
	}

	// ── Refresh current page ─────────────────────────────────
	function refreshPage() {
		var page = currentPage || 'scheduled';
		var btn = document.getElementById('refresh-btn');
		if (btn) btn.classList.add('spinning');

		fetch('/api/' + page)
			.then(function (res) {
				if (!res.ok) throw new Error('HTTP ' + res.status);
				return res.text();
			})
			.then(function (html) {
				updateStats(html);
				contentEl.innerHTML = html;
				if (btn) btn.classList.remove('spinning');
			})
			.catch(function (err) {
				if (btn) btn.classList.remove('spinning');
				console.error(err);
			});
	}

	var refreshBtn = document.getElementById('refresh-btn');
	if (refreshBtn) refreshBtn.addEventListener('click', refreshPage);

	// ── Edit + Delete handler (event delegation on page-content) ───
	contentEl.addEventListener('click', function (e) {
		// Edit
		var editBtn = e.target.closest('.icon-btn.edit');
		if (editBtn) {
			window.location.href = '/edit/' + editBtn.dataset.id;
			return;
		}

		// Delete
		var btn = e.target.closest('.icon-btn.delete');
		if (!btn) return;

		var id = btn.dataset.id;
		var type = btn.dataset.type;
		var who = btn.dataset.who || 'this booking';

		if (!confirm('Delete booking for "' + who + '"?')) return;

		fetch('/api/delete', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ id: id, type: type })
		})
			.then(function (res) { return res.json(); })
			.then(function (data) {
				if (data.ok) {
					currentPage = null;
					loadPage('scheduled', false);
				} else {
					alert('Error: ' + (data.error || 'Unknown error'));
				}
			})
			.catch(function (err) {
				alert('Request failed. Please try again.');
				console.error(err);
			});
	});

	// ── Nav click handlers ───────────────────────────────────
	document.querySelectorAll('[data-page]').forEach(function (el) {
		el.addEventListener('click', function (e) {
			e.preventDefault();
			loadPage(el.dataset.page);
		});
	});

	// ── Back/forward navigation ──────────────────────────────
	window.addEventListener('popstate', function (e) {
		var page = (e.state && e.state.page) ? e.state.page : 'scheduled';
		currentPage = null; // force reload
		loadPage(page, false);
	});

	// ── Initial load (read URL) ───────────────────────────────
	var path = location.pathname.replace(/^\//, '') || 'scheduled';
	var initialPage = (path === 'booked') ? 'booked' : 'scheduled';
	loadPage(initialPage, false);
	history.replaceState({ page: initialPage }, '', '/' + initialPage);
})();

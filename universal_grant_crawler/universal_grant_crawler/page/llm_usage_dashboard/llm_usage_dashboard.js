frappe.pages['llm-usage-dashboard'].on_page_load = function(wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: 'LLM Usage Dashboard',
		single_column: true
	});

	page.main.html(`
		<div id="llm-dashboard-root">
			<div class="llm-dash-loading">
				<div class="spinner"></div>
				<p>Loading usage data...</p>
			</div>
		</div>
	`);

	// Add refresh button
	page.set_primary_action(__('Refresh'), () => {
		loadDashboard(page);
	}, 'refresh');

	// Add period selector
	page.add_field({
		fieldname: 'period',
		label: __('Period'),
		fieldtype: 'Select',
		options: 'Today\nLast 7 Days\nLast 30 Days\nAll Time',
		default: 'Today',
		change: () => loadDashboard(page)
	});

	loadDashboard(page);

	// Auto-refresh every 30 seconds
	page._refresh_interval = setInterval(() => {
		loadDashboard(page, true);
	}, 30000);

	// Cleanup on page destroy
	page.wrapper.on('remove', () => {
		if (page._refresh_interval) clearInterval(page._refresh_interval);
	});
};


function loadDashboard(page, silent) {
	const period = page.fields_dict.period?.get_value() || 'Today';

	if (!silent) {
		$('#llm-dashboard-root').html(`
			<div class="llm-dash-loading">
				<div class="spinner"></div>
				<p>Loading usage data...</p>
			</div>
		`);
	}

	frappe.call({
		method: 'universal_grant_crawler.universal_grant_crawler.api.get_llm_usage_dashboard',
		args: { period: period },
		callback: function(r) {
			if (r.message) {
				renderDashboard(r.message, period);
			}
		},
		error: function() {
			$('#llm-dashboard-root').html(`
				<div class="llm-dash-error">
					<i class="fa fa-exclamation-triangle"></i>
					<p>Failed to load usage data. Please try again.</p>
				</div>
			`);
		}
	});
}


function renderDashboard(data, period) {
	const root = $('#llm-dashboard-root');
	const providers = data.providers || [];
	const recent_logs = data.recent_logs || [];
	const totals = data.totals || {};
	const daily_breakdown = data.daily_breakdown || [];

	let html = `
		<!-- ═══ SUMMARY CARDS ═══ -->
		<div class="llm-summary-row">
			<div class="llm-card llm-card-total">
				<div class="llm-card-icon">
					<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
						<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline>
					</svg>
				</div>
				<div class="llm-card-body">
					<div class="llm-card-value">${totals.total_requests || 0}</div>
					<div class="llm-card-label">Total Requests</div>
				</div>
			</div>
			<div class="llm-card llm-card-success">
				<div class="llm-card-icon">
					<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
						<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path>
						<polyline points="22 4 12 14.01 9 11.01"></polyline>
					</svg>
				</div>
				<div class="llm-card-body">
					<div class="llm-card-value">${totals.successful || 0}</div>
					<div class="llm-card-label">Successful</div>
				</div>
			</div>
			<div class="llm-card llm-card-failed">
				<div class="llm-card-icon">
					<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
						<circle cx="12" cy="12" r="10"></circle>
						<line x1="15" y1="9" x2="9" y2="15"></line>
						<line x1="9" y1="9" x2="15" y2="15"></line>
					</svg>
				</div>
				<div class="llm-card-body">
					<div class="llm-card-value">${totals.failed || 0}</div>
					<div class="llm-card-label">Failed</div>
				</div>
			</div>
			<div class="llm-card llm-card-grants">
				<div class="llm-card-icon">
					<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
						<rect x="2" y="7" width="20" height="14" rx="2" ry="2"></rect>
						<path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"></path>
					</svg>
				</div>
				<div class="llm-card-body">
					<div class="llm-card-value">${totals.grants_extracted || 0}</div>
					<div class="llm-card-label">Grants Extracted</div>
				</div>
			</div>
			<div class="llm-card llm-card-rate">
				<div class="llm-card-icon">
					<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
						<path d="M12 20V10"></path>
						<path d="M18 20V4"></path>
						<path d="M6 20v-4"></path>
					</svg>
				</div>
				<div class="llm-card-body">
					<div class="llm-card-value">${totals.success_rate || '0%'}</div>
					<div class="llm-card-label">Success Rate</div>
				</div>
			</div>
		</div>

		<!-- ═══ PROVIDER USAGE CARDS ═══ -->
		<div class="llm-section-header">
			<h3>Provider Usage</h3>
			<span class="llm-period-badge">${period}</span>
		</div>
		<div class="llm-providers-grid">
			${providers.length === 0
				? '<div class="llm-empty-state"><p>No LLM providers configured yet.<br>Go to <strong>Universal Crawler Settings</strong> to add providers.</p></div>'
				: providers.map(p => renderProviderCard(p)).join('')
			}
		</div>
	`;

	// Daily breakdown chart (only for multi-day periods)
	if (daily_breakdown.length > 1) {
		html += `
			<div class="llm-section-header">
				<h3>Daily Breakdown</h3>
			</div>
			<div class="llm-chart-container">
				${renderBarChart(daily_breakdown)}
			</div>
		`;
	}

	// Recent usage logs
	html += `
		<div class="llm-section-header">
			<h3>Recent Activity</h3>
			<a href="/app/llm-usage-log" class="llm-view-all">View All →</a>
		</div>
		<div class="llm-table-container">
			${recent_logs.length === 0
				? '<div class="llm-empty-state"><p>No usage logs yet. Run a crawl to see activity here.</p></div>'
				: renderRecentTable(recent_logs)
			}
		</div>
	`;

	root.html(html);
}


function renderProviderCard(p) {
	const used = p.used || 0;
	const limit = p.daily_limit || 100000;
	const remaining = Math.max(0, limit - used);
	const pct = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;

	let barClass = 'llm-bar-good';
	if (pct > 80) barClass = 'llm-bar-danger';
	else if (pct > 50) barClass = 'llm-bar-warning';

	const statusDot = p.active ? 'llm-dot-active' : 'llm-dot-inactive';
	const successRate = p.total_requests > 0
		? Math.round((p.successful / p.total_requests) * 100) + '%'
		: '—';

	return `
		<div class="llm-provider-card">
			<div class="llm-provider-header">
				<div class="llm-provider-name">
					<span class="llm-status-dot ${statusDot}"></span>
					${p.provider_name}
				</div>
				<div class="llm-provider-model">${p.model_name || ''}</div>
			</div>

			<div class="llm-usage-bar-wrap">
				<div class="llm-usage-bar">
					<div class="llm-usage-fill ${barClass}" style="width: ${pct}%"></div>
				</div>
				<div class="llm-usage-numbers">
					<span><strong>${used.toLocaleString()}</strong> used</span>
					<span>${remaining.toLocaleString()} remaining</span>
				</div>
			</div>

			<div class="llm-provider-stats">
				<div class="llm-stat">
					<span class="llm-stat-val">${(p.total_requests || 0).toLocaleString()}</span>
					<span class="llm-stat-lbl">Requests</span>
				</div>
				<div class="llm-stat">
					<span class="llm-stat-val">${(p.grants_extracted || 0).toLocaleString()}</span>
					<span class="llm-stat-lbl">Grants</span>
				</div>
				<div class="llm-stat">
					<span class="llm-stat-val">${successRate}</span>
					<span class="llm-stat-lbl">Success</span>
				</div>
				<div class="llm-stat">
					<span class="llm-stat-val">${(p.failed || 0).toLocaleString()}</span>
					<span class="llm-stat-lbl">Failed</span>
				</div>
			</div>
		</div>
	`;
}


function renderBarChart(dailyData) {
	if (!dailyData.length) return '';
	const maxVal = Math.max(...dailyData.map(d => d.total), 1);

	const bars = dailyData.map(d => {
		const h = Math.max(4, (d.total / maxVal) * 160);
		const successH = d.total > 0 ? (d.successful / d.total) * h : 0;
		const failH = h - successH;
		const label = d.date.slice(5); // "MM-DD"
		return `
			<div class="llm-bar-col">
				<div class="llm-bar-stack" style="height:${h}px" title="${d.date}: ${d.total} requests (${d.successful} ok, ${d.failed} failed)">
					<div class="llm-bar-fail" style="height:${failH}px"></div>
					<div class="llm-bar-ok" style="height:${successH}px"></div>
				</div>
				<div class="llm-bar-label">${label}</div>
				<div class="llm-bar-count">${d.total}</div>
			</div>
		`;
	}).join('');

	return `<div class="llm-bar-chart">${bars}</div>`;
}


function renderRecentTable(logs) {
	const rows = logs.map(l => {
		const statusClass = {
			'Success': 'llm-badge-success',
			'Failed': 'llm-badge-failed',
			'Content Too Large': 'llm-badge-warning',
			'Rate Limited': 'llm-badge-warning'
		}[l.status] || 'llm-badge-failed';

		const time = frappe.datetime.prettyDate(l.creation);
		const url = l.page_url ? l.page_url.substring(0, 60) + (l.page_url.length > 60 ? '…' : '') : '—';

		return `
			<tr>
				<td>${time}</td>
				<td><strong>${l.provider_name}</strong></td>
				<td>${l.model_name || '—'}</td>
				<td><span class="llm-badge ${statusClass}">${l.status}</span></td>
				<td>${(l.grants_extracted || 0).toLocaleString()}</td>
				<td>${(l.content_chars_sent || 0).toLocaleString()}</td>
				<td class="llm-url-cell" title="${l.page_url || ''}">${url}</td>
			</tr>
		`;
	}).join('');

	return `
		<table class="llm-table">
			<thead>
				<tr>
					<th>Time</th>
					<th>Provider</th>
					<th>Model</th>
					<th>Status</th>
					<th>Grants</th>
					<th>Chars</th>
					<th>Page URL</th>
				</tr>
			</thead>
			<tbody>${rows}</tbody>
		</table>
	`;
}

/* profile.js — 用户画像工作台（替换 mindmap 主视图）*/

let profileItems = [];
let profileSummary = {};
let profileFacetFilter = '';
let profileStatusFilter = 'active';

const FACET_TYPES = ['', 'preference', 'fact', 'style', 'restriction', 'task_pattern'];
const FACET_LABELS = { '': '全部', preference: '偏好', fact: '事实', style: '风格', restriction: '约束', task_pattern: '任务' };
const STATUS_LABELS = { active: '活跃', superseded: '已取代', contradicted: '矛盾', archived: '已归档' };

async function loadProfileSummary(userId) {
  const data = await api('/profile/summary?user=' + encodeURIComponent(userId));
  if (!data) return;
  profileSummary = data;
  renderProfileSummary();
  renderProfileFacetTabs();
}

function renderProfileSummary() {
  const el = document.getElementById('profileSummary');
  if (!el) return;
  const up = profileSummary.user_profile;
  if (!up) {
    el.innerHTML = '<div class="empty-state"><div class="icon">👤</div><div>该用户暂无画像数据</div></div>';
    return;
  }
  const counts = profileSummary.facet_counts || {};
  const badge = (t) => counts[t] ? `<span class="stat-chip">${FACET_LABELS[t] || t}: ${counts[t]}</span>` : '';
  el.innerHTML =
    `<div class="profile-card">
      <div class="profile-card-header">
        <span class="profile-name">${escHtml(up.display_name || up.canonical_user_id)}</span>
        <span class="profile-version">v${up.profile_version}</span>
      </div>
      ${up.summary_text ? `<div class="profile-summary-text">${escHtml(up.summary_text)}</div>` : ''}
      <div class="profile-chips">${badge('preference')}${badge('fact')}${badge('style')}${badge('restriction')}${badge('task_pattern')}</div>
    </div>`;
}

function renderProfileFacetTabs() {
  const el = document.getElementById('profileFacetTabs');
  if (!el) return;
  el.innerHTML = FACET_TYPES.map(ft =>
    `<button class="${profileFacetFilter === ft ? 'active' : ''}" onclick="filterProfileItems('${ft}', profileStatusFilter)">${FACET_LABELS[ft] || ft}${ft === '' ? ' (' + (profileSummary.total_items || 0) + ')' : ''}</button>`
  ).join('');
}

async function filterProfileItems(facetType, status) {
  profileFacetFilter = facetType;
  profileStatusFilter = status;
  renderProfileFacetTabs();
  await loadProfileItems(currentUser);
}

async function loadProfileItems(userId) {
  let url = '/profile/items?user=' + encodeURIComponent(userId) + '&status=' + profileStatusFilter;
  if (profileFacetFilter) url += '&facet_type=' + profileFacetFilter;
  const data = await api(url);
  profileItems = data?.items || [];
  renderProfileItemTable();
}

function renderProfileItemTable() {
  const tbody = document.getElementById('profileItemBody');
  if (!tbody) return;
  if (!profileItems.length) {
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text2);padding:40px">暂无画像条目</td></tr>';
    return;
  }
  tbody.innerHTML = profileItems.map(item => {
    const statusCls = item.status === 'active' ? 'status-active' : item.status === 'contradicted' ? 'status-contra' : 'status-other';
    const statusText = STATUS_LABELS[item.status] || item.status;
    return `<tr>
      <td>${item.id}</td>
      <td><span class="type-badge type-${item.facet_type === 'task_pattern' ? 'task' : item.facet_type}">${FACET_LABELS[item.facet_type] || item.facet_type}</span></td>
      <td>
        <div class="item-title">${escHtml(item.title || '')}</div>
        <div class="item-content">${escHtml(item.content)}</div>
      </td>
      <td><span class="${statusCls}">${statusText}</span></td>
      <td>${scoreBar(item.confidence, 'var(--accent)')}<span class="bar-num">${(item.confidence*100).toFixed(0)}%</span></td>
      <td>${scoreBar(item.importance, 'var(--brand)')}<span class="bar-num">${(item.importance*100).toFixed(0)}%</span></td>
      <td style="font-size:11px;color:var(--text2)">${item.evidence_count} 条</td>
      <td style="white-space:nowrap">
        <input type="checkbox" class="profile-merge-chk" value="${item.id}" title="选择合并">
        <button class="btn btn-sm" onclick="openProfileEditModal(${item.id})" title="编辑">✏️</button>
        <button class="btn btn-sm" onclick="openEvidenceModal(${item.id})" title="证据">🔗</button>
        <button class="btn btn-sm btn-danger" onclick="archiveProfileItem(${item.id})" title="归档">📦</button>
      </td>
    </tr>`;
  }).join('');
}

function scoreBar(val, color) {
  const pct = Math.min(100, Math.max(0, Math.round(val * 100)));
  return `<span class="score-bar"><span class="score-bar-fill" style="width:${pct}%;background:${color}"></span></span>`;
}

// ── Edit Profile Item Modal ─────────────────────────────────────────────────

function openProfileEditModal(itemId) {
  const item = profileItems.find(i => i.id === itemId);
  if (!item) return;
  document.getElementById('profileEditId').value = item.id;
  document.getElementById('profileEditTitle').value = item.title || '';
  document.getElementById('profileEditContent').value = item.content;
  document.getElementById('profileEditFacet').value = item.facet_type;
  document.getElementById('profileEditStatus').value = item.status;
  document.getElementById('profileEditConfidence').value = item.confidence;
  document.getElementById('profileEditImportance').value = item.importance;
  document.getElementById('profileEditModal').classList.add('show');
}

async function saveProfileItem() {
  const id = parseInt(document.getElementById('profileEditId').value);
  const data = {
    id, user: currentUser,
    title: document.getElementById('profileEditTitle').value.trim(),
    content: document.getElementById('profileEditContent').value.trim(),
    facet_type: document.getElementById('profileEditFacet').value,
    status: document.getElementById('profileEditStatus').value,
    confidence: parseFloat(document.getElementById('profileEditConfidence').value) || 0.5,
    importance: parseFloat(document.getElementById('profileEditImportance').value) || 0.5,
  };
  const res = await api('/profile/item/update', { method: 'POST', body: JSON.stringify(data) });
  if (res?.ok) {
    closeModal('profileEditModal');
    await loadProfileItems(currentUser);
    await loadProfileSummary(currentUser);
    toast('画像条目已更新', 'success');
  }
}

// ── Archive ─────────────────────────────────────────────────────────────────

async function archiveProfileItem(itemId) {
  if (!confirm('确认归档此画像条目？')) return;
  const res = await api('/profile/item/archive', { method: 'POST', body: JSON.stringify({ id: itemId }) });
  if (res?.ok) {
    await loadProfileItems(currentUser);
    await loadProfileSummary(currentUser);
    toast('已归档', 'success');
  }
}

// ── Merge ───────────────────────────────────────────────────────────────────

async function mergeSelectedProfileItems() {
  const checked = [...document.querySelectorAll('.profile-merge-chk:checked')].map(cb => parseInt(cb.value));
  if (checked.length < 2) { toast('请至少勾选两条画像条目', 'error'); return; }
  // Guard: all selected items must share the same facet_type
  const selectedItems = profileItems.filter(item => checked.includes(item.id));
  const facetTypes = new Set(selectedItems.map(item => item.facet_type));
  if (facetTypes.size > 1) {
    toast('只能合并相同类型的画像条目', 'error');
    return;
  }
  if (!confirm(`确认将 ${checked.length} 条画像条目合并为一条？`)) return;
  const res = await api('/profile/items/merge', { method: 'POST', body: JSON.stringify({ user: currentUser, ids: checked }) });
  if (res?.ok) {
    await loadProfileItems(currentUser);
    await loadProfileSummary(currentUser);
    toast(`已合并，保留 #${res.keep_id}，已取代 ${res.archived_count} 条`, 'success');
  }
}

// ── Evidence Modal ──────────────────────────────────────────────────────────

async function openEvidenceModal(itemId) {
  const item = profileItems.find(i => i.id === itemId);
  const titleEl = document.getElementById('evidenceTitle');
  titleEl.textContent = item ? `证据链 — #${itemId}` : `证据链 — #${itemId}`;
  document.getElementById('evidenceBody').innerHTML = '<p style="color:var(--text2)">加载中...</p>';
  document.getElementById('evidenceModal').classList.add('show');

  const data = await api(`/profile/items/${itemId}/evidence`);
  const evidence = data?.evidence || [];
  const body = document.getElementById('evidenceBody');
  if (!evidence.length) {
    body.innerHTML = '<p style="color:var(--text2)">暂无证据记录</p>';
    return;
  }
  body.innerHTML = evidence.map(e => {
    const roleBadge = e.source_role === 'user' ? '👤 用户' : e.source_role === 'assistant' ? '🤖 助手' : e.source_role === 'manual' ? '✋ 人工' : `📋 ${escHtml(e.source_role)}`;
    return `<div class="evidence-item">
      <div class="evidence-meta">${roleBadge} · ${escHtml(e.evidence_kind)} · Δ${(e.confidence_delta >= 0 ? '+' : '')}${e.confidence_delta}</div>
      <div class="evidence-text">${escHtml(e.excerpt || e.source_excerpt || '(无文本)')}</div>
      <div class="evidence-time">${escHtml(e.created_at)}</div>
    </div>`;
  }).join('');
}

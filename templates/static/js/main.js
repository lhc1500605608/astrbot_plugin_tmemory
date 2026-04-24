/* main.js — 初始化入口：全局数据加载、事件绑定 */

async function loadStats() {
  const data = await api('/stats');
  if (!data) return;
  document.getElementById('statActive').textContent = data.total_active_memories ?? '-';
  document.getElementById('statDeactivated').textContent = data.total_deactivated_memories ?? '-';
  document.getElementById('statPending').textContent = data.pending_cached_rows ?? '-';
  document.getElementById('statPendingUsers').textContent = data.pending_users ?? '-';
}

async function loadUserData(userId) {
  currentUser = userId;
  const [memData, evtData] = await Promise.all([
    api('/memories?user=' + encodeURIComponent(userId)),
    api('/events?user=' + encodeURIComponent(userId))
  ]);
  allMemories = memData?.memories || [];
  renderMemoryTable(allMemories);
  renderMindmap(allMemories, userId);
  renderEvents(evtData?.events || []);
}

async function refreshAll() {
  await Promise.all([loadUsers(), loadStats()]);
  if (currentUser) await loadUserData(currentUser);
}

async function exportCurrentUser() {
  if (!currentUser) { toast('请先选择用户', 'error'); return; }
  const res = await api('/user/export', { method: 'POST', body: JSON.stringify({ user: currentUser }) });
  if (res && !res.error) {
    const blob = new Blob([JSON.stringify(res, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `tmemory_export_${currentUser}.json`;
    a.click(); URL.revokeObjectURL(url);
    toast('导出成功', 'success');
  }
}

async function purgeCurrentUser() {
  if (!currentUser) { toast('请先选择用户', 'error'); return; }
  if (!confirm(`确认清除 "${currentUser}" 的所有记忆和缓存？此操作不可撤销！`)) return;
  const res = await api('/user/purge', { method: 'POST', body: JSON.stringify({ user: currentUser }) });
  if (res && res.ok) {
    toast(`已清除：${res.memories} 条记忆，${res.cache} 条缓存`, 'success');
    currentUser = null;
    await refreshAll();
  }
}

async function doLogin() {
  const username = document.getElementById('loginUser').value.trim();
  const password = document.getElementById('loginPass').value;
  const errEl = document.getElementById('loginError');
  errEl.textContent = '';
  if (!username || !password) { errEl.textContent = '请输入用户名和密码'; return; }
  try {
    const resp = await fetch(API_BASE + '/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password })
    });
    const data = await resp.json();
    if (data.token) { setToken(data.token); showApp(); }
    else { errEl.textContent = data.error || '登录失败'; }
  } catch (e) { errEl.textContent = '连接失败: ' + e.message; }
}

// ── Init ──────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  if (getToken()) {
    fetch(API_BASE + '/stats', { headers: getAuthHeaders() })
      .then(r => { if (r.ok) showApp(); else logout(); })
      .catch(() => logout());
  }
  document.querySelectorAll('.modal-backdrop').forEach(el => {
    el.addEventListener('click', e => { if (e.target === el) el.classList.remove('show'); });
  });
});

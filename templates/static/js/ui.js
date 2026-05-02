/* ui.js — 通用 UI 工具函数 */

function escHtml(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function toast(msg, type = 'success') {
  const c = document.getElementById('toastContainer');
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  c.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 4000);
}

function closeModal(id) { document.getElementById(id).classList.remove('show'); }

function switchTab(tab, btn) {
  document.querySelectorAll('.tab-bar button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  ['panelMindmap','panelTable','panelPending','panelEvents','panelIdentity','panelHealth','panelRefine'].forEach(id => {
    document.getElementById(id).style.display = 'none';
  });
  const panelMap = { mindmap:'panelMindmap', table:'panelTable', pending:'panelPending', events:'panelEvents', identity:'panelIdentity', health:'panelHealth', refine:'panelRefine' };
  const panelId = panelMap[tab];
  if (!panelId) return;
  document.getElementById(panelId).style.display = '';
  if (tab === 'mindmap') requestAnimationFrame(() => renderMindmap(allMemories, currentUser));
  if (tab === 'pending') loadPendingQueue();
  if (tab === 'identity') loadIdentities();
  if (tab === 'health') loadHealth();
  if (tab === 'refine') loadRefineMemoryOptions();
}

function logout() {
  clearToken();
  document.getElementById('mainApp').style.display = 'none';
  document.getElementById('loginOverlay').classList.remove('hidden');
}

function showApp() {
  document.getElementById('loginOverlay').classList.add('hidden');
  document.getElementById('mainApp').style.display = '';
  refreshAll();
}

/* memories.js — 记忆列表渲染、CRUD 操作 */

let allMemories = [];
let memoryFilter = '';

function filterMemories(q) { memoryFilter = q.toLowerCase(); renderMemoryTable(allMemories); }

function scoreBar(val) {
  const v = Math.round((val || 0) * 100);
  const color = v >= 70 ? 'var(--accent2)' : v >= 40 ? 'var(--warn)' : 'var(--danger)';
  return `<div class="score-bar"><div class="score-bar-fill" style="width:${v}%;background:${color}"></div></div> ${(val||0).toFixed(2)}`;
}

function renderMemoryTable(memories) {
  const filtered = memoryFilter ? memories.filter(m => m.memory.toLowerCase().includes(memoryFilter)) : memories;
  const tbody = document.getElementById('memoryTableBody');
  if (filtered.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--text2);padding:40px">暂无记忆</td></tr>';
    return;
  }
  tbody.innerHTML = filtered.map(m => `
    <tr>
      <td>${m.id}</td>
      <td><span class="type-badge type-${m.memory_type}">${m.memory_type}</span></td>
      <td style="max-width:300px;word-break:break-all">${escHtml(m.memory)}</td>
      <td>${scoreBar(m.score)}</td>
      <td>${scoreBar(m.importance)}</td>
      <td>${scoreBar(m.confidence)}</td>
      <td style="text-align:center">${m.reinforce_count}</td>
      <td style="text-align:center">${m.is_pinned ? '📌' : ''}</td>
      <td>
        <button class="btn btn-sm" onclick="togglePin(${m.id}, ${!m.is_pinned})" title="${m.is_pinned ? '取消常驻' : '设为常驻'}">${m.is_pinned ? '📌' : '📍'}</button>
        <button class="btn btn-sm" onclick='openEditModal(${JSON.stringify(m)})'>✏️</button>
        <button class="btn btn-sm btn-danger" onclick="deleteMemory(${m.id})">🗑️</button>
      </td>
    </tr>
  `).join('');
}

function openEditModal(m) {
  document.getElementById('editModalTitle').textContent = m.id ? '编辑记忆 #' + m.id : '添加记忆';
  document.getElementById('editId').value = m.id || '';
  document.getElementById('editMemory').value = m.memory || '';
  document.getElementById('editType').value = m.memory_type || 'fact';
  document.getElementById('editScore').value = m.score ?? 0.7;
  document.getElementById('editImportance').value = m.importance ?? 0.6;
  document.getElementById('editConfidence').value = m.confidence ?? 0.7;
  document.getElementById('editPinned').checked = !!m.is_pinned;
  document.getElementById('editModal').classList.add('show');
}

function openAddMemoryModal() {
  openEditModal({ id: '', memory: '', memory_type: 'fact', score: 0.7, importance: 0.6, confidence: 0.7, is_pinned: false });
  document.getElementById('editModalTitle').textContent = '添加新记忆';
}

async function saveMemory() {
  const id = document.getElementById('editId').value;
  const body = {
    user: currentUser,
    memory: document.getElementById('editMemory').value,
    memory_type: document.getElementById('editType').value,
    score: parseFloat(document.getElementById('editScore').value) || 0.7,
    importance: parseFloat(document.getElementById('editImportance').value) || 0.6,
    confidence: parseFloat(document.getElementById('editConfidence').value) || 0.7,
    is_pinned: document.getElementById('editPinned').checked,
  };
  if (id) body.id = parseInt(id);
  const endpoint = id ? '/memory/update' : '/memory/add';
  const res = await api(endpoint, { method: 'POST', body: JSON.stringify(body) });
  if (res && !res.error) {
    toast(id ? '记忆已更新' : '记忆已添加', 'success');
    closeModal('editModal');
    loadUserData(currentUser);
  } else {
    toast('操作失败: ' + (res?.error || 'unknown'), 'error');
  }
}

async function deleteMemory(id) {
  if (!confirm(`确认删除记忆 #${id}？`)) return;
  const res = await api('/memory/delete', { method: 'POST', body: JSON.stringify({ id }) });
  if (res && !res.error) { toast('记忆已删除', 'success'); loadUserData(currentUser); loadStats(); }
  else { toast('删除失败', 'error'); }
}

async function togglePin(id, pinned) {
  const res = await api('/memory/pin', { method: 'POST', body: JSON.stringify({ id, pinned }) });
  if (res && res.ok) {
    toast(pinned ? '已设为常驻 📌' : '已取消常驻', 'success');
    if (currentUser) loadUserData(currentUser);
  }
}

function renderEvents(events) {
  const tbody = document.getElementById('eventsTableBody');
  if (events.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--text2);padding:40px">暂无事件</td></tr>';
    return;
  }
  tbody.innerHTML = events.map(e => `
    <tr>
      <td>${e.id}</td>
      <td style="font-size:12px;color:var(--text2)">${e.created_at}</td>
      <td><span class="type-badge type-${e.event_type === 'delete' ? 'restriction' : e.event_type === 'merge' ? 'task' : 'fact'}">${e.event_type}</span></td>
      <td style="font-size:12px;max-width:400px;word-break:break-all">${escHtml(e.payload_json)}</td>
    </tr>
  `).join('');
}

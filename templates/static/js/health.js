/* health.js — 系统健康面板（蒸馏历史 + 暂停控制） */

let isPaused = false;

async function loadHealth() {
  const data = await api('/distill/history');
  const tbody = document.getElementById('healthTableBody');
  if (!data || !data.history || data.history.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--text2);padding:40px">暂无蒸馏记录</td></tr>';
    return;
  }
  tbody.innerHTML = data.history.map(h => {
    const triggerBadge = h.trigger_type === 'auto' ? 'type-fact' : 'type-preference';
    const failStyle = h.users_failed > 0 ? 'color:var(--danger);font-weight:600' : '';
    const errText = h.errors && h.errors !== '[]' ? h.errors : '-';
    return `<tr>
      <td style="font-size:12px">${h.started_at}</td>
      <td><span class="type-badge ${triggerBadge}">${h.trigger_type}</span></td>
      <td style="text-align:center">${h.users_processed}</td>
      <td style="text-align:center">${h.memories_created}</td>
      <td style="text-align:center;${failStyle}">${h.users_failed}</td>
      <td style="text-align:center">${h.duration_sec}</td>
      <td style="font-size:11px;max-width:300px;word-break:break-all;color:var(--text2)">${escHtml(typeof errText === 'string' ? errText : JSON.stringify(errText))}</td>
    </tr>`;
  }).join('');
}

async function togglePause() {
  isPaused = !isPaused;
  const res = await api('/distill/pause', { method: 'POST', body: JSON.stringify({ pause: isPaused }) });
  if (res && res.ok) {
    document.getElementById('pauseBtn').textContent = isPaused ? '▶️ 恢复自动蒸馏' : '⏸️ 暂停自动蒸馏';
    document.getElementById('pauseStatus').textContent = isPaused ? '自动蒸馏已暂停' : '自动蒸馏运行中';
    toast(isPaused ? '已暂停自动蒸馏' : '已恢复自动蒸馏', 'success');
  }
}

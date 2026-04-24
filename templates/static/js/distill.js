/* distill.js — 蒸馏操作与待蒸馏队列 */

async function loadPendingQueue() {
  const data = await api('/pending');
  const tbody = document.getElementById('pendingTableBody');
  if (!data || !data.pending || data.pending.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--text2);padding:40px">当前没有待蒸馏消息</td></tr>';
    return;
  }
  tbody.innerHTML = data.pending.map(p => `
    <tr>
      <td style="cursor:pointer;color:var(--accent)" onclick="selectUser('${p.user.replace(/'/g, "\\'")}')">${escHtml(p.user)}</td>
      <td style="text-align:center"><span class="badge" style="background:#3d2e00;color:#e3b341">${p.count}</span></td>
      <td style="font-size:12px;color:var(--text2)">${p.oldest}</td>
      <td style="font-size:12px;color:var(--text2)">${p.newest}</td>
    </tr>
  `).join('');
}

async function triggerDistill() {
  closeModal('distillModal');
  toast('正在蒸馏，请稍候…', 'success');
  const res = await api('/distill', { method: 'POST' });
  if (res && res.ok) {
    if (res.processed_users === 0 && res.total_memories === 0) {
      toast('蒸馏完成：没有需要处理的数据（可能缓存为空或 LLM 不可用）', 'error');
    } else {
      toast(`蒸馏完成：${res.processed_users} 个用户，${res.total_memories} 条记忆`, 'success');
    }
    await refreshAll();
  } else {
    toast('蒸馏失败: ' + (res?.error || 'unknown'), 'error');
  }
}

function openDistillModal() { document.getElementById('distillModal').classList.add('show'); }

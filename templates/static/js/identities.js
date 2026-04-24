/* identities.js — 身份绑定管理 */

async function loadIdentities() {
  const data = await api('/identities');
  const tbody = document.getElementById('identityTableBody');
  if (!data || !data.bindings || data.bindings.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text2);padding:40px">暂无绑定记录</td></tr>';
    return;
  }
  let lastCanonical = '';
  tbody.innerHTML = data.bindings.map(b => {
    const isNewGroup = b.canonical_user_id !== lastCanonical;
    lastCanonical = b.canonical_user_id;
    const sep = isNewGroup ? 'border-top:2px solid var(--border)' : '';
    return `<tr style="${sep}">
      <td>${b.id}</td>
      <td><span class="type-badge type-fact">${escHtml(b.adapter)}</span></td>
      <td>${escHtml(b.adapter_user_id)}</td>
      <td style="font-weight:${isNewGroup ? '600' : '400'}">${escHtml(b.canonical_user_id)}</td>
      <td style="font-size:12px;color:var(--text2)">${b.updated_at}</td>
      <td><button class="btn btn-sm" onclick="openRebindModal(${b.id}, '${escHtml(b.adapter)}:${escHtml(b.adapter_user_id)}', '${escHtml(b.canonical_user_id)}')">✏️ 改绑</button></td>
    </tr>`;
  }).join('');
}

function openMergeModal() {
  document.getElementById('mergeFrom').value = '';
  document.getElementById('mergeTo').value = '';
  document.getElementById('mergeModal').classList.add('show');
}

async function doMerge() {
  const from = document.getElementById('mergeFrom').value.trim();
  const to = document.getElementById('mergeTo').value.trim();
  if (!from || !to) { toast('请填写来源和目标用户 ID', 'error'); return; }
  if (from === to) { toast('两个 ID 相同', 'error'); return; }
  if (!confirm(`确认将 "${from}" 的所有数据合并到 "${to}"？此操作不可撤销。`)) return;
  const res = await api('/identity/merge', { method: 'POST', body: JSON.stringify({ from_user: from, to_user: to }) });
  if (res && res.ok) {
    toast(`合并完成：${res.from_user} → ${res.to_user}，迁移 ${res.moved} 条记忆`, 'success');
    closeModal('mergeModal');
    await refreshAll();
    loadIdentities();
  } else {
    toast('合并失败: ' + (res?.error || 'unknown'), 'error');
  }
}

function openRebindModal(bindingId, displayInfo, currentCanonical) {
  document.getElementById('rebindId').value = bindingId;
  document.getElementById('rebindInfo').value = displayInfo + ' → ' + currentCanonical;
  document.getElementById('rebindNewCanonical').value = '';
  document.getElementById('rebindModal').classList.add('show');
}

async function doRebind() {
  const bindingId = parseInt(document.getElementById('rebindId').value);
  const newCanonical = document.getElementById('rebindNewCanonical').value.trim();
  if (!bindingId || !newCanonical) { toast('请填写新的统一用户 ID', 'error'); return; }
  const res = await api('/identity/rebind', { method: 'POST', body: JSON.stringify({ binding_id: bindingId, new_canonical_user_id: newCanonical }) });
  if (res && res.ok) {
    toast('改绑成功', 'success');
    closeModal('rebindModal');
    loadIdentities();
    await refreshAll();
  } else {
    toast('改绑失败: ' + (res?.error || 'unknown'), 'error');
  }
}

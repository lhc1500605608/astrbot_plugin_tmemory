/* refine.js — 手动精馏、记忆合并与拆分 */

async function loadRefineMemoryOptions() {
  if (!currentUser) return;
  const data = await api('/memories?user=' + encodeURIComponent(currentUser));
  const mems = data?.memories || [];

  const mergeEl = document.getElementById('mergeMemSelect');
  mergeEl.innerHTML = mems.map(m =>
    `<option value="${m.id}">[#${m.id}${m.is_pinned ? ' 📌' : ''}] (${m.memory_type}) ${escHtml(m.memory.slice(0, 60))}</option>`
  ).join('');

  const splitEl = document.getElementById('splitMemSelect');
  splitEl.innerHTML = '<option value="">— 选择一条记忆 —</option>' + mems.map(m =>
    `<option value="${m.id}">[#${m.id}${m.is_pinned ? ' 📌' : ''}] (${m.memory_type}) ${escHtml(m.memory.slice(0, 60))}</option>`
  ).join('');
}

async function runRefine() {
  if (!currentUser) { toast('请先选择用户', 'error'); return; }
  const mode = document.getElementById('refineMode').value;
  const limit = parseInt(document.getElementById('refineLimit').value) || 20;
  const dry_run = document.getElementById('refineDryRun').checked;
  const include_pinned = document.getElementById('refineIncludePinned').checked;
  const extra = document.getElementById('refineExtra').value.trim();

  const btn = document.querySelector('#panelRefine .btn-primary');
  const oldText = btn.textContent;
  btn.textContent = '⏳ 处理中…';
  btn.disabled = true;

  const res = await api('/memory/refine', {
    method: 'POST',
    body: JSON.stringify({ user: currentUser, mode, limit, dry_run, include_pinned, extra_instruction: extra }),
  });

  btn.textContent = oldText;
  btn.disabled = false;

  const resultEl = document.getElementById('refineResult');
  if (res && res.ok !== undefined) {
    const prefix = dry_run ? '📋 预览结果（未落库）' : '✅ 精馏完成';
    resultEl.style.display = '';
    resultEl.innerHTML = `<strong>${prefix}</strong>：更新 <b>${res.updates}</b> 条 · 新增 <b>${res.adds}</b> 条 · 删除 <b>${res.deletes}</b> 条` +
      (res.note ? `<br><span style="color:var(--text2);font-size:12px">备注：${escHtml(res.note)}</span>` : '');
    if (!dry_run) {
      await loadUserData(currentUser);
      loadRefineMemoryOptions();
    }
  } else {
    resultEl.style.display = '';
    resultEl.innerHTML = `<span style="color:var(--danger)">精馏失败：${escHtml(res?.error || 'unknown')}</span>`;
  }
}

async function doMemMerge() {
  if (!currentUser) { toast('请先选择用户', 'error'); return; }
  const select = document.getElementById('mergeMemSelect');
  const ids = Array.from(select.selectedOptions).map(o => parseInt(o.value));
  if (ids.length < 2) { toast('请至少选择两条记忆', 'error'); return; }
  const memory = document.getElementById('mergeMemText').value.trim();
  if (!confirm(`确认合并 ${ids.length} 条记忆？保留第一条，删除其余。此操作不可撤销。`)) return;

  const res = await api('/memory/merge', {
    method: 'POST',
    body: JSON.stringify({ user: currentUser, ids, memory }),
  });
  if (res && res.ok) {
    toast(`合并完成：保留 #${res.keep_id}，删除 ${res.deleted} 条`, 'success');
    document.getElementById('mergeMemText').value = '';
    await loadUserData(currentUser);
    loadRefineMemoryOptions();
  } else {
    toast('合并失败: ' + (res?.error || 'unknown'), 'error');
  }
}

async function doMemSplit() {
  if (!currentUser) { toast('请先选择用户', 'error'); return; }
  const memId = parseInt(document.getElementById('splitMemSelect').value);
  if (!memId) { toast('请选择要拆分的记忆', 'error'); return; }
  const rawSegments = document.getElementById('splitMemSegments').value.trim();

  let segments = null;
  if (rawSegments) {
    segments = rawSegments.split('|').map(s => s.trim()).filter(Boolean);
    if (segments.length < 2) { toast('请至少输入两个片段，用 | 分隔', 'error'); return; }
  }

  const btn = document.querySelectorAll('#panelRefine .btn-primary')[2];
  if (btn) { btn.textContent = '⏳ 处理中…'; btn.disabled = true; }

  const body = { user: currentUser, id: memId };
  if (segments) body.segments = segments;

  const res = await api('/memory/split', { method: 'POST', body: JSON.stringify(body) });
  if (btn) { btn.textContent = '✂️ 拆分'; btn.disabled = false; }

  if (res && res.ok) {
    toast(`拆分完成：原记忆 #${res.base_id} + 新增 ${res.added_ids.length} 条`, 'success');
    document.getElementById('splitMemSegments').value = '';
    document.getElementById('splitMemSelect').value = '';
    await loadUserData(currentUser);
    loadRefineMemoryOptions();
  } else {
    toast('拆分失败: ' + (res?.error || 'unknown'), 'error');
  }
}

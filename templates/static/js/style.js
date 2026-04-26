/* style.js — 聊天风格记忆管理 (v3: 双开关 + 人格档案 + 对话绑定) */

let styleFilter = '';
let allStyleMemories = [];
let currentStyleConfig = null;

function filterStyle(q) { styleFilter = q.toLowerCase(); renderStyleTable(allStyleMemories); }

function renderStyleTable(memories) {
  const filtered = styleFilter ? memories.filter(m => m.memory.toLowerCase().includes(styleFilter)) : memories;
  const tbody = document.getElementById('styleTableBody');
  if (filtered.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text2);padding:40px">暂无聊天风格记忆<br><small>等待 LLM 蒸馏提取或选择用户后查看</small></td></tr>';
    return;
  }
  tbody.innerHTML = filtered.map(m => `
    <tr>
      <td>${m.id}</td>
      <td style="font-size:12px;color:var(--accent)">${escHtml(currentUser || '-')}</td>
      <td style="max-width:300px;word-break:break-all">${escHtml(m.memory)}</td>
      <td>${scoreBar(m.score)}</td>
      <td>${scoreBar(m.importance)}</td>
      <td>${scoreBar(m.confidence)}</td>
      <td style="text-align:center">${m.is_pinned ? '📌' : ''}</td>
      <td>
        <button class="btn btn-sm" onclick="togglePin(${m.id}, ${!m.is_pinned})" title="${m.is_pinned ? '取消常驻' : '设为常驻'}">${m.is_pinned ? '📌' : '📍'}</button>
        <button class="btn btn-sm" onclick='openEditModal(${JSON.stringify(m)})'>✏️</button>
        <button class="btn btn-sm btn-danger" onclick="deleteMemory(${m.id})">🗑️</button>
      </td>
    </tr>
  `).join('');
}

async function loadStyleData() {
  const [stats, memData, configData] = await Promise.all([
    api('/style/stats'),
    currentUser ? api('/style/memories?user=' + encodeURIComponent(currentUser)) : Promise.resolve(null),
    api('/config?keys=style_distill_settings')
  ]);

  if (configData && configData.style_distill_settings) {
    currentStyleConfig = configData.style_distill_settings;
    // 风格采集开关 — 只读状态展示，唯一写入口为 /style_distill on|off 指令
    const distillOn = currentStyleConfig.enable_style_distill !== false;
    document.getElementById('styleDistillStatus').innerHTML =
      distillOn ? '<span style="color:#3fb950">● 采集中</span>' : '<span style="color:#f85149">● 已暂停</span>';
    // 人格注入开关 — WebUI 可写
    document.getElementById('styleInjectionSwitch').checked = currentStyleConfig.enable_style_injection === true;
    document.getElementById('styleMinConf').value = currentStyleConfig.style_min_confidence || 0.55;
    document.getElementById('styleMinImp').value = currentStyleConfig.style_min_importance || 0.4;
  }

  const globalConfig = await api('/config?keys=memory_scope');
  if (globalConfig && globalConfig.memory_scope) {
    document.getElementById('styleScopeConfig').value = globalConfig.memory_scope;
  }

  if (stats) {
    document.getElementById('styleTotal').textContent = stats.total_style_memories ?? '-';
    document.getElementById('styleUsers').textContent = stats.style_users ?? '-';
    document.getElementById('styleAvgConf').textContent = stats.avg_confidence ?? '-';
  }
  if (memData && memData.memories) {
    allStyleMemories = memData.memories;
    renderStyleTable(allStyleMemories);
  } else {
    allStyleMemories = [];
    renderStyleTable([]);
  }

  // v3: 加载人格档案与会话绑定
  loadProfiles();
  loadBindings();
}

async function toggleStyleInjection(enabled) {
  if (!currentStyleConfig) currentStyleConfig = {};
  currentStyleConfig.enable_style_injection = enabled;
  const res = await api('/config', {
    method: 'PATCH',
    body: JSON.stringify({ style_distill_settings: currentStyleConfig })
  });
  if (res) {
    toast('人格注入已' + (enabled ? '开启' : '关闭'));
  } else {
    loadStyleData();
  }
}

async function updateStyleConfig(key, value) {
  // enable_style_distill 不可通过 WebUI 修改，唯一写入口为 /style_distill 指令
  if (key === 'enable_style_distill') return;
  if (!currentStyleConfig) currentStyleConfig = {};

  let val = parseFloat(value);
  if (isNaN(val)) return;

  currentStyleConfig[key] = val;

  const res = await api('/config', {
    method: 'PATCH',
    body: JSON.stringify({ style_distill_settings: currentStyleConfig })
  });

  if (res) {
    toast('风格蒸馏配置已更新');
  } else {
    loadStyleData();
  }
}

async function updateGlobalConfig(key, value) {
  const res = await api('/config', {
    method: 'PATCH',
    body: JSON.stringify({ [key]: value })
  });

  if (res) {
    toast('全局配置已更新');
  } else {
    loadStyleData();
  }
}

async function forceStyleDistill() {
  if (!currentUser) {
    toast('请先在左侧选择一个用户', 'error');
    return;
  }
  if (!confirm(`确定要立即对用户 ${currentUser} 进行强制风格蒸馏吗？`)) return;

  const res = await api('/distill/trigger', {
    method: 'POST',
    body: JSON.stringify({ user: currentUser, type: 'style', force: true })
  });

  if (res) {
    toast('已触发强制风格蒸馏任务');
    setTimeout(loadStyleData, 2000);
  }
}

// ── 人格档案管理 (v3) ─────────────────────────────────────────────

async function loadProfiles() {
  const data = await api('/style/profiles');
  const list = document.getElementById('styleProfilesList');
  if (!data || !data.profiles || data.profiles.length === 0) {
    list.innerHTML = '<span style="color:var(--text2)">暂无自定义人格档案。创建档案后可绑定到具体会话。</span>';
    return;
  }
  list.innerHTML = data.profiles.map(p => `
    <div style="display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--border)">
      <span style="font-weight:600">${escHtml(p.profile_name)}</span>
      <span style="color:var(--text2)">${escHtml((p.prompt_supplement || '').substring(0, 60))}...</span>
      <span style="color:var(--text2);font-size:11px">${escHtml(p.description || '')}</span>
      <div style="flex:1"></div>
      <button class="btn btn-sm btn-danger" onclick="deleteProfile(${p.id},'${escHtml(p.profile_name)}')">删除</button>
    </div>
  `).join('');
}

function openProfileCreateModal() {
  document.getElementById('profileModal').classList.add('show');
  document.getElementById('profileName').value = '';
  document.getElementById('profilePrompt').value = '';
  document.getElementById('profileDesc').value = '';
}

async function createProfile() {
  const name = document.getElementById('profileName').value.trim();
  const prompt_supplement = document.getElementById('profilePrompt').value.trim();
  const description = document.getElementById('profileDesc').value.trim();
  if (!name || !prompt_supplement) { toast('名称和人格补充提示词不能为空', 'error'); return; }

  const res = await api('/style/profile/create', {
    method: 'POST',
    body: JSON.stringify({ name, prompt_supplement, description })
  });
  if (res && res.ok) {
    toast(`人格档案 "${name}" 已创建`);
    closeModal('profileModal');
    loadProfiles();
  }
}

async function deleteProfile(id, name) {
  if (!confirm(`确定删除人格档案 "${name}"？相关会话绑定将自动解除。`)) return;
  await api('/style/profile/delete', {
    method: 'POST',
    body: JSON.stringify({ id })
  });
  toast(`人格档案 "${name}" 已删除`);
  loadProfiles();
  loadBindings();
}

// ── 会话绑定管理 (v3) ─────────────────────────────────────────────

async function loadBindings() {
  const data = await api('/style/bindings');
  const list = document.getElementById('styleBindingsList');
  if (!data || !data.bindings || data.bindings.length === 0) {
    list.innerHTML = '<span style="color:var(--text2)">暂无会话绑定。使用 /style_bind &lt;档案名&gt; 命令或在下方手动绑定。</span>';
    list.innerHTML += `
      <div style="margin-top:8px;display:flex;gap:8px;align-items:center">
        <span style="font-size:12px;color:var(--text2)">手动绑定:</span>
        <input id="bindAdapter" type="text" class="search-box" style="width:110px" placeholder="adapter名称">
        <input id="bindConv" type="text" class="search-box" style="width:160px" placeholder="conversation_id">
        <select id="bindProfileSelect" class="search-box" style="width:130px"></select>
        <button class="btn btn-sm btn-primary" onclick="doSetBinding()">绑定</button>
      </div>`;
    loadProfileSelect();
    return;
  }
  let html = data.bindings.map(b => `
    <div style="display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--border)">
      <span style="font-size:11px;color:var(--text2)">${escHtml(b.adapter_name)}</span>
      <span style="font-size:11px">→ ${escHtml((b.conversation_id || '').substring(0, 24))}</span>
      <span style="font-weight:600;color:var(--accent)">${escHtml(b.profile_name || '(未绑定档案)')}</span>
      <span style="font-size:11px;color:var(--text2)">${escHtml(b.updated_at || '')}</span>
      <div style="flex:1"></div>
      <button class="btn btn-sm btn-danger" onclick="doRemoveBinding('${escHtml(b.adapter_name)}','${escHtml(b.conversation_id)}')">解除</button>
    </div>
  `).join('');
  html += `
    <div style="margin-top:8px;display:flex;gap:8px;align-items:center">
      <span style="font-size:12px;color:var(--text2)">手动绑定:</span>
      <input id="bindAdapter" type="text" class="search-box" style="width:110px" placeholder="adapter名称">
      <input id="bindConv" type="text" class="search-box" style="width:160px" placeholder="conversation_id">
      <select id="bindProfileSelect" class="search-box" style="width:130px"></select>
      <button class="btn btn-sm btn-primary" onclick="doSetBinding()">绑定</button>
    </div>`;
  list.innerHTML = html;
  loadProfileSelect();
}

async function loadProfileSelect() {
  const data = await api('/style/profiles');
  const sel = document.getElementById('bindProfileSelect');
  if (!sel) return;
  if (!data || !data.profiles || data.profiles.length === 0) {
    sel.innerHTML = '<option value="">无可用档案</option>';
    return;
  }
  sel.innerHTML = data.profiles.map(p => `<option value="${p.id}">${escHtml(p.profile_name)}</option>`).join('');
}

async function doSetBinding() {
  const adapter_name = document.getElementById('bindAdapter').value.trim();
  const conversation_id = document.getElementById('bindConv').value.trim();
  const profile_id = parseInt(document.getElementById('bindProfileSelect').value);
  if (!adapter_name || !conversation_id || !profile_id) {
    toast('请填写 adapter 名称、conversation_id 并选择档案', 'error');
    return;
  }
  await api('/style/binding/set', {
    method: 'POST',
    body: JSON.stringify({ adapter_name, conversation_id, profile_id })
  });
  toast('绑定已设置');
  loadBindings();
}

async function doRemoveBinding(adapter_name, conversation_id) {
  if (!confirm(`确定解除绑定？`)) return;
  await api('/style/binding/remove', {
    method: 'POST',
    body: JSON.stringify({ adapter_name, conversation_id })
  });
  toast('绑定已解除');
  loadBindings();
}

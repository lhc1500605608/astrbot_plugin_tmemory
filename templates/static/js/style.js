/* style.js — 聊天风格记忆管理 */

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
    document.getElementById('styleEnableSwitch').checked = currentStyleConfig.enable_style_distill !== false;
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
}

async function toggleStyleDistill(enabled) {
  await updateStyleConfig('enable_style_distill', enabled);
}

async function updateStyleConfig(key, value) {
  if (!currentStyleConfig) currentStyleConfig = {};
  
  let val = value;
  if (key !== 'enable_style_distill') {
    val = parseFloat(value);
    if (isNaN(val)) return;
  }
  
  currentStyleConfig[key] = val;
  
  const res = await api('/config', {
    method: 'PATCH',
    body: JSON.stringify({ style_distill_settings: currentStyleConfig })
  });
  
  if (res) {
    toast('风格蒸馏配置已更新');
  } else {
    // Revert UI on failure
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

/* users.js — 用户列表渲染与选择 */

let currentUser = null;
let allUsers = [];

async function loadUsers() {
  const data = await api('/users');
  if (!data) return;
  allUsers = data.users || [];
  renderUserList(allUsers);
  if (!currentUser && allUsers.length > 0) selectUser(allUsers[0].id);
}

function renderUserList(users) {
  const ul = document.getElementById('userList');
  if (users.length === 0) {
    ul.innerHTML = '<li style="color:var(--text2);justify-content:center">暂无用户数据</li>';
    return;
  }
  ul.innerHTML = '';
  users.forEach(u => {
    const active = u.id === currentUser;
    const li = document.createElement('li');
    if (active) li.classList.add('active');
    li.onclick = () => selectUser(u.id);

    // Avatar (Phase 3)
    const av = makeAvatar(u.id, 'avatar-sm');
    li.appendChild(av);

    const label = document.createElement('span');
    label.className = 'user-label';
    label.title = u.id;
    label.textContent = u.id;
    li.appendChild(label);

    const badges = document.createElement('span');
    badges.style.cssText = 'display:flex;gap:4px;flex-shrink:0';
    if (u.memory_count > 0) {
      const m = document.createElement('span');
      m.className = 'badge';
      m.textContent = u.memory_count;
      badges.appendChild(m);
    }
    if (u.pending_count > 0) {
      const p = document.createElement('span');
      p.className = 'badge';
      p.style.cssText = 'background:#3d2e00;color:#e3b341';
      p.textContent = u.pending_count + '⏳';
      badges.appendChild(p);
    }
    li.appendChild(badges);
    ul.appendChild(li);
  });
}

function filterUsers(q) {
  const filtered = allUsers.filter(u => u.id.toLowerCase().includes(q.toLowerCase()));
  renderUserList(filtered);
}

function selectUser(userId) {
  currentUser = userId;
  renderUserList(allUsers);
  loadUserData(userId);
}

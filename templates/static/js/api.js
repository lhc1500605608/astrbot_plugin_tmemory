/* api.js — HTTP 请求封装与认证 */
const API_BASE = window.location.origin + '/api';

function getToken() { return localStorage.getItem('tmemory_token') || ''; }
function setToken(t) { localStorage.setItem('tmemory_token', t); }
function clearToken() { localStorage.removeItem('tmemory_token'); }
function getAuthHeaders() {
  const token = getToken();
  return token ? { 'Authorization': `Bearer ${token}` } : {};
}

async function api(path, opts = {}) {
  const url = API_BASE + path;
  try {
    const resp = await fetch(url, {
      headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
      ...opts
    });
    if (resp.status === 401) { logout(); toast('登录已过期', 'error'); return null; }
    const ct = resp.headers.get('content-type') || '';
    if (!ct.includes('application/json')) {
      const text = await resp.text();
      console.error('Non-JSON response:', resp.status, text.slice(0, 200));
      toast(`服务端错误 (${resp.status})`, 'error');
      return null;
    }
    const data = await resp.json();
    if (resp.status >= 400 && data.error) {
      toast(data.error, 'error');
      return null;
    }
    return data;
  } catch (e) {
    console.error('API error:', e);
    toast('请求失败: ' + e.message, 'error');
    return null;
  }
}

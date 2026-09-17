/* plugin_page_bridge.js — Dashboard 托管插件页的 API 层（替代 legacy api.js）
 *
 * 与 legacy api.js 的区别：
 * - API_BASE 指向 bridge 路由 /api/astrbot_plugin_tmemory
 * - 无自签 JWT，无 login/logout/token 管理（鉴权由 Dashboard 统一处理）
 * - 错误信封兼容 bridge 的 {status:"error", message:...} 与 legacy 的 {error:...}
 */

const PLUGIN_NAME = 'astrbot_plugin_tmemory';
const API_BASE = window.location.origin + '/api/' + PLUGIN_NAME;

async function api(path, opts = {}) {
  const url = API_BASE + path;
  try {
    const resp = await fetch(url, {
      headers: { 'Content-Type': 'application/json' },
      ...opts
    });
    const ct = resp.headers.get('content-type') || '';
    if (!ct.includes('application/json')) {
      const text = await resp.text();
      console.error('Non-JSON response:', resp.status, text.slice(0, 200));
      toast(`服务端错误 (${resp.status})`, 'error');
      return null;
    }
    const data = await resp.json();
    if (resp.status >= 400) {
      const msg = data.message || data.error || '请求失败';
      toast(msg, 'error');
      return null;
    }
    if (data && data.status === 'error') {
      toast(data.message || '请求失败', 'error');
      return null;
    }
    return data;
  } catch (e) {
    console.error('API error:', e);
    toast('请求失败: ' + e.message, 'error');
    return null;
  }
}

function getToken() { return ''; }
function setToken() {}
function clearToken() {}
function getAuthHeaders() { return {}; }

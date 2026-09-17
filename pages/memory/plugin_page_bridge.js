/* plugin_page_bridge.js — Dashboard 托管插件页的 API 层（替代 legacy api.js）
 *
 * 与 legacy api.js 的区别：
 * - 通过 AstrBotPluginPage SDK（postMessage → 父窗口代理）发起请求
 * - 无自签 JWT，无 login/logout/token 管理（鉴权由 Dashboard 统一处理）
 * - 错误信封兼容 bridge 的 {status:"error", message:...} 与 legacy 的 {error:...}
 */

const PLUGIN_NAME = 'astrbot_plugin_tmemory';

async function api(path, opts = {}) {
  const sdk = window.AstrBotPluginPage;
  if (!sdk) {
    console.error('API error: AstrBotPluginPage SDK not loaded');
    toast('SDK 未加载，请刷新页面重试', 'error');
    return null;
  }
  const endpoint = path.charAt(0) === '/' ? path.slice(1) : path;
  const isPost = (opts.method && opts.method.toUpperCase() === 'POST') || opts.body;
  try {
    let data;
    if (isPost) {
      const body = opts.body ? JSON.parse(opts.body) : {};
      data = await sdk.apiPost(endpoint, body);
    } else {
      const qIdx = endpoint.indexOf('?');
      const ep = qIdx >= 0 ? endpoint.slice(0, qIdx) : endpoint;
      const qs = qIdx >= 0 ? endpoint.slice(qIdx + 1) : '';
      const params = {};
      if (qs) {
        qs.split('&').forEach(kv => {
          const parts = kv.split('=');
          params[decodeURIComponent(parts[0])] = decodeURIComponent(parts[1] || '');
        });
      }
      data = await sdk.apiGet(ep, params);
    }
    if (data && data.status === 'error') {
      toast(data.message || '请求失败', 'error');
      return null;
    }
    return data;
  } catch (e) {
    console.error('API error:', e);
    toast('请求失败: ' + (e.message || e), 'error');
    return null;
  }
}

function getToken() { return ''; }
function setToken() {}
function clearToken() {}
function getAuthHeaders() { return {}; }

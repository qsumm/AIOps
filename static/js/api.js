// api.js —— 统一 API 客户端（免构建 ESM）
// 约定：后端响应包 {code,msg,data}；code!==0 → 抛错（统一 toast）
//      （D8 起后端**所有**错误也走同一信封：401/403/404/409/422/500 都带可读 msg）
const TOKEN_KEY = "ops_token";
const USER_KEY = "ops_user";
const PERM_KEY = "ops_perms";
const AUTH_PATHS = ["/api/auth/login", "/api/auth/demo"];  // 这两个接口的 401 是"密码错"，不是"登录态失效"

export function getToken() { return localStorage.getItem(TOKEN_KEY) || ""; }
export function getUser() { try { return JSON.parse(localStorage.getItem(USER_KEY) || "null"); } catch { return null; } }
export function setAuth(token, user) {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(USER_KEY, JSON.stringify(user || {}));
}
export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY); localStorage.removeItem(USER_KEY); localStorage.removeItem(PERM_KEY);
}

// ---- 能力集（D8）：按钮可见性一律取自服务端 /api/auth/perms，不在页面里按 role 硬编码 ----
let _perms = null;
export function cachedPerms() {
  if (_perms) return _perms;
  try { _perms = JSON.parse(localStorage.getItem(PERM_KEY) || "null"); } catch { _perms = null; }
  return _perms;
}
export async function loadPerms() {
  const c = cachedPerms();
  if (c) return c;
  return refreshPerms();
}
export async function refreshPerms() {
  const data = await request("GET", "/api/auth/perms");
  _perms = data?.can || {};
  localStorage.setItem(PERM_KEY, JSON.stringify(_perms));
  return _perms;
}
/** can("write") / can("alert_ops") / can("diag") / can("chat")；拿不到能力集时按最小权限处理。 */
export function can(level) {
  const p = cachedPerms();
  if (!p) return level === "read";
  return !!p[level];
}

export class ApiError extends Error {
  constructor(msg, code, status) { super(msg); this.code = code; this.status = status; }
}

async function request(method, path, { json, params, form } = {}) {
  const url = new URL(path, location.origin);
  const run = async () => {
  if (params) Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
  });
  const headers = {};
  const tok = getToken();
  if (tok) headers.Authorization = `Bearer ${tok}`;
  let body;
  if (json !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(json); }
  if (form) { body = new URLSearchParams(form); headers["Content-Type"] = "application/x-www-form-urlencoded"; }

  const r = await fetch(url, { method, headers, body });
  // 登录/演示登录接口的 401 是"账号密码错误"，不能当成"登录态失效"去清空并跳转
  // （D8 实测缺陷：输错密码时提示变成"登录已失效，请重新登录"，误导排查方向）
  if (r.status === 401 && !AUTH_PATHS.includes(path)) {
    clearAuth(); location.hash = "#/login";
    throw new ApiError("登录已失效，请重新登录", 401, 401);
  }
  let payload = null;
  try { payload = await r.json(); } catch { /* 非 JSON */ }
  const msg = payload?.msg || payload?.detail || `HTTP ${r.status}`;
  if (r.status === 403) throw new ApiError(payload?.msg || "无权限（当前角色不允许此操作）", 403, 403);
  if (!r.ok) throw new ApiError(msg, payload?.code ?? r.status, r.status);
  if (payload && typeof payload.code === "number" && payload.code !== 0) {
    throw new ApiError(payload.msg || "请求失败", payload.code, r.status);
  }
  return payload ? payload.data : null;
  };
  try {
    return await run();
  } catch (e) {
    // 偶现"Failed to fetch"（网络瞬时断/连接重置，见于资源偏紧的部署）：GET 是幂等读，
    // 自动重试一次自愈，不进"加载失败"空态；非 GET 或已是服务端响应(ApiError)不重试，
    // 避免把已生效的写操作重复执行（D93）。
    if (method === "GET" && !(e instanceof ApiError)) return await run();
    throw e;
  }
}

export const api = {
  get: (p, params) => request("GET", p, { params }),
  post: (p, json) => request("POST", p, { json }),
  put: (p, json) => request("PUT", p, { json }),
  patch: (p, json) => request("PATCH", p, { json }),
  del: (p) => request("DELETE", p),
};

// 登录（后端 LoginIn 为 JSON body；见 05 §1）
export async function login(username, password) {
  const data = await request("POST", "/api/auth/login", { json: { username, password } });
  setAuth(data.access_token, { username: data.username || username, role: data.role || "member" });
  // D97：把服务端 preferred_lang 同步到前端 i18n 模块
  try { if (data?.preferred_lang) (await import("./i18n.js")).applyServerLang(data.preferred_lang); } catch {}
  await refreshPerms().catch(() => {});   // 登录后立刻取能力集，页面据此渲染按钮
  return data;
}
export async function loginDemo() {
  const data = await request("POST", "/api/auth/demo", { json: {} });
  setAuth(data.access_token, { username: data.username || "demo", role: "demo" });
  try { if (data?.preferred_lang) (await import("./i18n.js")).applyServerLang(data.preferred_lang); } catch {}
  await refreshPerms().catch(() => {});
  return data;
}
export async function me() { return request("GET", "/api/auth/me"); }

// SSE 告警流：先换一次性 ticket（03 §1.22，EventSource 不能带 header），再以 ?ticket= 建流
// ticket 消费即焚 → 断线重连时重新取票
export async function openAlertStream(onEvent) {
  let closed = false;
  let es = null;
  const connect = async () => {
    if (closed) return;
    let ticket;
    try { ticket = (await request("GET", "/api/sse-ticket")).ticket; }
    catch { setTimeout(connect, 5000); return; }
    es = new EventSource(`/api/ops/alerts/stream?ticket=${encodeURIComponent(ticket)}`);
    es.addEventListener("hello", () => onEvent("hello", {}));
    ["firing", "recovered", "updated", "ack", "resolved"].forEach((t) =>
      es.addEventListener(t, (e) => { try { onEvent(t, JSON.parse(e.data)); } catch { /* ignore */ } }));
    es.onerror = () => { es.close(); if (!closed) setTimeout(connect, 3000); };  // 重连需新票
    es.addEventListener("hello", () => { /* 已连上 */ });
  };
  await connect();
  return { close: () => { closed = true; es?.close(); } };
}

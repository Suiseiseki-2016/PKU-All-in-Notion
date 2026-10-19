"use strict";
(() => {
let token = "";
const el = id => document.getElementById(id);
const show = message => { el("notice").textContent = message; };
async function call(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "Authorization": "Bearer " + token, "Content-Type": "application/json" }, cache: "no-store" });
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || "\u8bf7\u6c42\u5931\u8d25");
  return body;
}
function put(id, value) { el(id).textContent = String(value); }
async function refresh() {
  const [stats, data] = await Promise.all([call("/admin/api/overview"), call("/admin/api/accounts")]);
  put("accounts-total", stats.accounts.total); put("codes-available", stats.codes.available);
  put("asr-cost", stats.asr.estimated_cost_rmb); put("llm-cost", stats.llm.estimated_cost_rmb);
  put("cache-rate", stats.cache.hit_rate === null ? "\u5c1a\u65e0\u6570\u636e" : (stats.cache.hit_rate * 100).toFixed(1) + "%");
  put("cache-saved", stats.cache.estimated_saved_cost_rmb); put("total-cost", stats.estimated_total_cost_rmb);
  put("usage-note", "\u8f6c\u5199 " + stats.asr.upstream_calls + " \u6b21\uff0c\u8ba1\u8d39 " + stats.asr.charged_seconds + " \u79d2\uff1bAI " + stats.llm.calls + " \u6b21\uff0c\u8f93\u5165 " + stats.llm.input_tokens + " token\uff0c\u8f93\u51fa " + stats.llm.output_tokens + " token\uff1b\u7f13\u5b58\u8282\u7701 " + stats.cache.saved_seconds + " \u79d2\u3002");
  put("cost-note", "\u4f30\u7b97\uff1a\u8f6c\u5199 " + stats.asr.unit_cost_rmb_per_second + " \u5143/\u79d2\uff1bAI \u6309\u8ba1\u5165\u70b9\u6570\u4f30\u7b97\u3002\u672a\u8ba1\u5165\u514d\u8d39\u989d\u5ea6\u3001\u6298\u6263\u3001OSS\u3001\u6536\u5165\u4e0e\u6bdb\u5229\u3002\u7f13\u5b58\u547d\u4e2d " + stats.cache.hits + " \u6b21\uff0c\u672a\u547d\u4e2d " + stats.cache.misses + " \u6b21\uff1b\u4ece\u529f\u80fd\u542f\u7528\u540e\u8ba1\u6570\u3002");
  const tbody = el("accounts"); tbody.replaceChildren();
  for (const account of data.accounts) {
    const row = document.createElement("tr");
    const values = [account.id, account.email || account.label, account.email_verified === null ? "\u65e7\u7248\u8d26\u53f7" : (account.email_verified ? "\u5df2\u9a8c\u8bc1" : "\u672a\u9a8c\u8bc1"), account.status, Math.floor(account.transcribe_seconds_remaining / 60), account.llm_points_remaining];
    for (const value of values) { const cell = document.createElement("td"); cell.textContent = String(value); row.appendChild(cell); }
    const cell = document.createElement("td"); const button = document.createElement("button"); button.className = "secondary"; button.textContent = account.status === "active" ? "\u505c\u7528" : "\u542f\u7528";
    button.addEventListener("click", async () => {
      const next = account.status === "active" ? "disabled" : "active";
      if (!window.confirm("\u786e\u5b9a\u8981" + button.textContent + "\u8fd9\u4e2a\u8d26\u6237\u5417\uff1f")) return;
      button.disabled = true;
      try { await call("/admin/api/accounts/" + account.id + "/status", { method: "PATCH", body: JSON.stringify({ status: next }) }); await refresh(); show("\u8d26\u6237\u72b6\u6001\u5df2\u66f4\u65b0"); }
      catch (error) { show(error.message); button.disabled = false; }
    });
    cell.appendChild(button); row.appendChild(cell); tbody.appendChild(row);
  }
}
el("login-form").addEventListener("submit", async event => {
  event.preventDefault(); token = el("token").value.trim(); el("token").value = "";
  try { await refresh(); el("login").hidden = true; el("panel").hidden = false; el("forget").hidden = false; show(""); }
  catch (error) { token = ""; window.alert(error.message); }
});
el("forget").addEventListener("click", () => { token = ""; el("panel").hidden = true; el("login").hidden = false; el("forget").hidden = true; el("issued").textContent = ""; el("issued").hidden = true; });
el("issue-form").addEventListener("submit", async event => {
  event.preventDefault(); const button = el("issue-button"); button.disabled = true; el("issued").hidden = true;
  try {
    const result = await call("/admin/api/codes", { method: "POST", body: JSON.stringify({ transcribe_minutes: Number(el("minutes").value), llm_points: el("points").value, count: Number(el("count").value) }) });
    el("issued").textContent = result.codes.join(String.fromCharCode(10)); el("issued").hidden = false; await refresh(); show("\u5151\u6362\u7801\u5df2\u751f\u6210\uff0c\u8bf7\u7acb\u5373\u5b89\u5168\u53d1\u653e");
  } catch (error) { show(error.message); } finally { button.disabled = false; }
});
})();
"use strict";
const $ = (id) => document.getElementById(id);
let token = "", selected = "", cursor = 0, epoch = 0, loading = false, posting = false;
let snapshot = null, runs = [], nextHistory = 0;
const seen = new Set();
const statuses = {running: "运行中", awaiting_approval: "等待审批", cancelling: "正在取消", cancelled: "已取消", unverified: "已结束 · 未验证", failed: "执行失败", budget_exceeded: "预算耗尽", interrupted: "运行已中断 · 不会自动续跑"};
const activeStatuses = new Set(["running", "awaiting_approval", "cancelling"]);
const reasons = {turn_limit: "已达到模型交互轮数上限，未收到有效的最终答复。请查看轨迹；需要继续时可调整 max_turns 后发起新任务。", model_call_limit: "已达到模型请求次数上限（包含重试），未收到最终答复。", total_timeout: "已达到本次运行的总时长上限。", input_char_limit: "模型输入超过上下文字符预算。", model_request_timeout: "模型请求超时，有限重试后仍未成功。"};
const eventNames = {run_started: "运行开始", rules_loaded: "规范已加载", plan_updated: "计划变更", memory_updated: "记忆更新", model_requested: "请求模型", model_responded: "模型回复", tool_requested: "调用工具", tool_responded: "工具反馈", model_error: "模型错误", model_retry_scheduled: "请求重试", context_history_limited: "历史裁剪", final_response_requested: "请求最终答复", tool_dispatch_blocked: "已阻止工具调用", cancel_requested: "请求取消", approval_requested: "等待模拟审批", approval_decided: "审批决定", approval_cancelled: "审批已取消", approval_expired: "审批已过期", simulated_action_completed: "模拟操作完成", run_finished: "运行结束"};
function text(id, value) { $(id).textContent = value; }
function pretty(value) { return JSON.stringify(value, null, 2); }
function error(message = "") { text("error", message); $("error").hidden = !message; }
async function api(path, body) {
  const options = {headers: {"X-Harness-Token": token}, cache: "no-store"};
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
  return value;
}
function controls() {
  const active = runs.some((run) => activeStatuses.has(run.status)) || (snapshot && activeStatuses.has(snapshot.status));
  $("start").disabled = posting || active;
  $("demo").disabled = posting || active;
  $("cancel").disabled = posting || !snapshot || !activeStatuses.has(snapshot.status) || snapshot.status === "cancelling";
  const pending = snapshot && snapshot.approval && snapshot.approval.status === "pending" && snapshot.status === "awaiting_approval";
  $("approve").disabled = posting || !pending;
  $("reject").disabled = posting || !pending;
}
function render(value) {
  snapshot = value;
  text("status", statuses[value.status] || value.status);
  text("run-meta", `${value.run_id} · ${value.metadata.workspace || ""} · ${value.metadata.mode || ""}`);
  text("metadata", pretty(value.metadata));
  const config = value.metadata.config || {};
  const usedTurns = value.result?.turns;
  const finalPolicy = value.metadata.final_response_policy === "reserve_last_turn" ? "最后一轮仅用于最终答复。" : "该历史运行未启用收尾轮次预留。";
  text("run-limits", value.metadata.mode === "approval_demo" ? "审批演练，不调用模型。" :
    `模型交互轮数：${usedTurns === undefined ? "待统计" : usedTurns} / ${config.max_turns ?? "未知"}；总时限：${config.total_timeout_seconds ?? "未知"} 秒。${finalPolicy}`);
  const list = $("plan"); list.replaceChildren();
  const plan = value.state.plan || [];
  for (const step of plan) {
    const li = document.createElement("li");
    li.textContent = `[${step.status}] ${step.id} · ${step.goal} — 完成条件：${step.completion_condition}`;
    list.append(li);
  }
  if (!plan.length) { const li = document.createElement("li"); li.textContent = "尚无计划"; list.append(li); }
  text("memory", pretty(value.state.memory || {}));
  const rules = value.rules;
  text("rules-version", rules ? (rules.present ? `AGENTS.md · SHA256 ${rules.sha256}` : "根目录无 AGENTS.md") : "本次尚未加载规范（模拟演练不加载规范）");
  text("rules", rules ? rules.content : "暂无记录");
  text("answer", value.result ? (value.result.answer || reasons[value.result.reason] || `运行结束：${value.result.reason}`) : "等待结果");
  text("verification", value.checks.verified ? "配置的检查已通过" : value.checks.note);
  const artifacts = $("artifacts"); artifacts.replaceChildren();
  for (const artifact of value.artifacts) {
    const li = document.createElement("li");
    li.textContent = `${artifact.path} · ${artifact.bytes} 字节 · SHA256 ${artifact.sha256}`;
    artifacts.append(li);
  }
  if (!value.artifacts.length) { const li = document.createElement("li"); li.textContent = "暂无产物"; artifacts.append(li); }
  $("approval-section").hidden = !value.approval;
  text("approval", value.approval ? pretty(value.approval) : "");
  controls();
}
function appendEvent(event) {
  if (seen.has(event.sequence)) return;
  seen.add(event.sequence);
  const details = document.createElement("details");
  details.dataset.sequence = String(event.sequence);
  const summary = document.createElement("summary");
  const data = event.data;
  let hint = data.name || data.reason || data.code || data.decision || "";
  if (event.type === "model_responded") hint = data.message?.content || "工具调用响应";
  if (event.type === "tool_responded" && data.result) hint += ` · ${data.result.ok ? "成功" : "失败"}${data.result.error ? " · " + data.result.error : ""}`;
  if (event.type === "plan_updated") hint = data.reason || "";
  if (data.display_truncated) hint = "长详情已截断";
  summary.textContent = `#${event.sequence} ${eventNames[event.type] || event.type} · ${String(hint).slice(0, 180)}`;
  const pre = document.createElement("pre");
  const raw = pretty(data);
  pre.textContent = raw.length > 24000 ? raw.slice(0, 24000) + "\n[页面详情已截断，完整内容见本地事件日志]" : raw;
  details.append(summary, pre); $("events").append(details);
  text("event-count", `${seen.size} 条事件`);
}
async function loadHistory() {
  const value = await api("/api/runs"); runs = value.runs;
  const list = $("history"); list.replaceChildren();
  const empty = document.createElement("option"); empty.value = ""; empty.textContent = "请选择运行"; list.append(empty);
  for (const run of runs) {
    const option = document.createElement("option"); option.value = run.run_id;
    option.textContent = `${statuses[run.status] || run.status} · ${run.task.slice(0, 36)} · ${run.run_id.slice(0, 8)}`;
    list.append(option);
  }
  list.value = selected; controls();
}
async function poll() {
  if (loading) return;
  loading = true;
  const generation = epoch, run = selected;
  try {
    if (Date.now() >= nextHistory) { await loadHistory(); nextHistory = Date.now() + 3000; }
    if (!run) return;
    const value = await api(`/api/runs/${run}`);
    if (generation !== epoch) return;
    render(value);
    let more = true;
    while (more && generation === epoch) {
      const page = await api(`/api/runs/${run}/events?after=${cursor}`);
      if (generation !== epoch) return;
      for (const event of page.events) appendEvent(event);
      cursor = page.cursor; more = page.has_more;
    }
    error();
  } catch (e) { if (generation === epoch) error(`读取失败：${e.message}。服务可用后会继续刷新；不会重新提交任务。`); }
  finally { loading = false; }
}
function select(run) {
  epoch++; selected = run; cursor = 0; snapshot = null; seen.clear();
  $("events").replaceChildren(); text("event-count", "0 条事件");
  localStorage.setItem("harness.run", run);
  window.history.replaceState(null, "", run ? `#${run}` : location.pathname);
  $("history").value = run;
  error(); controls(); void poll();
}
async function submit(mode) {
  if (posting) return;
  posting = true; controls(); error();
  try {
    const value = await api("/api/runs", {task: mode === "approval_demo" ? "模拟审批演练" : $("task").value,
      workspace: $("workspace").value, mode});
    select(value.run_id); nextHistory = 0;
  } catch (e) { error(`提交失败：${e.message}。可查看运行记录确认是否已启动。`); }
  finally { posting = false; controls(); }
}
async function action(kind) {
  if (!selected || posting) return;
  posting = true; controls(); error();
  try {
    if (kind === "cancel") await api(`/api/runs/${selected}/cancel`, {});
    else await api(`/api/runs/${selected}/approvals/${snapshot.approval.id}`, {decision: kind});
    nextHistory = 0;
  } catch (e) { error(`操作未接受：${e.message}。请查看最新状态。`); }
  finally { posting = false; controls(); await poll(); }
}
$("task-form").addEventListener("submit", (event) => { event.preventDefault(); void submit("model"); });
$("demo").addEventListener("click", () => void submit("approval_demo"));
$("cancel").addEventListener("click", () => void action("cancel"));
$("approve").addEventListener("click", () => void action("approve"));
$("reject").addEventListener("click", () => void action("reject"));
$("history").addEventListener("change", () => select($("history").value));
async function boot() {
  try {
    const session = await api("/api/session"); token = session.token;
    $("workspace").value = session.default_workspace;
    const run = location.hash.slice(1) || localStorage.getItem("harness.run") || "";
    await loadHistory();
    select(/^[0-9a-f]{32}$/.test(run) ? run : (runs.find((r) => activeStatuses.has(r.status))?.run_id || ""));
    setInterval(() => void poll(), 700);
  } catch (e) { error(`连接服务失败：${e.message}。请确认服务已启动并刷新页面。`); }
}
void boot();

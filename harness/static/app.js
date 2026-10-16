"use strict";
const $ = (id) => document.getElementById(id);
const MAX_VISIBLE_EVENTS = 300;
let token = "", selected = "", cursor = 0, epoch = 0, loading = false, posting = false;
let snapshot = null, runs = [], nextHistory = 0, activeTab = "answer";
let lastNetworkKey = "";
let displayedLogs = 0, observedTurn = 0, lastAnswerKey = "", lastPlanKey = "", lastHistoryKey = "", prefilledRun = "", formDirty = false;
const seen = new Set();
const statuses = {running: "运行中", verifying: "正在检查", awaiting_approval: "等待审批", cancelling: "正在取消", cancelled: "已取消", completed: "指定检查通过", verification_failed: "检查失败", unverified: "已结束 · 未验证", failed: "执行失败", budget_exceeded: "预算耗尽", interrupted: "运行已中断"};
const activeStatuses = new Set(["running", "verifying", "awaiting_approval", "cancelling"]);
const reasons = {configured_checks_failed: "配置检查未全部通过，详见检查证据。", repeated_tool_failure: "连续相同工具失败达到上限，已停止派发。", tool_call_limit: "工具调用总预算已耗尽。", process_termination_unconfirmed: "无法确认进程树已结束；已停止后续操作，请检查残留进程。", turn_limit: "已达到模型交互轮数上限，未收到有效的最终答复。请查看执行轨迹；需要继续时可调整 max_turns 后发起新任务。", model_call_limit: "已达到模型请求次数上限（包含重试），未收到最终答复。", total_timeout: "已达到本次运行的总时长上限。", input_char_limit: "模型输入超过上下文字符预算。", model_request_timeout: "模型请求超时，有限重试后仍未成功。"};
const eventNames = {verification_started: "开始检查", verification_finished: "检查结束", check_started: "执行检查项", check_finished: "检查项结果", model_repair_requested: "请求修正模型格式", run_started: "运行开始", rules_loaded: "规范已加载", plan_updated: "计划变更", memory_updated: "记忆更新", model_requested: "请求模型", model_responded: "模型回复", tool_requested: "调用工具", tool_responded: "工具反馈", model_error: "模型错误", model_retry_scheduled: "请求重试", context_history_limited: "历史裁剪", final_response_requested: "请求最终答复", tool_dispatch_blocked: "已阻止工具调用", cancel_requested: "请求取消", approval_requested: "等待审批", approval_decided: "审批决定", approval_cancelled: "审批已取消", approval_expired: "审批已过期", simulated_action_completed: "模拟操作完成", run_finished: "运行结束"};
Object.assign(eventNames, {network_request: "发送网络请求", network_response: "收到网络响应", network_error: "网络错误",
  network_retry_scheduled: "网络请求重试", network_operation: "联网工具结束"});
function text(id, value) { if ($(id).textContent !== String(value ?? "")) $(id).textContent = value ?? ""; }
function pretty(value) { return JSON.stringify(value, null, 2); }
function node(tag, className, content) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (content !== undefined) element.textContent = content;
  return element;
}
function error(message = "") { text("error", message); $("error").hidden = !message; }
function dateText(value, timeOnly = false) {
  if (!value || !Number.isFinite(new Date(value).getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", timeOnly ? {hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false} :
    {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false}).format(new Date(value));
}
function duration(seconds) {
  if (!Number.isFinite(seconds)) return "—";
  const value = Math.max(0, Math.round(seconds));
  return value < 60 ? value + " 秒" : Math.floor(value / 60) + " 分 " + value % 60 + " 秒";
}
function clipped(value, length = 160) { const s = String(value ?? ""); return s.length > length ? s.slice(0, length) + "…" : s; }
async function api(path, body) {
  const options = {headers: {"X-Harness-Token": token}, cache: "no-store"};
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "HTTP " + response.status);
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
  $("copy-answer").disabled = !snapshot?.result?.answer;
}
function setTab(name, focus = false) {
  if (!["answer", "files", "trace", "tools", "context"].includes(name)) return;
  activeTab = name;
  for (const key of ["answer", "files", "trace", "tools", "context"]) {
    $("tab-" + key).setAttribute("aria-selected", String(key === name));
    $("tab-" + key).tabIndex = key === name ? 0 : -1;
    $("panel-" + key).hidden = key !== name;
  }
  if (focus) $("tab-" + name).focus();
}
function inlineContent(target, value) {
  const tick = String.fromCharCode(96);
  const tokens = String(value).split(new RegExp("(\\[[^\\]\\n]+\\]\\(https?://[^\\s)]+\\)|\\*\\*[^*]+\\*\\*|" + tick + "[^" + tick + "]+" + tick + ")", "g"));
  for (const part of tokens) {
    const link = part.match(/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/);
    if (link) target.append(sourceLink(link[1], link[2]));
    else if (part.startsWith("**") && part.endsWith("**") && part.length > 4) target.append(node("strong", "", part.slice(2, -2)));
    else if (part.startsWith(tick) && part.endsWith(tick) && part.length > 2) target.append(node("code", "", part.slice(1, -1)));
    else target.append(document.createTextNode(part));
  }
}
function sourceLink(label, url) {
  try {
    const parsed = new URL(url);
    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password || /[\s\\\u0000-\u001f]/.test(url)) throw new Error();
    const link = node("a", "", label);
    link.href = parsed.href; link.target = "_blank"; link.rel = "noopener noreferrer";
    return link;
  } catch { return node("span", "", label + " · 无效来源链接"); }
}
function renderSources(network, citations) {
  const key = pretty({network, citations});
  if (key === lastNetworkKey) return;
  lastNetworkKey = key;
  const target = $("web-sources"); target.replaceChildren();
  target.append(node("h3", "", "网络来源"));
  target.append(node("p", "muted", network?.available ? "联网已启用 · 搜索请求 " + (network.requests?.search || 0) + " · 正文提取 " + (network.requests?.extract || 0) :
    network?.unavailable_reason === "network_key_missing" ? "联网不可用：缺少 Tavily 密钥" : "本次运行未启用联网"));
  for (const source of network?.sources || []) {
    const details = node("details", "context-details"), summary = node("summary");
    summary.append(document.createTextNode(source.source_id + " · "), sourceLink(source.title || source.url, source.url));
    details.append(summary, node("p", "muted", "记录时间：" + dateText(source.fetched_at || source.observed_at)));
    if (source.document_id) {
      details.append(node("p", "", "已保存提取正文 " + source.stored_chars + " 字符；已读范围：" +
        (source.read_ranges || []).map((range) => range.join("–")).join("、") + "。" +
        (source.saved_body_fully_read ? "已读完保存的正文。" : "尚未读完保存的正文。") +
        (source.storage_truncated ? "正文超过存储上限，保存内容已截断。" : "") + "无法保证提取结果等于原网页完整内容。"));
    } else details.append(node("p", "", "仅取得搜索摘要，尚未读取正文。"));
    const raw = node("details"); raw.append(node("summary", "", "来源记录详情"), node("pre", "", pretty(source)));
    details.append(raw);
    target.append(details);
  }
  if (citations?.unknown?.length) target.append(node("p", "", "回答包含未知来源编号：" + citations.unknown.join("、")));
}
function renderAnswer(value) {
  const target = $("answer");
  target.replaceChildren();
  const tick = String.fromCharCode(96), fence = tick.repeat(3);
  if (!/(^#{1,6} |^\s*[-*]\s|^\s*\d+\.\s|\*\*|^\||\[[^\]]+\]\(https?:\/\/)/m.test(value) && !value.includes(tick)) {
    target.textContent = value; target.className = "answer-content plain-line"; return;
  }
  target.className = "answer-content";
  const lines = value.replace(/\r\n/g, "\n").split("\n");
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index++; continue; }
    if (line.trimStart().startsWith(fence)) {
      const content = []; index++;
      while (index < lines.length && !lines[index].trimStart().startsWith(fence)) content.push(lines[index++]);
      if (index < lines.length) index++;
      const pre = node("pre"); pre.append(node("code", "", content.join("\n"))); target.append(pre); continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const h = node("h" + Math.min(heading[1].length, 3)); inlineContent(h, heading[2]); target.append(h); index++; continue;
    }
    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { target.append(node("hr")); index++; continue; }
    if (line.trim().startsWith("|") && index + 1 < lines.length && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[index + 1])) {
      const table = node("table"), head = node("thead"), body = node("tbody");
      const cells = (s) => s.trim().replace(/^\||\|$/g, "").split("|");
      const row = node("tr");
      for (const cell of cells(line)) { const th = node("th"); inlineContent(th, cell.trim()); row.append(th); }
      head.append(row); index += 2;
      while (index < lines.length && lines[index].trim().startsWith("|")) {
        const tr = node("tr");
        for (const cell of cells(lines[index++])) { const td = node("td"); inlineContent(td, cell.trim()); tr.append(td); }
        body.append(tr);
      }
      table.append(head, body); const scroll = node("div", "table-scroll"); scroll.append(table); target.append(scroll); continue;
    }
    const bullet = line.match(/^\s*(?:[-*]\s+|\d+\.\s+)(.+)$/);
    if (bullet) {
      const ordered = /^\s*\d+\./.test(line), list = node(ordered ? "ol" : "ul");
      while (index < lines.length) {
        const item = lines[index].match(ordered ? /^\s*\d+\.\s+(.+)$/ : /^\s*[-*]\s+(.+)$/);
        if (!item) break;
        const li = node("li"); inlineContent(li, item[1]); list.append(li); index++;
      }
      target.append(list); continue;
    }
    if (line.startsWith("> ")) {
      const quote = node("blockquote"); inlineContent(quote, line.slice(2)); target.append(quote); index++; continue;
    }
    const p = node("p", "plain-line"); inlineContent(p, line); target.append(p); index++;
  }
}
function emptyAnswer(title, description) {
  $("answer").replaceChildren();
  $("answer").className = "answer-content";
  const empty = node("div", "answer-empty");
  empty.append(node("span", "empty-orbit", "⌁"), node("h2", "", title), node("p", "", description));
  $("answer").append(empty);
}
function render(value) {
  snapshot = value;
  const metadata = value.metadata || {}, result = value.result, config = metadata.config || {};
  renderSources(value.network || result?.network, result?.citation_check);
  text("status", statuses[value.status] || value.status);
  text("overview-badge", value.status === "unverified" ? "未验证" : statuses[value.status] || value.status);
  $("overview-badge").dataset.status = value.status;
  $("status-symbol").dataset.state = value.status;
  text("status-symbol", value.status === "completed" ? "✓" : ["failed", "verification_failed", "budget_exceeded"].includes(value.status) ? "!" : activeStatuses.has(value.status) ? "◌" : "○");
  text("result-subtitle", metadata.task || "从一个目标开始，让每次执行都有迹可循");
  $("result-subtitle").title = metadata.task || "";
  text("run-meta", displayRunId(value.run_id)); $("run-meta").title = value.run_id;
  text("metadata", pretty(metadata));
  const seconds = result?.elapsed_seconds ?? (metadata.started_at ? (Date.now() - new Date(metadata.started_at).getTime()) / 1000 : undefined);
  const plan = value.state?.plan || [], completed = plan.filter((step) => step.status === "completed").length;
  text("result-duration", "耗时 " + duration(seconds) + (plan.length ? " · " + completed + "/" + plan.length + " 步" : ""));
  text("started-at", dateText(metadata.started_at)); text("finished-at", dateText(result?.finished_at)); text("elapsed", duration(seconds));
  const usedTurns = result?.turns ?? observedTurn;
  text("turn-progress", usedTurns + " / " + (config.max_turns ?? "—"));
  text("overview-workspace", metadata.workspace || "—"); $("overview-workspace").title = metadata.workspace || "";
  text("overview-model", config.model || metadata.mode || "—"); $("overview-model").title = config.model || "";
  const finalPolicy = metadata.final_response_policy === "reserve_last_turn" ? "最后一轮仅用于最终答复。" : "该历史运行未启用收尾轮次预留。";
  text("run-limits", metadata.mode === "approval_demo" ? "审批演练，不调用模型。" :
    "模型交互轮数：" + (result?.turns ?? "待统计") + " / " + (config.max_turns ?? "未知") + "；单次请求时限：" + (config.request_timeout_seconds ?? "未知") + " 秒；总时限：" + (config.total_timeout_seconds ?? "未知") + " 秒。" + finalPolicy);
  text("plan-progress", plan.length ? completed + " / " + plan.length + " 已完成" : "无需或尚未规划");
  $("plan-progress-bar").value = plan.length ? completed / plan.length * 100 : 0;
  const planKey = value.run_id + pretty(plan);
  if (planKey !== lastPlanKey) {
    lastPlanKey = planKey; const list = $("plan"); list.replaceChildren();
    for (const [index, step] of plan.entries()) {
      const li = node("li", "plan-step"); li.dataset.status = step.status;
      const info = node("div");
      info.append(node("div", "step-title", step.goal), node("p", "step-condition", step.completion_condition));
      li.append(node("span", "step-number", step.status === "completed" ? "✓" : String(index + 1)), info);
      li.title = ({"completed": "已完成", "in_progress": "进行中", "pending": "待开始", "blocked": "已阻塞"}[step.status] || step.status) + "：" + step.goal + "\n完成条件：" + step.completion_condition;
      list.append(li);
    }
    if (!plan.length) list.append(node("li", "empty-note", "尚无计划，简单问答可直接回答"));
  }
  text("memory", pretty(value.state?.memory || {}));
  const rules = value.rules;
  text("rules-version", rules ? (rules.present ? "版本 SHA256 " + rules.sha256 : "根目录无 AGENTS.md") : "本次尚未加载规范");
  text("rules", rules ? rules.content : "暂无记录");
  const answer = result ? (result.answer || reasons[result.reason] || "运行结束：" + result.reason) : null;
  const answerKey = value.run_id + value.status + (answer || "");
  if (answerKey !== lastAnswerKey) {
    lastAnswerKey = answerKey;
    if (answer) renderAnswer(answer);
    else emptyAnswer(value.status === "awaiting_approval" ? "等待你的决定" : "任务正在执行", "可以在右侧日志窗口查看最新进展，最终回答将在这里呈现。");
  }
  text("answer-label", result ? "本次任务的最终回复" : "等待任务结果");
  text("verification", value.checks.verified ? "仅所配置检查项通过；其他需求仍需核对。" : value.checks.note);
  const report = $("completion-report"); report.replaceChildren();
  if (result && (result.checks || result.operations)) {
    const details = node("details", "context-details");
    details.append(node("summary", "", "检查证据、操作与未完成事项"), node("pre", "", pretty({
      status: statuses[value.status] || value.status, reason: result.reason,
      checks: result.checks || [], operations: result.operations || [], artifacts: result.artifacts || [],
      unfinished_steps: result.unfinished_steps || [], unresolved: result.unresolved || [], scope: result.completion_scope,
    })));
    report.append(details);
  }
  const artifacts = $("artifacts"); artifacts.replaceChildren();
  for (const artifact of value.artifacts) {
    const li = node("li");
    li.append(node("strong", "", artifact.path), node("span", "", artifact.bytes + " 字节"), node("small", "", "SHA256 " + artifact.sha256));
    artifacts.append(li);
  }
  if (!value.artifacts.length) artifacts.append(node("li", "empty-note", "本次运行没有生成文件，问答类任务可直接返回回答。"));
  text("file-count", value.artifacts.length);
  $("approval-section").hidden = !value.approval;
  text("approval", value.approval ? pretty(value.approval) : "");
  text("approve", value.approval?.type === "shell" ? "批准执行此命令" : "批准模拟操作");
  if (prefilledRun !== value.run_id && !formDirty) {
    $("workspace").value = metadata.workspace || $("workspace").value;
    $("task").value = metadata.task || "";
    text("task-count", $("task").value.length + " 字"); prefilledRun = value.run_id;
  }
  controls();
}
function eventHint(event) {
  const data = event.data;
  let hint = data.name || data.reason || data.code || data.decision || "";
  if (event.type === "model_responded") hint = data.message?.content || "工具调用响应";
  if (event.type === "tool_requested") {
    try { const args = JSON.parse(data.arguments); hint += " · " + (args.path || args.query || args.url || args.document_id || ""); } catch {}
  }
  if (event.type === "tool_responded" && data.result) hint += " · " + (data.result.ok ? "成功" : "失败") + (data.result.error ? " · " + data.result.error : "");
  if (event.type === "network_operation") hint = data.name + " · " + data.elapsed_seconds + " 秒 · " + (data.error || "成功");
  if (event.type === "plan_updated") hint = data.reason || "";
  if (event.type === "model_retry_scheduled") hint = data.delay_seconds + " 秒后重试";
  if (event.type === "context_history_limited") hint = "保留近期交互，省略 " + data.omitted_turns + " 轮旧历史";
  if (data.display_truncated) hint = "长详情已截断";
  return clipped(hint);
}
function eventDetails(event) {
  const details = node("details"); details.dataset.sequence = String(event.sequence);
  const summary = node("summary", "", "#" + event.sequence + " " + (eventNames[event.type] || event.type) + " · " + eventHint(event));
  const raw = pretty(event.data);
  details.append(summary, node("pre", "", raw.length > 24000 ? raw.slice(0, 24000) + "\n[页面详情已截断，完整内容见本地事件日志]" : raw));
  return details;
}
function appendBounded(container, child) {
  container.append(child);
  while (container.children.length > MAX_VISIBLE_EVENTS) container.removeChild(container.firstElementChild);
}
function appendEvent(event) {
  if (seen.has(event.sequence)) return;
  seen.add(event.sequence);
  appendBounded($("events"), eventDetails(event));
  if (["tool_requested", "tool_responded", "tool_dispatch_blocked"].includes(event.type)) {
    appendBounded($("tool-events"), eventDetails(event)); $("tools-empty").hidden = true;
  }
  if (event.type === "model_requested") observedTurn = Math.max(observedTurn, event.data.turn || 0);
  if (!displayedLogs) $("logs").replaceChildren();
  let level = event.type === "plan_updated" ? "STEP" : event.type.startsWith("tool_") ? "TOOL" : "INFO";
  if (event.type === "model_error" || event.data.result?.ok === false) level = "ERROR";
  else if (["context_history_limited", "model_retry_scheduled", "tool_dispatch_blocked", "approval_expired"].includes(event.type)) level = "WARN";
  else if (event.type === "run_finished") level = ["failed", "verification_failed", "budget_exceeded"].includes(event.data.status) ? "WARN" : "END";
  const row = node("div", "log-row"); row.dataset.sequence = String(event.sequence);
  const tag = node("span", "log-level", level); tag.dataset.level = level;
  let logHint = eventHint(event);
  if (event.type === "model_responded") logHint = event.data.message?.content ? "收到文本回复 · " + event.data.message.content.length + " 字" : "模型选择工具";
  if (event.type === "model_requested") logHint = "第 " + (event.data.turn || "—") + " 轮交互";
  if (event.type === "plan_updated") logHint = "已保存步骤与完成条件";
  if (event.type === "run_finished") logHint = statuses[event.data.status] || event.data.reason || "";
  row.append(node("span", "log-time", dateText(event.timestamp, true)), tag,
    node("span", "log-message", (eventNames[event.type] || event.type) + (logHint ? " · " + clipped(logHint, 110) : "")));
  appendBounded($("logs"), row);
  displayedLogs = Math.min(displayedLogs + 1, MAX_VISIBLE_EVENTS);
  text("event-count", seen.size + " 条事件");
  text("log-count", displayedLogs + " 条日志" + (seen.size > displayedLogs ? " · 全部 " + seen.size + " 条" : ""));
  if ($("follow-logs").checked) $("logs").scrollTop = $("logs").scrollHeight;
}
function clearLogs() {
  $("logs").replaceChildren(node("p", "console-empty", "显示已清空，等待新事件。完整轨迹仍保留。"));
  displayedLogs = 0; text("log-count", "显示已清空 · 全部 " + seen.size + " 条");
}
function validRunId(value) {
  return /^(?:[0-9]{8}[0-9]{3,}|[0-9a-f]{32})$/.test(value);
}
function displayRunId(value) {
  return value.length <= 16 ? value : value.slice(0, 8) + "…" + value.slice(-6);
}
function renderHistory() {
  const historyKey = pretty(runs) + selected;
  if (historyKey === lastHistoryKey) return;
  lastHistoryKey = historyKey;
  const list = $("history"); list.replaceChildren();
  const empty = node("option", "", "选择历史运行"); empty.value = ""; list.append(empty);
  for (const run of runs) {
    const option = node("option", "", displayRunId(run.run_id) + " · " + (statuses[run.status] || run.status) + " · " + run.task.slice(0, 32));
    option.value = run.run_id; list.append(option);
  }
  list.value = selected;
  const recent = $("recent-runs"); recent.replaceChildren();
  for (const run of runs.slice(0, 8)) {
    const button = node("button", "recent-run"); button.type = "button"; button.setAttribute("aria-current", String(run.run_id === selected));
    const detail = node("span", "recent-run-text");
    detail.append(node("strong", "", run.task || "未命名任务"), node("small", "", (statuses[run.status] || run.status) + " · " + displayRunId(run.run_id)));
    const state = node("span", "recent-state"); state.dataset.state = run.status;
    button.append(node("span", "recent-run-icon", "▤"), detail, state);
    button.addEventListener("click", () => select(run.run_id)); recent.append(button);
  }
  if (!runs.length) recent.append(node("p", "empty-note", "任务开始后会保存在这里"));
  text("history-count", runs.length + " 项");
}
async function loadHistory() {
  const value = await api("/api/runs"); runs = value.runs; renderHistory(); controls();
}
async function poll() {
  if (loading) return;
  loading = true;
  const generation = epoch, run = selected;
  try {
    if (Date.now() >= nextHistory) { await loadHistory(); nextHistory = Date.now() + 3000; }
    if (!run) return;
    const value = await api("/api/runs/" + run);
    if (generation !== epoch) return;
    render(value);
    let more = true;
    while (more && generation === epoch) {
      const page = await api("/api/runs/" + run + "/events?after=" + cursor);
      if (generation !== epoch) return;
      for (const event of page.events) appendEvent(event);
      cursor = page.cursor; more = page.has_more;
    }
    error();
  } catch (e) { if (generation === epoch) error("读取失败：" + e.message + "。请检查服务状态或刷新页面；不会自动重新提交任务。"); }
  finally { loading = false; }
}
function select(run) {
  epoch++; selected = run; cursor = 0; snapshot = null; seen.clear();
  observedTurn = 0; displayedLogs = 0; lastAnswerKey = ""; lastPlanKey = "";
  $("events").replaceChildren(); $("tool-events").replaceChildren(); $("tools-empty").hidden = false;
  $("logs").replaceChildren(node("p", "console-empty", "等待任务事件…"));
  text("event-count", "0 条事件"); text("log-count", "0 条日志");
  for (const id of ["started-at", "finished-at", "elapsed", "turn-progress", "overview-workspace", "overview-model", "run-meta"]) { text(id, "—"); $(id).title = ""; }
  text("status", run ? "读取运行记录…" : "准备就绪"); text("overview-badge", "待运行");
  $("overview-badge").dataset.status = "idle"; $("status-symbol").dataset.state = "idle"; text("status-symbol", "○");
  text("result-subtitle", "从一个目标开始，让每次执行都有迹可循"); text("result-duration", "等待开始新的任务");
  $("result-subtitle").title = ""; text("verification", "回答与模型自检会如实记录；未配置通用结果验证器。");
  text("plan-progress", "等待规划"); $("plan-progress-bar").value = 0;
  $("plan").replaceChildren(node("li", "empty-note", "尚无计划，简单问答可直接回答"));
  $("artifacts").replaceChildren(node("li", "empty-note", "暂无产物")); text("file-count", "0");
  text("rules-version", "尚未加载"); text("rules", "暂无记录"); text("memory", "{}"); text("metadata", "暂无记录"); text("run-limits", "");
  $("approval-section").hidden = true;
  $("completion-report").replaceChildren();
  emptyAnswer(run ? "正在读取运行记录" : "让想法成为结果", run ? "正在恢复本次回答与执行轨迹。" : "在左侧描述你的任务，回答、产物与执行过程会在这里呈现。");
  localStorage.setItem("harness.run", run);
  window.history.replaceState(null, "", run ? "#" + run : location.pathname);
  $("history").value = run;
  renderHistory(); setTab("answer");
  error(); controls(); void poll();
}
async function submit(mode) {
  if (posting) return;
  posting = true; controls(); error();
  try {
    const value = await api("/api/runs", {task: mode === "approval_demo" ? "模拟审批演练" : $("task").value,
      workspace: $("workspace").value, mode});
    select(value.run_id); nextHistory = 0;
  } catch (e) { error("提交失败：" + e.message + "。可查看运行记录确认是否已启动。"); }
  finally { posting = false; controls(); }
}
async function action(kind) {
  if (!selected || posting) return;
  posting = true; controls(); error();
  try {
    if (kind === "cancel") await api("/api/runs/" + selected + "/cancel", {});
    else await api("/api/runs/" + selected + "/approvals/" + snapshot.approval.id, {decision: kind});
    nextHistory = 0;
  } catch (e) { error("操作未接受：" + e.message + "。请查看最新状态。"); }
  finally { posting = false; controls(); await poll(); }
}
$("task-form").addEventListener("submit", (event) => { event.preventDefault(); void submit("model"); });
$("demo").addEventListener("click", () => void submit("approval_demo"));
$("cancel").addEventListener("click", () => void action("cancel"));
$("approve").addEventListener("click", () => void action("approve"));
$("reject").addEventListener("click", () => void action("reject"));
$("history").addEventListener("change", () => select($("history").value));
$("task").addEventListener("input", () => { formDirty = true; text("task-count", $("task").value.length + " 字"); });
$("workspace").addEventListener("input", () => { formDirty = true; });
$("clear-logs").addEventListener("click", clearLogs);
$("open-trace").addEventListener("click", () => setTab("trace", true));
$("logs").addEventListener("keydown", (event) => {
  if (event.key === "Home" || event.key === "End") {
    event.preventDefault();
    $("follow-logs").checked = event.key === "End";
    $("logs").scrollTop = event.key === "End" ? $("logs").scrollHeight : 0;
  }
});
$("logs").addEventListener("scroll", () => {
  if ($("logs").scrollHeight - $("logs").clientHeight - $("logs").scrollTop > 30) $("follow-logs").checked = false;
});
for (const name of ["answer", "files", "trace", "tools", "context"]) {
  $("tab-" + name).addEventListener("click", () => setTab(name));
  $("tab-" + name).addEventListener("keydown", (event) => {
    const tabs = ["answer", "files", "trace", "tools", "context"];
    let target = null;
    if (event.key === "ArrowRight") target = tabs[(tabs.indexOf(name) + 1) % tabs.length];
    if (event.key === "ArrowLeft") target = tabs[(tabs.indexOf(name) + tabs.length - 1) % tabs.length];
    if (event.key === "Home") target = tabs[0];
    if (event.key === "End") target = tabs[tabs.length - 1];
    if (target) { event.preventDefault(); setTab(target, true); }
  });
}
$("copy-answer").addEventListener("click", async () => {
  if (!snapshot?.result?.answer) return;
  try { await navigator.clipboard.writeText(snapshot.result.answer); text("copy-answer", "已复制"); }
  catch { text("copy-answer", "复制失败"); }
  setTimeout(() => text("copy-answer", "▢ 复制"), 1600);
});
async function boot() {
  try {
    const session = await api("/api/session"); token = session.token;
    $("workspace").value = session.default_workspace;
    text("network-notice", session.network_available ? "联网已启用：搜索词和待读网址会发送给 Tavily，模型可在预算内自动调用。" :
      session.network_enabled ? "联网不可用：尚未配置 Tavily 密钥。" : "联网默认关闭；启用后搜索词和待读网址会发送给 Tavily。");
    const run = location.hash.slice(1) || localStorage.getItem("harness.run") || "";
    await loadHistory();
    select(validRunId(run) ? run : (runs.find((r) => activeStatuses.has(r.status))?.run_id || ""));
    setInterval(() => void poll(), 700);
  } catch (e) { error("连接服务失败：" + e.message + "。请确认服务已启动并刷新页面。"); }
}
void boot();

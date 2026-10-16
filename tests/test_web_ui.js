"use strict";
// Run with node tests/test_web_ui.js. No browser, packages or project server needed.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
class Element {
  constructor(tag = "div") { this.tagName = tag; this.children = []; this.dataset = {}; this._text = ""; this.attributes = new Map(); this.listeners = new Map(); }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  set textContent(value) { this._text = String(value ?? ""); this.children = []; }
  get firstElementChild() { return this.children[0] || null; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this._text = ""; this.children = nodes; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
  addEventListener(type, callback) { this.listeners.set(type, callback); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name); }
  focus() { this.focused = true; }
}
const elements = new Map();
const element = (id) => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
const context = vm.createContext({
  document: {getElementById: element, createElement: (tag) => new Element(tag), createTextNode: (text) => { const node = new Element("#text"); node.textContent = text; return node; }},
  // Leave startup pending so these checks exercise rendering without HTTP or timers.
  fetch: () => new Promise(() => {}),
  localStorage: {setItem() {}},
  window: {history: {replaceState() {}}},
  location: {pathname: "/"},
});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../harness/static/app.js"), "utf8"), context);
const value = {
  run_id: "historical", status: "budget_exceeded", metadata: {mode: "openai_compatible",
    config: {max_turns: 8, request_timeout_seconds: 30, total_timeout_seconds: 120}},
  state: {plan: []}, rules: null, artifacts: [], checks: {verified: false, note: "unverified"},
  result: {answer: null, reason: "turn_limit", turns: 8}, approval: null,
};
context.render(value);
assert.match(element("answer").textContent, /轮数上限/);
for (const id of ["20261015001", "202610151000", "a".repeat(32)]) assert(context.validRunId(id));
for (const id of ["2026101501", "../20261015001", "20261015001/other"]) assert(!context.validRunId(id));
value.run_id = "20261015001";
context.render(value);
assert.equal(element("run-meta").textContent, "20261015001");
vm.runInContext('runs = [{run_id: "20261015001", status: "unverified", task: "task"}]; renderHistory();', context);
assert.match(element("recent-runs").textContent, /20261015001/);
assert.match(element("history").textContent, /20261015001/);
assert.match(element("run-limits").textContent, /8 \/ 8/);
assert.match(element("run-limits").textContent, /单次请求时限：30 秒/);
assert.match(element("run-limits").textContent, /总时限：120 秒/);
assert.match(element("run-limits").textContent, /历史运行未启用/);
value.metadata.final_response_policy = "reserve_last_turn";
value.status = "unverified";
value.result = {answer: "Agent 是智能体。", reason: "model_finished_without_verifier", turns: 1};
context.render(value);
assert.equal(element("answer").textContent, "Agent 是智能体。");
assert.match(element("run-limits").textContent, /最后一轮仅用于最终答复/);
const event = {sequence: 1, type: "final_response_requested", data: {turn: 8}};
context.appendEvent(event);
context.appendEvent(event);
assert.equal(element("events").children.length, 1);
assert.equal(element("event-count").textContent, "1 条事件");
context.setTab("trace");
assert.equal(element("panel-trace").hidden, false);
assert.equal(element("panel-answer").hidden, true);
assert.equal(element("tab-trace").getAttribute("aria-selected"), "true");
let prevented = false;
element("tab-trace").listeners.get("keydown")({key: "ArrowRight", preventDefault() { prevented = true; }});
assert.equal(prevented, true);
assert.equal(element("panel-tools").hidden, false);
context.renderAnswer("# Agent 是什么？\n\n**加粗**与 `code`\n\n<script>bad()</script>\n\n- 文件操作\n- 工具反馈");
const descendants = (target) => [target, ...target.children.flatMap(descendants)];
assert(descendants(element("answer")).some((el) => el.tagName === "h1"));
assert(descendants(element("answer")).some((el) => el.tagName === "strong"));
assert(descendants(element("answer")).some((el) => el.tagName === "code"));
assert(!descendants(element("answer")).some((el) => ["script", "img"].includes(el.tagName)));
assert.match(element("answer").textContent, /<script>bad\(\)<\/script>/);
element("follow-logs").checked = true;
element("logs").scrollHeight = 1234;
for (let sequence = 2; sequence <= 501; sequence++) {
  context.appendEvent({sequence, type: "tool_requested", data: {name: "read_file", arguments: '{"path":"input.txt"}'}});
}
assert.equal(element("events").children.length, 300);
assert.equal(element("tool-events").children.length, 300);
assert.equal(element("logs").children.length, 300);
assert.equal(element("logs").scrollTop, 1234);
assert.equal(element("event-count").textContent, "501 条事件");
context.clearLogs();
assert.equal(element("events").children.length, 300);
assert.match(element("log-count").textContent, /显示已清空/);
context.appendEvent(event);
assert.equal(element("logs").children.length, 1); // Duplicate does not repopulate cleared logs.
element("follow-logs").checked = false;
element("logs").scrollTop = 50;
context.appendEvent({sequence: 502, type: "model_requested", data: {turn: 2}});
assert.equal(element("logs").children.length, 1);
assert.equal(element("logs").scrollTop, 50);
assert.equal(element("event-count").textContent, "502 条事件");
element("logs").listeners.get("keydown")({key: "Home", preventDefault() {}});
assert.equal(element("logs").scrollTop, 0);
assert.equal(element("follow-logs").checked, false);
element("logs").listeners.get("keydown")({key: "End", preventDefault() {}});
assert.equal(element("logs").scrollTop, 1234);
assert.equal(element("follow-logs").checked, true);
context.select("");
assert.equal(element("status").textContent, "准备就绪");
assert.match(element("answer").textContent, /让想法成为结果/);
assert.equal(element("events").children.length, 0);
assert.equal(element("approval-section").hidden, true);
value.status = "awaiting_approval";
value.approval = {type: "shell", id: "approval-one", status: "pending", command: "<script>bad()</script>",
  shell: "powershell.exe", workspace: "D:/trusted", timeout_seconds: 10, argv: ["powershell.exe", "-Command", "<script>bad()</script>"]};
context.render(value);
assert.equal(element("approval-section").hidden, false);
assert.match(element("approval").textContent, /<script>bad\(\)<\/script>/);
assert.match(element("approval").textContent, /powershell.exe/);
assert.match(element("approval").textContent, /timeout_seconds/);
assert.equal(element("approve").textContent, "批准执行此命令");
assert.equal(element("approve").disabled, false);
value.approval.status = "approved";
context.render(value);
assert.equal(element("approve").disabled, true);
console.log("PASS: budget explanations, tabs, safe Markdown and shell approval text, deduplication, node limits, logs, keyboard navigation, empty-state reset, single-use approval controls");

"use strict";
// Run with node tests/test_web_ui.js. No browser, packages or project server needed.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
class Element {
  constructor() { this.children = []; this.dataset = {}; this.textContent = ""; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener() {}
}
const elements = new Map();
const element = (id) => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
const context = vm.createContext({
  document: {getElementById: element, createElement: () => new Element()},
  // Leave startup pending so these checks exercise rendering without HTTP or timers.
  fetch: () => new Promise(() => {}),
});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../harness/static/app.js"), "utf8"), context);
const value = {
  run_id: "historical", status: "budget_exceeded", metadata: {mode: "openai_compatible",
    config: {max_turns: 8, total_timeout_seconds: 120}},
  state: {plan: []}, rules: null, artifacts: [], checks: {verified: false, note: "unverified"},
  result: {answer: null, reason: "turn_limit", turns: 8}, approval: null,
};
context.render(value);
assert.match(element("answer").textContent, /轮数上限/);
assert.match(element("run-limits").textContent, /8 \/ 8/);
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
console.log("PASS: historical budget explanation; limits; final answer; policy hint; event deduplication");

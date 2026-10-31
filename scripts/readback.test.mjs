// 上线回读's plan helpers: dotted paths, expectations (including {exists}) and saved values filled into later steps.
import assert from "node:assert/strict";
import test from "node:test";
import { fill, unmet, valueAt } from "./readback.mjs";

test("a dotted path reads nested fields and array indexes; a missing part reads undefined", () => {
  const value = { body: { trades: [{ id: "r1" }] }, state: { news: { tab: "debate" } } };
  assert.equal(valueAt(value, "body.trades.0.id"), "r1");
  assert.equal(valueAt(value, "state.news.tab"), "debate");
  assert.equal(valueAt(value, "state.reminders.tab"), undefined);
});

test("expectations compare whole values, {exists} only asks for presence, and every miss is named", () => {
  const state = { panel: "news", news: { tab: "debate" }, breakdown: { byPlaybook: [] } };
  assert.deepEqual(unmet(state, { panel: "news", "news.tab": "debate", breakdown: { exists: true } }), []);
  assert.deepEqual(unmet(state, { panel: "reminders", "reminders.tab": { exists: true } }), [
    "panel: 期望 \"reminders\"，实际 \"news\"", "reminders.tab: 期望 {\"exists\":true}，实际 undefined"]);
  assert.deepEqual(unmet(state, { "news.tab": { exists: false } }), ["news.tab: 期望 {\"exists\":false}，实际 \"debate\""]);
  assert.deepEqual(unmet({ observation: { text: "复盘 · 按打法、时段、星期" } }, { "observation.text": { includes: "按打法" } }), []);
  assert.equal(unmet({ observation: { text: "" } }, { "observation.text": { includes: "按打法" } }).length, 1);
  // An object's JSON is searched, so a select's options in observation.controls can be asked for.
  assert.deepEqual(unmet({ observation: { controls: [{ role: "select", options: ["固定", "追踪 · 前 N 根 K 线"] }] } },
    { "observation.controls": { includes: "前 N 根" } }), []);
});

test("saved values replace {{name}} in every string of a later step; unknown names stay as written", () => {
  const step = { api: "PUT /api/account/trade-playbooks/{{roundId}}", body: { playbookId: "{{playbookId}}", note: "{{unknown}}" } };
  assert.deepEqual(fill(step, { roundId: "a:b", playbookId: "p1" }),
    { api: "PUT /api/account/trade-playbooks/a:b", body: { playbookId: "p1", note: "{{unknown}}" } });
});

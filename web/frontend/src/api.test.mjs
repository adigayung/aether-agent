// Regression test (Node built-in `assert`, no framework) for openEventStream.
//
// Verifies that openEventStream registers exactly one listener per named SSE
// event type (no double delivery from addEventListener + onmessage).
//
// Each tool execution = 1 activity event = 1 SSE delivery.
//
// Run: node web/frontend/src/api.test.mjs

import assert from "node:assert/strict";

// Polyfill EventSource for testing.
class MockEventSource {
  constructor(url) {
    this.url = url;
    this.eventListeners = {};
    this.onmessage = null;
    this.onopen = null;
    this.onerror = null;
    this._closed = false;
    this.constructor._lastInstance = this;
  }

  addEventListener(type, listener) {
    if (!this.eventListeners[type]) this.eventListeners[type] = [];
    this.eventListeners[type].push(listener);
  }

  removeEventListener(type, listener) {
    if (this.eventListeners[type]) {
      this.eventListeners[type] = this.eventListeners[type].filter(
        (l) => l !== listener
      );
    }
  }

  // Simulate dispatch of a Named event (e.g. tool_called).
  dispatchNamed(type, dataObj) {
    const evt = { type, data: JSON.stringify(dataObj) };
    const listeners = this.eventListeners[type] || [];
    listeners.forEach((listener) => listener(evt));
    // onmessage should NOT fire for named events (that's the fix).
  }

  // Simulate dispatch of an unnamed message event.
  dispatchMessage(dataObj) {
    const evt = { type: "message", data: JSON.stringify(dataObj) };
    if (this.onmessage) this.onmessage(evt);
  }

  close() {
    this._closed = true;
  }
}

const originalEventSource = global.EventSource;
global.EventSource = MockEventSource;

// Load api.js (which imports from itself; module side-effects are minimal).
import { openEventStream } from "./api.js";

const KNOWN_EVENTS = [
  "task_created",
  "task_queued",
  "task_started",
  "phase_changed",
  "agent_commentary",
  "tool_called",
  "tool_completed",
  "observation_received",
  "provider_request",
  "provider_response",
  "validation_started",
  "validation_completed",
  "recovery_started",
  "recovery_completed",
  "change_detected",
  "approval_requested",
  "approval_resolved",
  "task_completed",
  "task_failed",
  "task_cancelled",
];

function setupStream() {
  const events = [];
  const source = openEventStream({ onEvent: (payload) => events.push(payload) });
  return { events, source };
}

// --- Test 1: each known event fires exactly once -------------------------
let allGood = true;

for (const name of KNOWN_EVENTS) {
  const { events, source } = setupStream();
  source.dispatchNamed(name, { tool: name, sample: true });
  if (events.length !== 1) {
    console.error(
      `FAIL: event "${name}" produced ${events.length} callbacks (expected 1)`
    );
    allGood = false;
  } else {
    console.log(`PASS: event "${name}" -> exactly 1 callback`);
  }
  source.close();
}

// --- Test 2: specific tool events (read_file, edit_file, etc.) ------------
const TOOL_EVENTS = [
  "tool_called",
  "tool_completed",
  "observation_received",
];
const TOOLS = ["read_file", "edit_file", "search_code", "run_command"];

for (const tool of TOOLS) {
  for (const evName of TOOL_EVENTS) {
    const { events, source } = setupStream();
    source.dispatchNamed(evName, { tool, success: true, target: `./${tool}.js` });
    if (events.length !== 1) {
      console.error(
        `FAIL: ${evName} for ${tool} produced ${events.length} callbacks (expected 1)`
      );
      allGood = false;
    } else {
      console.log(`PASS: ${evName} for ${tool} -> exactly 1 callback`);
    }
    source.close();
  }
}

// --- Test 3: onmessage fallback only for unnamed events --------------------
{
  const { events, source } = setupStream();
  source.dispatchMessage({ custom: "heartbeat" });
  assert.equal(
    events.length,
    1,
    "unnamed message should fire exactly once via onmessage"
  );
  assert.equal(events[0].custom, "heartbeat");
  console.log("PASS: unnamed message -> exactly 1 callback via onmessage");
  source.close();
}

// --- Test 4: multiple events each fire exactly once ------------------------
{
  const { events, source } = setupStream();
  source.dispatchNamed("tool_called", { tool: "read_file", target: "a" });
  source.dispatchNamed("tool_completed", { tool: "read_file", success: true });
  source.dispatchNamed("observation_received", {
    tool: "read_file",
    success: true,
    content: "result",
  });
  assert.equal(events.length, 3, "3 distinct events should yield 3 callbacks");
  console.log("PASS: 3 distinct events -> 3 callbacks (no dupes)");
  source.close();
}

// --- Test 5: onmessage is a function (for unnamed frames only) -------------
{
  const { source } = setupStream();
  // onmessage handles only unnamed message frames; named events use
  // addEventListener only. This is the fix that prevents double delivery.
  assert.equal(
    typeof source.onmessage,
    "function",
    "onmessage should be a function for unnamed message fallback"
  );
  console.log("PASS: onmessage is a function (only for unnamed frames)");
  source.close();
}

// Restore EventSource
global.EventSource = originalEventSource;

if (!allGood) {
  console.error("\n=== SOME CHECKS FAILED ===");
  process.exit(1);
} else {
  console.log("\n=== ALL TESTS PASSED ===");
}

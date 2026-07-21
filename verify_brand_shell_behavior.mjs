#!/usr/bin/env node
import assert from 'node:assert/strict';
import { createHarness } from './verify_quiet_debugger_behavior.mjs';

async function testTaskGroupPresentation() {
  const harness = createHarness();
  const { taskGroupColor, taskGroupTitle } = harness.api;
  assert.equal(taskGroupColor('same-task'), taskGroupColor('same-task'));
  assert.match(taskGroupColor('same-task'), /^(purple|cyan|green|yellow|orange|red|pink|blue)$/);
  assert.equal(taskGroupTitle({ name: 'Research GPU prices', state: 'working' }), '✦ Research GPU prices');
  assert.equal(taskGroupTitle({ name: 'Checkout', state: 'needs_user' }), '↗ Review needed: Checkout');
  assert.equal(taskGroupTitle({ name: 'Compare plans', state: 'completed' }), '✓ Compare plans');
  assert.ok(taskGroupTitle({ name: 'A task name that is deliberately far longer than Chrome allows', state: 'working' }).length <= 40);
}

async function testSessionStatePersistsAndRefreshesGroup() {
  const harness = createHarness({
    sessions: {
      S: { sessionId: 'S', name: 'Compare plans', state: 'working', color: 'purple', groupId: 7, tabIds: [1], updatedAt: 1 },
    },
    tabs: { 1: { groupId: 7, active: false, url: 'https://example.com' } },
  });
  const session = await harness.api.updateTaskSessionState('S', 'completed');
  assert.equal(session.state, 'completed');
  assert.equal(harness.sessions().S.state, 'completed');
  assert.deepEqual(harness.controller.tabGroupUpdates.at(-1), {
    groupId: 7,
    options: { title: '✓ Compare plans', color: 'purple', collapsed: false },
  });
  await assert.rejects(harness.api.updateTaskSessionState('S', 'paused'), /state must be/);
}

async function testPointerIsForegroundOnly() {
  const hidden = createHarness({ tabs: { 1: { active: false, windowId: 1, groupId: -1 } } });
  assert.equal(await hidden.api.showAgentPointer(1, 20, 30, true), false);
  assert.equal(hidden.controller.scriptCalls.length, 0);

  const backgroundWindow = createHarness({ tabs: { 1: { active: true, windowId: 2, groupId: -1 } } });
  assert.equal(await backgroundWindow.api.showAgentPointer(1, 20, 30, true), false);
  assert.equal(backgroundWindow.controller.scriptCalls.length, 0);

  const visible = createHarness({ tabs: { 1: { active: true, windowId: 1, groupId: -1 } } });
  assert.equal(await visible.api.showAgentPointer(1, 20, 30, true), true);
  assert.equal(visible.controller.scriptCalls.length, 1);
  const source = visible.controller.scriptCalls[0].func.toString();
  assert.match(source, /__chrome_bridge_pointer__/);
  assert.match(source, /pointer-events:none/);
  assert.match(source, /ripple/);

  const disabled = createHarness({
    tabs: { 1: { active: true, windowId: 1, groupId: -1 } },
    preferences: { showAgentPointer: false },
  });
  assert.equal(await disabled.api.showAgentPointer(1, 20, 30, true), false);
  assert.equal(disabled.controller.scriptCalls.length, 0);
}

async function testPopupStatusUsesLatestTaskAndPreference() {
  const harness = createHarness({
    sessions: {
      old: { sessionId: 'old', name: 'Old task', state: 'working', groupId: 7, tabIds: [1], updatedAt: 1 },
      current: { sessionId: 'current', name: 'Checkout', state: 'needs_user', color: 'cyan', groupId: 8, tabIds: [2], updatedAt: 2 },
    },
    tabs: {
      1: { active: false, windowId: 1, groupId: 7 },
      2: { active: true, windowId: 1, groupId: 8 },
    },
    preferences: { showAgentPointer: false },
  });
  const status = await harness.api.getBridgeStatus();
  assert.equal(status.connected, true);
  assert.equal(status.quietReads, true);
  assert.equal(status.showAgentPointer, false);
  assert.deepEqual(JSON.parse(JSON.stringify(status.activeTask)), {
    name: 'Checkout', state: 'needs_user', stateLabel: 'Needs your help', symbol: '↗', color: 'cyan',
  });
}

async function testHandoffIsCompactAndNonLayoutShifting() {
  const harness = createHarness({ tabs: { 1: { active: true, windowId: 1, groupId: -1 } } });
  await harness.api.showHandoffOverlay(1, 'Review checkout');
  const source = harness.controller.scriptCalls[0].func.toString();
  assert.match(source, /bottom:24px/);
  assert.match(source, /pointer-events:none/);
  assert.match(source, /Chrome Bridge needs your help/);
  assert.doesNotMatch(source, /top:0.*right:0/);
}

const tests = [
  testTaskGroupPresentation,
  testSessionStatePersistsAndRefreshesGroup,
  testPointerIsForegroundOnly,
  testPopupStatusUsesLatestTaskAndPreference,
  testHandoffIsCompactAndNonLayoutShifting,
];

for (const test of tests) {
  await test();
  process.stdout.write(`PASS ${test.name}\n`);
}
process.stdout.write('Brand shell behavioral contract OK\n');

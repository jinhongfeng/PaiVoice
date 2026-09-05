import test from 'node:test';
import assert from 'node:assert/strict';

import { PetController } from '../packages/web-client/pet-controller.js';


function fakeClock() {
  let nextId = 1;
  const jobs = new Map();
  return {
    setTimer(fn, delay) {
      const id = nextId++;
      jobs.set(id, { fn, delay });
      return id;
    },
    clearTimer(id) { jobs.delete(id); },
    nextDelay() { return jobs.values().next().value?.delay; },
    runNext() {
      const entry = jobs.entries().next().value;
      assert.ok(entry, 'expected a scheduled frame');
      const [id, job] = entry;
      jobs.delete(id);
      job.fn();
    },
    size() { return jobs.size; },
  };
}


function setup() {
  const clock = fakeClock();
  const frames = [];
  const controller = new PetController({
    onFrame: frame => frames.push(frame),
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  return { clock, frames, controller };
}


test('maps voice call states to standard pet actions', () => {
  const { controller, frames } = setup();
  const cases = [
    ['dialing', 'running', 7],
    ['listening', 'waiting', 6],
    ['thinking', 'running', 7],
    ['speaking', 'waving', 3],
    ['closed', 'idle', 0],
  ];
  for (const [state, action, row] of cases) {
    controller.setCallState(state);
    assert.deepEqual(frames.at(-1), { action, row, frame: 0 });
  }
});


test('does not restart an action when the same call state repeats', () => {
  const { clock, controller, frames } = setup();
  controller.setCallState('thinking');
  clock.runNext();
  assert.equal(frames.at(-1).frame, 1);
  controller.setCallState('thinking');
  assert.equal(frames.at(-1).frame, 1);
});


test('plays a transient reaction once then restores the latest call state', () => {
  const { clock, controller, frames } = setup();
  controller.setCallState('listening');
  controller.trigger('review');
  assert.equal(frames.at(-1).action, 'review');
  for (let i = 0; i < 6; i += 1) clock.runNext();
  assert.deepEqual(frames.at(-1), { action: 'waiting', row: 6, frame: 0 });
});


test('roaming overrides reactions and uses the movement direction', () => {
  const { controller, frames } = setup();
  controller.setCallState('thinking');
  controller.trigger('failed');
  controller.setRoaming(1);
  assert.deepEqual(frames.at(-1), { action: 'running-right', row: 1, frame: 0 });
  controller.setRoaming(-1);
  assert.deepEqual(frames.at(-1), { action: 'running-left', row: 2, frame: 0 });
  controller.stopRoaming();
  assert.equal(frames.at(-1).action, 'failed');
});


test('uses the standard per-frame timing and cancels work on destroy', () => {
  const { clock, controller, frames } = setup();
  assert.deepEqual(frames.at(-1), { action: 'idle', row: 0, frame: 0 });
  assert.equal(clock.nextDelay(), 280);
  clock.runNext();
  assert.deepEqual(frames.at(-1), { action: 'idle', row: 0, frame: 1 });
  assert.equal(clock.nextDelay(), 110);
  controller.destroy();
  assert.equal(clock.size(), 0);
});


test('can redraw the current frame after the sprite DOM is remounted', () => {
  const { clock, controller, frames } = setup();
  controller.setCallState('thinking');
  clock.runNext();
  const scheduledBefore = clock.size();
  controller.refresh();
  assert.deepEqual(frames.at(-1), { action: 'running', row: 7, frame: 1 });
  assert.equal(clock.size(), scheduledBefore);
});


test('invokes host timer functions without rebinding their receiver', () => {
  let timerId = 0;
  function hostSetTimer() {
    assert.equal(this, undefined);
    timerId += 1;
    return timerId;
  }
  function hostClearTimer() {
    assert.equal(this, undefined);
  }
  const controller = new PetController({
    onFrame() {},
    setTimer: hostSetTimer,
    clearTimer: hostClearTimer,
  });
  controller.destroy();
});

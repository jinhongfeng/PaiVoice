# Voice-driven Pet Controller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drive installed Codex-format pet animations from the voice chat project's real states and events.

**Architecture:** A testable `PetController` module owns action selection, priority, transient recovery, and exact frame timing. The existing page forwards VoiceCall and roaming events and renders each selected atlas cell.

**Tech Stack:** Browser ES modules, JavaScript timers, Node test runner, Python pytest

## Global Constraints

- Do not connect to the Codex app or Codex tasks.
- Preserve existing SVG pet behavior.
- Preserve installed pet selection, manual previews, dragging, and roaming.
- Use the 8-column by 9-row installed spritesheet contract.
- The workspace is not a Git repository, so commit steps are omitted.

---

### Task 1: Build the pet action state machine

**Files:**
- Create: `packages/web-client/pet-controller.js`
- Create: `tests/pet-controller.test.mjs`

**Interfaces:**
- Consumes: project call states, reaction names, and roaming direction.
- Produces: `new PetController({ onFrame, setTimer, clearTimer })`, `setCallState(state)`, `trigger(action)`, `setRoaming(direction)`, `stopRoaming()`, and `destroy()`.

- [ ] **Step 1: Write failing Node tests**

Test state mapping, same-state stability, review/failed/jumping recovery, roaming priority, left/right row selection, and timer-driven frame progression with a deterministic fake clock.

- [ ] **Step 2: Verify RED**

Run: `node --test tests/pet-controller.test.mjs`

Expected: failure because `pet-controller.js` does not exist.

- [ ] **Step 3: Implement the controller**

Define the standard action rows and durations, select the highest-priority action, emit exact frames, and schedule the next frame with injected timers. Do not add rendering or VoiceCall dependencies to this module.

- [ ] **Step 4: Verify GREEN**

Run: `node --test tests/pet-controller.test.mjs`

Expected: all controller tests pass.

### Task 2: Integrate voice, reaction, and roaming events

**Files:**
- Modify: `packages/web-client/index.html`
- Modify: `packages/realtime-core/server.py`
- Modify: `tests/test_pet_sprite_rendering.py`

**Interfaces:**
- Consumes: `PetController` frame callbacks and existing `VoiceCall` callbacks.
- Produces: DOM background-position updates and automatic project-state reactions.

- [ ] **Step 1: Write failing integration assertions**

Assert that the page imports `PetController`, forwards state/reply/error/playback events, routes roaming direction, and that the server exposes `pet-controller.js`.

- [ ] **Step 2: Verify RED**

Run: `pytest -q tests/test_pet_sprite_rendering.py`

Expected: failure because controller integration is absent.

- [ ] **Step 3: Wire the controller into the page**

Replace the CSS stepped sprite playback with frame callbacks. Keep `petSetMood` as the compatibility boundary for existing buttons and SVG pets. Forward voice states and transient events, and make roam start/stop set and clear the locomotion override.

- [ ] **Step 4: Serve the new browser module**

Add `/pet-controller.js` to the server's static module map using the same no-store response behavior as `voice-call.js`.

- [ ] **Step 5: Run full verification**

Run: `node --test tests/pet-controller.test.mjs`, `pytest -q`, restart the service, and verify that the served HTML and module contain the controller integration.

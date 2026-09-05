# Voice-driven Pet Controller Design

## Goal

Make installed Codex-format pets react automatically to this voice chat project's own lifecycle. The feature has no dependency on the Codex app or Codex task events.

## Architecture

Add a browser module named `pet-controller.js`. It owns the pet action state machine and deterministic frame scheduler. `index.html` remains responsible for DOM mounting, installed-pet selection, and roaming coordinates; it forwards voice and UI events to the controller and renders the controller's frame output into `background-position`.

Action priority is `roaming > transient reaction > call state`. Repeated delivery of the same call state must not restart an animation. Transient reactions play one complete cycle and then resolve to the latest call state. Roaming selects the dedicated right- or left-running row and returns to the latest call state when movement ends.

## State Mapping

| Project signal | Pet action |
| --- | --- |
| idle or closed | idle |
| dialing | running |
| listening | waiting |
| thinking | running |
| reply text received | review once |
| audio speaking | waving |
| playback completed | jumping once, then waiting |
| request or connection error | failed once |
| roam right | running-right |
| roam left | running-left |

## Frame Playback

Use exact row and frame indices instead of a CSS background-position sweep. Use the standard per-row durations from the installed Codex pet contract. Loop `idle`, `waiting`, `running`, `waving`, and directional running. Play `jumping`, `failed`, and `review` once when used as transient reactions. The renderer sets positive CSS background-position percentages using `column * 100 / 7` and `row * 100 / 8`.

## Compatibility

Only installed sprite pets use the controller's frame output. Existing SVG pets keep their CSS animations. Existing pet selection, manual action buttons, dragging, saved homes, and automatic roaming remain available.

## Verification

Node tests cover state mapping, no-restart behavior, transient recovery, roaming priority, direction selection, and exact frame progression. Python source-level tests ensure the browser imports and connects the controller. The full existing test suite and a live served-page check complete verification.

# Pet Sprite Flicker Fix Design

## Problem

The Codex pet spritesheet is an 8-column by 9-row atlas. The current CSS animation moves `background-position` toward negative percentages and always spans the full horizontal range. In CSS, percentage positioning for an oversized background uses the difference between the container and image dimensions. The negative direction therefore moves the sheet away from the viewport, while the fixed range also samples beyond rows that contain fewer than eight frames. The result is periodic transparent frames that appear as flicker.

## Design

Keep the existing CSS `steps()` animation and image-loading lifecycle. Calculate positions in the positive CSS background-position direction. For an atlas with eight columns and a row containing `N` frames, animate from `0%` to `N * 100 / 7%`; with `steps(N)`, the visible samples are the exact frame positions `0` through `N - 1`. Calculate the row position as `row * 100 / 8%` for the nine-row atlas.

Do not change SVG pets, roaming, dragging, mood mapping, durations, server endpoints, or pet assets.

## Verification

Add a regression test that reads the browser source and verifies the coordinate formulas. It must fail against the current negative/fixed calculations and pass after the minimal correction. Run the focused regression test, the existing automated suite, and a syntax/coordinate sanity check.

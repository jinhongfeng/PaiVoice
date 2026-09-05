# Pet Sprite Flicker Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop Codex spritesheet pets from intermittently rendering transparent atlas regions.

**Architecture:** Preserve the current CSS stepped animation and preload behavior. Correct only the percentage coordinates derived in `petSprApply`, with one source-level regression test protecting the atlas math.

**Tech Stack:** HTML/CSS/JavaScript, Python unittest/pytest-compatible tests

## Global Constraints

- Preserve the 8-column by 9-row atlas contract.
- Preserve all existing pet states and frame counts.
- Do not alter SVG pet behavior or server endpoints.
- The workspace is not a Git repository, so no commit step is possible.

---

### Task 1: Correct spritesheet coordinates

**Files:**
- Create: `tests/test_pet_sprite_rendering.py`
- Modify: `packages/web-client/index.html:1161-1197`

**Interfaces:**
- Consumes: `SPR_FRAMES`, the selected mood row, and CSS variables `--sprPos`/`--sprPosEnd`.
- Produces: exact positive background-position percentages for every visible frame and atlas row.

- [ ] **Step 1: Write the failing regression test**

```python
from pathlib import Path


INDEX = Path(__file__).parents[1] / "packages" / "web-client" / "index.html"


def test_pet_sprite_animation_uses_exact_positive_atlas_positions():
    source = INDEX.read_text(encoding="utf-8")
    assert "const startY = row * 100 / 8;" in source
    assert "const endX = steps * 100 / 7;" in source
    assert "el.style.setProperty('--sprPosEnd', endX + '% ' + startY + '%');" in source
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `pytest -q tests/test_pet_sprite_rendering.py`

Expected: one assertion failure because the corrected formulas are absent.

- [ ] **Step 3: Implement the minimal formula correction**

Replace the negative row position and fixed `-100%` endpoint with positive atlas-relative values:

```javascript
const startY = row * 100 / 8;
const startX = 0;
const endX = steps * 100 / 7;
```

Use `endX` when setting `--sprPosEnd`.

- [ ] **Step 4: Run focused and full verification**

Run: `pytest -q tests/test_pet_sprite_rendering.py` and `pytest -q`.

Expected: focused test passes and the existing suite has no regressions.

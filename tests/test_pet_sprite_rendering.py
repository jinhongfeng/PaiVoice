from pathlib import Path


INDEX = Path(__file__).parents[1] / "packages" / "web-client" / "index.html"
SERVER = Path(__file__).parents[1] / "packages" / "realtime-core" / "server.py"
VOICE_CALL = Path(__file__).parents[1] / "packages" / "web-client" / "voice-call.js"
CONTROLLER = Path(__file__).parents[1] / "packages" / "web-client" / "pet-controller.js"
DOCKERFILE = Path(__file__).parents[1] / "Dockerfile"


def test_pet_sprite_animation_uses_exact_positive_atlas_positions():
    source = INDEX.read_text(encoding="utf-8")
    assert "el.style.backgroundPosition = (frame * 100 / 7) + '% ' + (row * 100 / 8) + '%';" in source


def test_pet_sprite_animation_uses_standard_per_frame_durations():
    source = CONTROLLER.read_text(encoding="utf-8")
    assert "idle:            { row: 0, durations: [280, 110, 110, 140, 140, 320] }" in source
    assert "review:          { row: 8, durations: [150, 150, 150, 150, 150, 280] }" in source


def test_page_no_longer_uses_css_sprite_sheet_sweeps():
    source = INDEX.read_text(encoding="utf-8")
    assert "@keyframes pSprStep" not in source
    assert "classList.add('playing')" not in source


def test_roaming_pet_does_not_hide_its_own_parent():
    source = INDEX.read_text(encoding="utf-8")
    assert "body.pet-away .orb-wrap{ display:none" not in source
    assert "const origin = petEl.getBoundingClientRect();" in source
    assert "petEl.style.left = origin.left + 'px'; petEl.style.top = origin.top + 'px';" in source


def test_page_drives_sprite_pets_through_the_pet_controller():
    source = INDEX.read_text(encoding="utf-8")
    assert "import { PetController } from './pet-controller.js';" in source
    assert "const petController = new PetController({" in source
    assert "petController.setCallState('dialing');" in source
    assert "petController.setCallState(m);" in source
    assert "if (active) petController.trigger('waving');" in source
    assert "petController.trigger('review');" in source
    assert "petController.trigger('failed');" in source
    assert "petController.trigger('jumping');" in source
    assert "petController.setRoaming(x >= sx ? 1 : -1);" in source
    assert "petController.stopRoaming();" in source


def test_server_exposes_the_pet_controller_browser_module():
    source = SERVER.read_text(encoding="utf-8")
    assert '"/pet-controller.js": ("application/javascript; charset=utf-8", pet_controller_text)' in source
    assert "PET_ROW_FRAMES = (6, 8, 8, 4, 5, 8, 6, 6, 6)" in source
    assert "COPY packages/web-client/pet-controller.js ./pet-controller.js" in DOCKERFILE.read_text(encoding="utf-8")


def test_voice_call_distinguishes_completed_playback_from_interruptions():
    source = VOICE_CALL.read_text(encoding="utf-8")
    assert "this.emit('playbackComplete');" in source


def test_pet_panel_accepts_full_npx_command_and_uses_server_sprite_url():
    source = INDEX.read_text(encoding="utf-8")
    assert "npx codex-pet-installer add kitagawa-marin" in source
    assert "input=" in source
    assert "p.spriteUrl" in source


def test_pet_panel_can_delete_an_installed_codex_pet():
    source = INDEX.read_text(encoding="utf-8")
    assert "async function petRemoveSprite(slug)" in source
    assert "method:'DELETE'" in source
    assert "petState.kind = 'cat'" in source
    assert "删除" in source

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SERVER_PATH = ROOT / "packages" / "realtime-core" / "server.py"
sys.path.insert(0, str(SERVER_PATH.parent))
spec = importlib.util.spec_from_file_location("pet_server", SERVER_PATH)
server = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = server
spec.loader.exec_module(server)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("kitagawa-marin", "kitagawa-marin"),
        ("npx codex-pet-installer add kitagawa-marin", "kitagawa-marin"),
        ("npx --yes codex-pet-installer add kitagawa-marin", "kitagawa-marin"),
    ],
)
def test_parse_pet_install_input_accepts_id_and_fixed_npx_commands(value, expected):
    assert server._parse_pet_install_input(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "npx codex-pet-installer remove kitagawa-marin",
        "npx codex-pet-installer add kitagawa-marin --force",
        "kitagawa-marin; whoami",
        "../kitagawa-marin",
        "",
    ],
)
def test_parse_pet_install_input_rejects_everything_else(value):
    assert server._parse_pet_install_input(value) == ""


def test_pets_dir_uses_codex_home(monkeypatch, tmp_path):
    codex_home = tmp_path / "custom-codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert Path(server._pets_dir()) == codex_home / "pets"


def test_pet_asset_supports_manifest_paths_and_blocks_traversal(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    pet_dir = Path(server._pet_dir("marin"))
    asset = pet_dir / "assets" / "marin.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"png")

    (pet_dir / "pet.json").write_text(
        json.dumps({"id": "marin", "assets": {"spritesheet": "assets/marin.png"}}),
        encoding="utf-8",
    )
    assert server._pet_asset(str(pet_dir)) == (str(asset.resolve()), "image/png")

    (pet_dir / "pet.json").write_text(
        json.dumps({"id": "marin", "spritesheetPath": "../../outside.webp"}),
        encoding="utf-8",
    )
    assert server._pet_asset(str(pet_dir)) is None


def test_installed_list_only_includes_complete_pets(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    complete = Path(server._pet_dir("complete"))
    complete.mkdir(parents=True)
    (complete / "pet.json").write_text('{"displayName":"Complete"}', encoding="utf-8")
    (complete / "spritesheet.webp").write_bytes(b"webp")
    incomplete = Path(server._pet_dir("incomplete"))
    incomplete.mkdir(parents=True)
    (incomplete / "pet.json").write_text("{}", encoding="utf-8")

    pets = server._pets_installed()
    assert [pet["slug"] for pet in pets] == ["complete"]
    assert pets[0]["spriteUrl"] == "/v1/pets/complete/asset"


class _FakeProcess:
    def __init__(self, returncode=0, stdout=b"installed", stderr=b""):
        self.returncode = returncode
        self._result = (stdout, stderr)

    async def communicate(self):
        return self._result

    def kill(self):
        self.returncode = -1


def test_pet_install_invokes_fixed_installer_arguments(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        pet_dir = codex_home / "pets" / "kitagawa-marin"
        pet_dir.mkdir(parents=True)
        (pet_dir / "pet.json").write_text(
            '{"id":"kitagawa-marin","displayName":"Marin"}', encoding="utf-8"
        )
        (pet_dir / "spritesheet.webp").write_bytes(b"webp")
        return _FakeProcess()

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_exec)
    ok, message, slug = asyncio.run(
        server._pet_install("npx codex-pet-installer add kitagawa-marin")
    )

    executable = "npx.cmd" if os.name == "nt" else "npx"
    assert calls[0][0] == (
        executable,
        "--yes",
        "codex-pet-installer",
        "add",
        "kitagawa-marin",
    )
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["env"]["CODEX_HOME"] == str(codex_home)
    assert (ok, message, slug) == (True, "Marin", "kitagawa-marin")


def test_pet_install_reports_nonzero_exit(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))

    async def fake_exec(*args, **kwargs):
        return _FakeProcess(returncode=1, stderr=b"not found")

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_exec)
    ok, message, slug = asyncio.run(server._pet_install("missing-pet"))
    assert not ok
    assert "not found" in message
    assert slug == "missing-pet"


def test_pet_remove_deletes_only_a_direct_pet_child(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    pet_dir = Path(server._pet_dir("kitagawa-marin"))
    pet_dir.mkdir(parents=True)
    (pet_dir / "pet.json").write_text("{}", encoding="utf-8")

    assert server._pet_remove("kitagawa-marin")[0] is True
    assert not pet_dir.exists()
    assert server._pet_remove("../outside")[0] is False
    assert server._pet_remove("missing")[0] is False


def test_server_source_exposes_install_delete_and_dynamic_asset_routes():
    source = SERVER_PATH.read_text(encoding="utf-8")
    assert 'qs.get("input")' in source
    assert 'request.method == "DELETE"' in source
    assert "_pet_remove(" in source
    assert 'r"/v1/pets/([a-z0-9-]+)/(pet\\.json|asset)"' in source

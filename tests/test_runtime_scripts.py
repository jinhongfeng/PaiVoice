from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_stop_script_stops_every_listener_on_app_ports():
    source = (ROOT / "scripts" / "stop.ps1").read_text(encoding="utf-8")
    assert "Select-Object -First 1" not in source
    assert "Select-Object -ExpandProperty OwningProcess -Unique" in source


def test_run_script_clears_stale_services_before_starting():
    source = (ROOT / "scripts" / "run.ps1").read_text(encoding="utf-8")
    assert "& (Join-Path $PSScriptRoot \"stop.ps1\")" in source

"""The flasher must not change a body's identity when a card is re-flashed."""
import importlib.util
from pathlib import Path

import pytest

from pychdk.util import parse_own_txt

TOOL = Path(__file__).resolve().parent.parent / "tools" / "flash_chdk.py"


def _load_tool():
    """Load tools/flash_chdk.py, which is a script and not a package."""
    spec = importlib.util.spec_from_file_location("flash_chdk", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCameraIdSurvivesAReflash:
    def test_main_reads_the_id_before_it_erases_the_card(self, monkeypatch):
        tool = _load_tool()
        calls = []
        received = {}

        def record(name, result=None):
            def fake(*args, **kwargs):
                calls.append(name)
                return result
            return fake

        def fake_write(mount_point, existing_id=None):
            calls.append("write_camera_side")
            received["existing_id"] = existing_id

        monkeypatch.setattr(tool, "download_chdk", record("download_chdk"))
        monkeypatch.setattr(tool, "find_removable_disks", record("find", []))
        monkeypatch.setattr(tool, "pick_disk", record("pick_disk", "/dev/disk9"))
        monkeypatch.setattr(
            tool, "read_existing_camera_id",
            record("read_existing_camera_id", "abc123def456"),
        )
        monkeypatch.setattr(tool, "format_card", record("format_card", "/Volumes/X"))
        monkeypatch.setattr(tool, "extract_chdk", record("extract_chdk"))
        monkeypatch.setattr(tool, "patch_boot_sector", record("patch_boot_sector"))
        monkeypatch.setattr(tool, "get_mount_point", record("get_mount_point", "/Volumes/X"))
        monkeypatch.setattr(tool, "write_camera_side", fake_write)
        monkeypatch.setattr(tool, "eject_card", record("eject_card"))

        tool.main()

        assert calls.index("read_existing_camera_id") < calls.index("format_card")
        assert received["existing_id"] == "abc123def456"

    def test_write_camera_side_keeps_the_id_it_is_handed(
        self, tmp_path, monkeypatch, capsys,
    ):
        tool = _load_tool()
        monkeypatch.setattr("builtins.input", lambda prompt="": "e")
        tool.write_camera_side(str(tmp_path), existing_id="abc123def456")
        assert (tmp_path / "OWN.TXT").read_text() == "EVEN\nid=abc123def456\n"
        assert "kept" in capsys.readouterr().out

    def test_write_camera_side_mints_an_id_when_there_is_none(
        self, tmp_path, monkeypatch, capsys,
    ):
        tool = _load_tool()
        monkeypatch.setattr("builtins.input", lambda prompt="": "o")
        tool.write_camera_side(str(tmp_path))
        side, camera_id = parse_own_txt((tmp_path / "OWN.TXT").read_bytes())
        assert side == "ODD"
        assert camera_id is not None
        assert "minted" in capsys.readouterr().out

    def test_skipping_parity_still_writes_the_id_back(
        self, tmp_path, monkeypatch, capsys,
    ):
        tool = _load_tool()
        monkeypatch.setattr("builtins.input", lambda prompt="": "s")
        tool.write_camera_side(str(tmp_path), existing_id="abc123def456")
        assert (tmp_path / "OWN.TXT").read_text() == "id=abc123def456\n"
        assert "kept" in capsys.readouterr().out

    def test_an_unrecognized_answer_still_writes_the_id_back(
        self, tmp_path, monkeypatch,
    ):
        tool = _load_tool()
        monkeypatch.setattr("builtins.input", lambda prompt="": "banana")
        tool.write_camera_side(str(tmp_path), existing_id="abc123def456")
        assert (tmp_path / "OWN.TXT").read_text() == "id=abc123def456\n"

    def test_skipping_parity_with_no_id_writes_nothing(
        self, tmp_path, monkeypatch,
    ):
        tool = _load_tool()
        monkeypatch.setattr("builtins.input", lambda prompt="": "s")
        tool.write_camera_side(str(tmp_path))
        assert not (tmp_path / "OWN.TXT").exists()

    def test_a_card_that_will_not_mount_is_treated_as_blank(
        self, monkeypatch, capsys,
    ):
        tool = _load_tool()

        def refuse(disk):
            raise SystemExit(1)

        monkeypatch.setattr(tool, "get_mount_point", refuse)
        assert tool.read_existing_camera_id("/dev/disk9") is None
        assert "blank" in capsys.readouterr().out

    def test_a_mounted_card_with_no_own_txt_has_no_id(
        self, tmp_path, monkeypatch,
    ):
        tool = _load_tool()
        monkeypatch.setattr(tool, "get_mount_point", lambda disk: str(tmp_path))
        assert tool.read_existing_camera_id("/dev/disk9") is None

    def test_an_unreadable_own_txt_stops_rather_than_reporting_no_id(
        self, tmp_path, monkeypatch,
    ):
        tool = _load_tool()
        # A path that exists but cannot be read as a file: not absence.
        (tmp_path / "OWN.TXT").mkdir()
        monkeypatch.setattr(tool, "get_mount_point", lambda disk: str(tmp_path))
        with pytest.raises(SystemExit):
            tool.read_existing_camera_id("/dev/disk9")

    def test_main_does_not_erase_a_card_it_could_not_check(
        self, tmp_path, monkeypatch,
    ):
        tool = _load_tool()
        (tmp_path / "OWN.TXT").mkdir()
        formatted = []
        monkeypatch.setattr(tool, "download_chdk", lambda: None)
        monkeypatch.setattr(tool, "find_removable_disks", lambda: [])
        monkeypatch.setattr(tool, "pick_disk", lambda disks: "/dev/disk9")
        monkeypatch.setattr(tool, "get_mount_point", lambda disk: str(tmp_path))
        monkeypatch.setattr(
            tool, "format_card", lambda disk: formatted.append(disk),
        )
        with pytest.raises(SystemExit):
            tool.main()
        assert formatted == []

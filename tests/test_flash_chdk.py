"""The flasher must not change a body's identity when a card is re-flashed."""
import importlib.util
import plistlib
import subprocess
from pathlib import Path

import pytest

from pychdk.util import parse_own_txt

TOOL = Path(__file__).resolve().parent.parent / "tools" / "flash_chdk.py"

# Real `diskutil list -plist` output, captured on macOS 15 from a disk
# with a GUID partition scheme. The shapes below are taken from it:
# AllDisksAndPartitions holds whole disks, each with a Content naming
# the partition scheme and a Partitions list (APFS containers add
# APFSVolumes). A genuinely blank card — no partition map at all —
# could not be captured here, since it needs an SD card in a reader;
# _blank_layout() is this structure with the layout emptied, and the
# real thing is still worth one check at the bench.
PARTITIONED_LAYOUT = {
    "AllDisks": ["disk4", "disk4s1"],
    "AllDisksAndPartitions": [
        {
            "Content": "GUID_partition_scheme",
            "DeviceIdentifier": "disk4",
            "OSInternal": False,
            "Partitions": [
                {
                    "Content": "Apple_HFS",
                    "DeviceIdentifier": "disk4s1",
                    "DiskUUID": "12EEA754-2CA1-4EA7-A1B4-54DF40936F21",
                    "Size": 15931539456,
                },
            ],
            "Size": 15931539456,
        },
    ],
    "VolumesFromDisks": [],
    "WholeDisks": ["disk4"],
}


def _blank_layout():
    """PARTITIONED_LAYOUT with no partition map and no filesystem."""
    return {
        "AllDisks": ["disk4"],
        "AllDisksAndPartitions": [
            {
                "Content": "",
                "DeviceIdentifier": "disk4",
                "OSInternal": False,
                "Size": 15931539456,
            },
        ],
        "VolumesFromDisks": [],
        "WholeDisks": ["disk4"],
    }


def _fake_run(layout=None, returncode=0, stdout=None):
    """Stand in for _run, answering `diskutil list -plist` with a plist."""
    if stdout is None:
        stdout = plistlib.dumps(layout).decode() if layout else ""

    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")

    return run


def _dispatching_run(info=None, layout=None):
    """Stand in for _run, answering `info` and `list` separately."""

    def run(cmd, **kwargs):
        payload = layout if "list" in cmd else info
        stdout = plistlib.dumps(payload).decode() if payload is not None else ""
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    return run


def _load_tool():
    """Load tools/flash_chdk.py, which is a script and not a package."""
    spec = importlib.util.spec_from_file_location("flash_chdk", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestGetMountPoint:
    """A path we guessed is worse than no path: callers cannot tell."""

    def test_a_report_with_no_mount_point_fails(self, monkeypatch):
        tool = _load_tool()
        monkeypatch.setattr(
            tool, "_run", _dispatching_run(info={"DeviceIdentifier": "disk4s1"}),
        )
        with pytest.raises(SystemExit):
            tool.get_mount_point("/dev/disk4")

    def test_an_empty_mount_point_fails(self, monkeypatch):
        tool = _load_tool()
        monkeypatch.setattr(tool, "_run", _dispatching_run(info={"MountPoint": ""}))
        with pytest.raises(SystemExit):
            tool.get_mount_point("/dev/disk4")

    def test_a_mount_point_that_is_not_there_fails(self, monkeypatch):
        tool = _load_tool()
        monkeypatch.setattr(
            tool, "_run",
            _dispatching_run(info={"MountPoint": "/Volumes/NoSuchCard"}),
        )
        with pytest.raises(SystemExit):
            tool.get_mount_point("/dev/disk4")

    def test_a_real_mount_point_is_returned(self, tmp_path, monkeypatch):
        tool = _load_tool()
        monkeypatch.setattr(
            tool, "_run", _dispatching_run(info={"MountPoint": str(tmp_path)}),
        )
        assert tool.get_mount_point("/dev/disk4") == str(tmp_path)


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

        def fake_write(mount_point, existing_side=None, existing_id=None):
            calls.append("write_camera_side")
            received["existing_side"] = existing_side
            received["existing_id"] = existing_id

        monkeypatch.setattr(tool, "download_chdk", record("download_chdk"))
        monkeypatch.setattr(tool, "find_removable_disks", record("find", []))
        monkeypatch.setattr(tool, "pick_disk", record("pick_disk", "/dev/disk9"))
        monkeypatch.setattr(
            tool, "read_existing_own_txt",
            record("read_existing_own_txt", ("EVEN", "abc123def456")),
        )
        monkeypatch.setattr(tool, "format_card", record("format_card", "/Volumes/X"))
        monkeypatch.setattr(tool, "extract_chdk", record("extract_chdk"))
        monkeypatch.setattr(tool, "patch_boot_sector", record("patch_boot_sector"))
        monkeypatch.setattr(tool, "get_mount_point", record("get_mount_point", "/Volumes/X"))
        monkeypatch.setattr(tool, "write_camera_side", fake_write)
        monkeypatch.setattr(tool, "eject_card", record("eject_card"))

        tool.main()

        assert calls.index("read_existing_own_txt") < calls.index("format_card")
        assert received["existing_id"] == "abc123def456"
        assert received["existing_side"] == "EVEN"

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

    def test_skipping_parity_leaves_the_existing_assignment_alone(
        self, tmp_path, monkeypatch, capsys,
    ):
        tool = _load_tool()
        monkeypatch.setattr("builtins.input", lambda prompt="": "s")
        tool.write_camera_side(
            str(tmp_path), existing_side="EVEN", existing_id="abc123def456",
        )
        assert (tmp_path / "OWN.TXT").read_text() == "EVEN\nid=abc123def456\n"
        assert "kept" in capsys.readouterr().out

    def test_skipping_parity_on_a_card_that_had_none_writes_the_id_alone(
        self, tmp_path, monkeypatch,
    ):
        tool = _load_tool()
        monkeypatch.setattr("builtins.input", lambda prompt="": "s")
        tool.write_camera_side(str(tmp_path), existing_id="abc123def456")
        assert (tmp_path / "OWN.TXT").read_text() == "id=abc123def456\n"

    def test_an_unrecognized_answer_leaves_the_assignment_alone(
        self, tmp_path, monkeypatch,
    ):
        tool = _load_tool()
        monkeypatch.setattr("builtins.input", lambda prompt="": "banana")
        tool.write_camera_side(
            str(tmp_path), existing_side="ODD", existing_id="abc123def456",
        )
        assert (tmp_path / "OWN.TXT").read_text() == "ODD\nid=abc123def456\n"

    def test_choosing_a_parity_overrides_the_one_on_the_card(
        self, tmp_path, monkeypatch,
    ):
        tool = _load_tool()
        monkeypatch.setattr("builtins.input", lambda prompt="": "o")
        tool.write_camera_side(
            str(tmp_path), existing_side="EVEN", existing_id="abc123def456",
        )
        assert (tmp_path / "OWN.TXT").read_text() == "ODD\nid=abc123def456\n"

    def test_skipping_parity_with_no_id_writes_nothing(
        self, tmp_path, monkeypatch,
    ):
        tool = _load_tool()
        monkeypatch.setattr("builtins.input", lambda prompt="": "s")
        tool.write_camera_side(str(tmp_path))
        assert not (tmp_path / "OWN.TXT").exists()

    def test_a_card_with_no_filesystem_is_treated_as_blank(
        self, monkeypatch, capsys,
    ):
        tool = _load_tool()

        def refuse(disk):
            raise SystemExit(1)

        monkeypatch.setattr(tool, "get_mount_point", refuse)
        monkeypatch.setattr(tool, "_run", _fake_run(_blank_layout()))
        assert tool.read_existing_own_txt("/dev/disk4") == (None, None)
        assert "blank" in capsys.readouterr().out

    def test_a_volume_that_will_not_mount_stops_the_run(
        self, monkeypatch, capsys,
    ):
        tool = _load_tool()

        def refuse(disk):
            raise SystemExit(1)

        monkeypatch.setattr(tool, "get_mount_point", refuse)
        # diskutil says there is a filesystem, but it would not mount.
        monkeypatch.setattr(tool, "_run", _fake_run(PARTITIONED_LAYOUT))
        with pytest.raises(SystemExit):
            tool.read_existing_own_txt("/dev/disk4")
        assert "could not be inspected" in capsys.readouterr().out

    def test_main_does_not_erase_a_volume_it_could_not_mount(
        self, monkeypatch,
    ):
        tool = _load_tool()
        formatted = []

        def refuse(disk):
            raise SystemExit(1)

        monkeypatch.setattr(tool, "download_chdk", lambda: None)
        monkeypatch.setattr(tool, "find_removable_disks", lambda: [])
        monkeypatch.setattr(tool, "pick_disk", lambda disks: "/dev/disk4")
        monkeypatch.setattr(tool, "get_mount_point", refuse)
        monkeypatch.setattr(tool, "_run", _fake_run(PARTITIONED_LAYOUT))
        monkeypatch.setattr(
            tool, "format_card", lambda disk: formatted.append(disk),
        )
        with pytest.raises(SystemExit):
            tool.main()
        assert formatted == []

    def test_a_layout_that_cannot_be_inspected_stops_the_run(
        self, monkeypatch,
    ):
        tool = _load_tool()

        def refuse(disk):
            raise SystemExit(1)

        monkeypatch.setattr(tool, "get_mount_point", refuse)
        # diskutil itself failed: the card is not demonstrably blank.
        monkeypatch.setattr(tool, "_run", _fake_run(returncode=1))
        with pytest.raises(SystemExit):
            tool.read_existing_own_txt("/dev/disk4")

    def test_unparseable_layout_stops_the_run(self, monkeypatch):
        tool = _load_tool()

        def refuse(disk):
            raise SystemExit(1)

        monkeypatch.setattr(tool, "get_mount_point", refuse)
        monkeypatch.setattr(tool, "_run", _fake_run(stdout="not a plist"))
        with pytest.raises(SystemExit):
            tool.read_existing_own_txt("/dev/disk4")

    def test_an_apfs_container_counts_as_a_filesystem(self, monkeypatch):
        tool = _load_tool()
        layout = {
            "AllDisksAndPartitions": [
                {
                    "Content": "Apple_APFS_Container",
                    "DeviceIdentifier": "disk4",
                    "APFSVolumes": [{"DeviceIdentifier": "disk4s1"}],
                    "Partitions": [],
                },
            ],
        }
        monkeypatch.setattr(tool, "_run", _fake_run(layout))
        assert tool._classify_card("/dev/disk4") == tool.CARD_HAS_FILESYSTEM

    def test_a_blank_layout_is_classified_blank(self, monkeypatch):
        tool = _load_tool()
        monkeypatch.setattr(tool, "_run", _fake_run(_blank_layout()))
        assert tool._classify_card("/dev/disk4") == tool.CARD_BLANK

    def test_a_partitioned_layout_holds_a_filesystem(self, monkeypatch):
        tool = _load_tool()
        monkeypatch.setattr(tool, "_run", _fake_run(PARTITIONED_LAYOUT))
        assert tool._classify_card("/dev/disk4") == tool.CARD_HAS_FILESYSTEM

    def test_an_empty_layout_list_is_unknown_not_blank(self, monkeypatch):
        tool = _load_tool()
        monkeypatch.setattr(tool, "_run", _fake_run({"AllDisksAndPartitions": []}))
        assert tool._classify_card("/dev/disk4") == tool.CARD_UNKNOWN

    def test_an_empty_entry_is_unknown_not_blank(self, monkeypatch):
        tool = _load_tool()
        # Nothing here says the device is empty; it says nothing at all.
        monkeypatch.setattr(tool, "_run", _fake_run({
            "AllDisksAndPartitions": [{}],
        }))
        assert tool._classify_card("/dev/disk4") == tool.CARD_UNKNOWN

    def test_an_entry_of_unrecognized_fields_is_unknown(self, monkeypatch):
        tool = _load_tool()
        monkeypatch.setattr(tool, "_run", _fake_run({
            "AllDisksAndPartitions": [
                {"SomeFutureKey": "whatever", "DeviceIdentifier": "disk4"},
            ],
        }))
        assert tool._classify_card("/dev/disk4") == tool.CARD_UNKNOWN

    def test_a_payload_about_another_device_is_unknown(self, monkeypatch):
        tool = _load_tool()
        # A blank layout, but not for the card we asked about.
        monkeypatch.setattr(tool, "_run", _fake_run(_blank_layout()))
        assert tool._classify_card("/dev/disk9") == tool.CARD_UNKNOWN

    def test_a_bare_disk_identifier_is_accepted(self, monkeypatch):
        tool = _load_tool()
        monkeypatch.setattr(tool, "_run", _fake_run(_blank_layout()))
        assert tool._classify_card("disk4") == tool.CARD_BLANK

    def test_an_unmountable_card_with_a_filesystem_stops_the_run(
        self, monkeypatch,
    ):
        tool = _load_tool()
        # diskutil reports no mount point; the layout shows a filesystem.
        # The old guessed path made this read as a clean absence.
        monkeypatch.setattr(tool, "_run", _dispatching_run(
            info={"DeviceIdentifier": "disk4s1"},
            layout=PARTITIONED_LAYOUT,
        ))
        with pytest.raises(SystemExit):
            tool.read_existing_own_txt("/dev/disk4")

    def test_main_does_not_erase_a_card_it_could_not_mount(self, monkeypatch):
        tool = _load_tool()
        formatted = []
        monkeypatch.setattr(tool, "download_chdk", lambda: None)
        monkeypatch.setattr(tool, "find_removable_disks", lambda: [])
        monkeypatch.setattr(tool, "pick_disk", lambda disks: "/dev/disk4")
        monkeypatch.setattr(tool, "_run", _dispatching_run(
            info={"DeviceIdentifier": "disk4s1"},
            layout=PARTITIONED_LAYOUT,
        ))
        monkeypatch.setattr(
            tool, "format_card", lambda disk: formatted.append(disk),
        )
        with pytest.raises(SystemExit):
            tool.main()
        assert formatted == []

    def test_a_mounted_card_with_no_own_txt_has_no_id(
        self, tmp_path, monkeypatch,
    ):
        tool = _load_tool()
        monkeypatch.setattr(tool, "get_mount_point", lambda disk: str(tmp_path))
        monkeypatch.setattr(tool, "_run", _fake_run(PARTITIONED_LAYOUT))
        assert tool.read_existing_own_txt("/dev/disk4") == (None, None)

    def test_an_unreadable_own_txt_stops_rather_than_reporting_no_id(
        self, tmp_path, monkeypatch,
    ):
        tool = _load_tool()
        # A path that exists but cannot be read as a file: not absence.
        (tmp_path / "OWN.TXT").mkdir()
        monkeypatch.setattr(tool, "get_mount_point", lambda disk: str(tmp_path))
        with pytest.raises(SystemExit):
            tool.read_existing_own_txt("/dev/disk4")

    def test_main_does_not_erase_a_card_it_could_not_check(
        self, tmp_path, monkeypatch,
    ):
        tool = _load_tool()
        (tmp_path / "OWN.TXT").mkdir()
        formatted = []
        monkeypatch.setattr(tool, "download_chdk", lambda: None)
        monkeypatch.setattr(tool, "find_removable_disks", lambda: [])
        monkeypatch.setattr(tool, "pick_disk", lambda disks: "/dev/disk4")
        monkeypatch.setattr(tool, "get_mount_point", lambda disk: str(tmp_path))
        monkeypatch.setattr(
            tool, "format_card", lambda disk: formatted.append(disk),
        )
        with pytest.raises(SystemExit):
            tool.main()
        assert formatted == []

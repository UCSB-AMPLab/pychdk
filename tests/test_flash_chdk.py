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


def _scheme_only_layout(scheme):
    """A card carrying a partition map and nothing else."""
    return {
        "AllDisksAndPartitions": [
            {
                "Content": scheme,
                "DeviceIdentifier": "disk4",
                "OSInternal": False,
                "Partitions": [],
                "Size": 15931539456,
            },
        ],
        "WholeDisks": ["disk4"],
    }


def _fat32_layout():
    """A card with the single FAT32 partition this tool creates."""
    return {
        "AllDisksAndPartitions": [
            {
                "Content": "FDisk_partition_scheme",
                "DeviceIdentifier": "disk4",
                "OSInternal": False,
                "Partitions": [
                    {
                        "Content": "DOS_FAT_32",
                        "DeviceIdentifier": "disk4s1",
                        "VolumeName": "CHDK_A2500",
                        "Size": 15931539456,
                    },
                ],
                "Size": 15931539456,
            },
        ],
        "WholeDisks": ["disk4"],
    }


def _two_volume_layout():
    """A card whose identity could live on a partition we never read."""
    return {
        "AllDisks": ["disk4", "disk4s1", "disk4s2"],
        "AllDisksAndPartitions": [
            {
                "Content": "GUID_partition_scheme",
                "DeviceIdentifier": "disk4",
                "OSInternal": False,
                "Partitions": [
                    {
                        "Content": "Microsoft Basic Data",
                        "DeviceIdentifier": "disk4s1",
                        "Size": 7965769728,
                    },
                    {
                        "Content": "Apple_HFS",
                        "DeviceIdentifier": "disk4s2",
                        "Size": 7965769728,
                    },
                ],
                "Size": 15931539456,
            },
        ],
        "VolumesFromDisks": [],
        "WholeDisks": ["disk4"],
    }


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


class TestFormatCard:
    """A format that did not come back mounted must not look like success."""

    def test_a_reported_mount_point_proceeds(self, tmp_path, monkeypatch):
        tool = _load_tool()
        monkeypatch.setattr(
            tool, "_run", _dispatching_run(info={"MountPoint": str(tmp_path)}),
        )
        assert tool.format_card("/dev/disk4") == str(tmp_path)

    def test_no_mount_point_stops_before_anything_is_extracted(
        self, monkeypatch, capsys,
    ):
        tool = _load_tool()
        monkeypatch.setattr(
            tool, "_run", _dispatching_run(info={"DeviceIdentifier": "disk4s1"}),
        )
        with pytest.raises(SystemExit):
            tool.format_card("/dev/disk4")
        out = capsys.readouterr().out
        assert "Nothing has been written" in out

    def test_a_mount_point_that_is_not_there_stops(self, monkeypatch):
        tool = _load_tool()
        monkeypatch.setattr(
            tool, "_run",
            _dispatching_run(info={"MountPoint": "/Volumes/NoSuchCard"}),
        )
        with pytest.raises(SystemExit):
            tool.format_card("/dev/disk4")

    def test_main_extracts_nothing_when_the_format_does_not_mount(
        self, monkeypatch,
    ):
        tool = _load_tool()
        extracted = []
        monkeypatch.setattr(tool, "download_chdk", lambda: None)
        monkeypatch.setattr(tool, "find_removable_disks", lambda: [])
        monkeypatch.setattr(tool, "pick_disk", lambda disks: "/dev/disk4")
        monkeypatch.setattr(tool, "_run", _dispatching_run(
            info={"DeviceIdentifier": "disk4s1"},
            layout=_blank_layout(),
        ))
        monkeypatch.setattr(
            tool, "extract_chdk",
            lambda zip_path, mount: extracted.append(mount),
        )
        with pytest.raises(SystemExit):
            tool.main()
        assert extracted == []


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
        assert "could not be mounted" in capsys.readouterr().out

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

    def test_a_nested_volume_with_no_content_is_still_a_filesystem(
        self, monkeypatch,
    ):
        tool = _load_tool()
        # The parent carries nothing but a scheme, so the nested volume
        # is the only thing that can decide. Real APFS volumes have no
        # Content of their own and name themselves instead; without the
        # name and mount-point checks this classifies as blank.
        layout = {
            "AllDisksAndPartitions": [
                {
                    "Content": "GUID_partition_scheme",
                    "DeviceIdentifier": "disk4",
                    "Size": 15931539456,
                    "Partitions": [],
                    "APFSVolumes": [
                        {
                            "DeviceIdentifier": "disk4s1",
                            "MountPoint": "/Volumes/Card",
                            "VolumeName": "Card",
                            "Size": 15931539456,
                        },
                    ],
                },
            ],
        }
        monkeypatch.setattr(tool, "_run", _fake_run(layout))
        assert tool._classify_card("/dev/disk4") == tool.CARD_HAS_FILESYSTEM

    def test_an_apfs_container_on_the_whole_disk_is_a_filesystem(
        self, monkeypatch,
    ):
        tool = _load_tool()
        # The parent-level case, separated out: Apple_APFS_Container is
        # not a partition scheme, so the entry decides on its own and
        # the nested volumes are never reached.
        layout = {
            "AllDisksAndPartitions": [
                {
                    "Content": "Apple_APFS_Container",
                    "DeviceIdentifier": "disk4",
                    "Size": 15931539456,
                },
            ],
        }
        monkeypatch.setattr(tool, "_run", _fake_run(layout))
        assert tool._classify_card("/dev/disk4") == tool.CARD_HAS_FILESYSTEM

    def test_a_volume_is_judged_by_name_or_mount_point_or_content(self):
        tool = _load_tool()
        assert tool._volume_holds_a_filesystem({"VolumeName": "Card"})
        assert tool._volume_holds_a_filesystem({"MountPoint": "/Volumes/Card"})
        assert tool._volume_holds_a_filesystem({"Content": "DOS_FAT_32"})
        assert not tool._volume_holds_a_filesystem(
            {"DeviceIdentifier": "disk4s1", "Size": 1},
        )

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

    def test_a_partition_map_with_no_partitions_is_blank(self, monkeypatch):
        tool = _load_tool()
        # A card that has been formatted and emptied still carries a
        # scheme. That is the ordinary state of a card from a camera.
        monkeypatch.setattr(tool, "_run", _fake_run(_scheme_only_layout(
            "FDisk_partition_scheme",
        )))
        assert tool._classify_card("/dev/disk4") == tool.CARD_BLANK

    def test_a_guid_scheme_with_no_partitions_is_blank(self, monkeypatch):
        tool = _load_tool()
        monkeypatch.setattr(tool, "_run", _fake_run(_scheme_only_layout(
            "GUID_partition_scheme",
        )))
        assert tool._classify_card("/dev/disk4") == tool.CARD_BLANK

    def test_a_device_with_no_content_at_all_is_blank(self, monkeypatch):
        tool = _load_tool()
        monkeypatch.setattr(tool, "_run", _fake_run(_blank_layout()))
        assert tool._classify_card("/dev/disk4") == tool.CARD_BLANK

    def test_one_fat32_partition_holds_a_filesystem(self, monkeypatch):
        tool = _load_tool()
        monkeypatch.setattr(tool, "_run", _fake_run(_fat32_layout()))
        assert tool._classify_card("/dev/disk4") == tool.CARD_HAS_FILESYSTEM

    def test_a_freshly_formatted_card_can_still_be_flashed(
        self, tmp_path, monkeypatch,
    ):
        tool = _load_tool()
        monkeypatch.setattr(tool, "get_mount_point", lambda disk: str(tmp_path))
        monkeypatch.setattr(tool, "_run", _fake_run(_fat32_layout()))
        assert tool.read_existing_own_txt("/dev/disk4") == (None, None)

    def test_an_empty_partitioned_card_is_not_refused(self, monkeypatch):
        tool = _load_tool()
        # The common case: a card out of a camera or a shop. Refusing
        # this made the tool unusable on the first card anyone picked up.
        monkeypatch.setattr(tool, "_run", _fake_run(_scheme_only_layout(
            "FDisk_partition_scheme",
        )))
        assert tool.read_existing_own_txt("/dev/disk4") == (None, None)

    def test_a_volume_we_cannot_characterize_is_unknown(self, monkeypatch):
        tool = _load_tool()
        # Something is under there, but nothing says what it is.
        monkeypatch.setattr(tool, "_run", _fake_run({
            "AllDisksAndPartitions": [
                {
                    "Content": "FDisk_partition_scheme",
                    "DeviceIdentifier": "disk4",
                    "Size": 15931539456,
                    "Partitions": [{"DeviceIdentifier": "disk4s1"}],
                },
            ],
        }))
        assert tool._classify_card("/dev/disk4") == tool.CARD_UNKNOWN

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
        monkeypatch.setattr(tool, "_run", _fake_run(PARTITIONED_LAYOUT))
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
        monkeypatch.setattr(tool, "_run", _fake_run(PARTITIONED_LAYOUT))
        monkeypatch.setattr(
            tool, "format_card", lambda disk: formatted.append(disk),
        )
        with pytest.raises(SystemExit):
            tool.main()
        assert formatted == []


class TestOnlyAnInspectableLayoutMayBeErased:
    """A mountable first partition is not an inspection of the card."""

    def test_a_second_volume_stops_before_any_erase(
        self, tmp_path, monkeypatch,
    ):
        tool = _load_tool()
        formatted = []
        monkeypatch.setattr(tool, "download_chdk", lambda: None)
        monkeypatch.setattr(tool, "find_removable_disks", lambda: [])
        monkeypatch.setattr(tool, "pick_disk", lambda disks: "/dev/disk4")
        monkeypatch.setattr(tool, "get_mount_point", lambda disk: str(tmp_path))
        monkeypatch.setattr(tool, "_run", _fake_run(_two_volume_layout()))
        monkeypatch.setattr(
            tool, "format_card", lambda disk: formatted.append(disk),
        )
        with pytest.raises(SystemExit):
            tool.main()
        assert formatted == []

    def test_a_mountable_s1_with_another_volume_is_not_an_absence(
        self, tmp_path, monkeypatch, capsys,
    ):
        tool = _load_tool()
        # s1 mounts and has no OWN.TXT — but disk4s2 does, and we never
        # look there, so this is not evidence the card has no identity.
        monkeypatch.setattr(tool, "get_mount_point", lambda disk: str(tmp_path))
        monkeypatch.setattr(tool, "_run", _fake_run(_two_volume_layout()))
        with pytest.raises(SystemExit):
            tool.read_existing_own_txt("/dev/disk4")
        assert "cannot inspect" in capsys.readouterr().out

    def test_a_single_inspectable_volume_proceeds(
        self, tmp_path, monkeypatch,
    ):
        tool = _load_tool()
        (tmp_path / "OWN.TXT").write_text("EVEN\nid=abc123def456\n")
        monkeypatch.setattr(tool, "get_mount_point", lambda disk: str(tmp_path))
        monkeypatch.setattr(tool, "_run", _fake_run(PARTITIONED_LAYOUT))
        assert tool.read_existing_own_txt("/dev/disk4") == (
            "EVEN", "abc123def456",
        )

    def test_a_blank_layout_proceeds_with_no_identity(self, monkeypatch):
        tool = _load_tool()
        monkeypatch.setattr(tool, "_run", _fake_run(_blank_layout()))
        assert tool.read_existing_own_txt("/dev/disk4") == (None, None)

    def test_a_filesystem_on_the_whole_device_is_not_inspectable(
        self, tmp_path, monkeypatch,
    ):
        tool = _load_tool()
        monkeypatch.setattr(tool, "get_mount_point", lambda disk: str(tmp_path))
        monkeypatch.setattr(tool, "_run", _fake_run({
            "AllDisksAndPartitions": [
                {
                    "Content": "Apple_HFS",
                    "DeviceIdentifier": "disk4",
                    "Size": 15931539456,
                },
            ],
        }))
        with pytest.raises(SystemExit):
            tool.read_existing_own_txt("/dev/disk4")


def _answerer(answers):
    """Stand in for input(), recording every prompt it is shown."""
    remaining = list(answers)
    prompts = []

    def ask(prompt=""):
        prompts.append(prompt)
        if not remaining:
            raise AssertionError(f"unexpected prompt: {prompt!r}")
        return remaining.pop(0)

    ask.prompts = prompts
    return ask


def _disks(count):
    """`count` removable disks, as find_removable_disks reports them."""
    return [
        {
            "disk": f"/dev/disk{4 + i}",
            "name": f"Card {i}",
            "size_gb": 15.9 + i,
        }
        for i in range(count)
    ]


class TestNoDiskIsErasedWithoutAConfirmation:
    """The multi-disk path is where picking wrong is most likely.

    It was also the only path that returned before the confirmation was
    asked, so choosing from a list erased whatever was chosen. Every
    path has to ask, and the question has to name the disk: the tool
    accepts any physical removable medium, which on a Mac includes an
    external USB drive that is not an SD card at all.
    """

    def test_choosing_from_several_disks_is_still_confirmed(self, monkeypatch):
        tool = _load_tool()
        ask = _answerer(["1", "y"])
        monkeypatch.setattr("builtins.input", ask)
        assert tool.pick_disk(_disks(3)) == "/dev/disk5"
        assert any("ERASE" in p for p in ask.prompts)

    def test_declining_after_choosing_from_several_disks_stops(
        self, monkeypatch,
    ):
        tool = _load_tool()
        monkeypatch.setattr("builtins.input", _answerer(["1", "n"]))
        with pytest.raises(SystemExit) as excinfo:
            tool.pick_disk(_disks(3))
        assert excinfo.value.code == 0

    def test_saying_nothing_after_choosing_from_several_disks_stops(
        self, monkeypatch,
    ):
        tool = _load_tool()
        monkeypatch.setattr("builtins.input", _answerer(["2", ""]))
        with pytest.raises(SystemExit) as excinfo:
            tool.pick_disk(_disks(3))
        assert excinfo.value.code == 0

    def test_the_multi_disk_prompt_names_the_disk_it_will_erase(
        self, monkeypatch,
    ):
        tool = _load_tool()
        ask = _answerer(["2", "y"])
        monkeypatch.setattr("builtins.input", ask)
        # The returned disk is asserted, not just the prompt. A prompt that
        # names one disk while the function returns another would approve an
        # erase of something the operator never saw, and is the failure this
        # whole class exists to prevent.
        assert tool.pick_disk(_disks(3)) == "/dev/disk6"
        erase = [p for p in ask.prompts if "ERASE" in p]
        assert erase, ask.prompts
        assert "/dev/disk6" in erase[0]
        assert "Card 2" in erase[0]
        assert "17.9" in erase[0]

    def test_the_single_disk_prompt_names_the_disk_it_will_erase(
        self, monkeypatch,
    ):
        tool = _load_tool()
        ask = _answerer(["y"])
        monkeypatch.setattr("builtins.input", ask)
        assert tool.pick_disk(_disks(1)) == "/dev/disk4"
        erase = [p for p in ask.prompts if "ERASE" in p]
        assert erase, ask.prompts
        assert "/dev/disk4" in erase[0]
        assert "Card 0" in erase[0]
        assert "15.9" in erase[0]

    def test_an_unreadable_choice_stops_before_the_confirmation(
        self, monkeypatch,
    ):
        tool = _load_tool()
        monkeypatch.setattr("builtins.input", _answerer(["banana"]))
        with pytest.raises(SystemExit) as excinfo:
            tool.pick_disk(_disks(3))
        assert excinfo.value.code == 1

    def test_a_negative_choice_is_not_quietly_counted_from_the_end(
        self, monkeypatch,
    ):
        """A typed '-1' must not silently select a disk nobody named."""
        tool = _load_tool()
        monkeypatch.setattr("builtins.input", _answerer(["-1"]))
        with pytest.raises(SystemExit) as excinfo:
            tool.pick_disk(_disks(3))
        assert excinfo.value.code == 1

    def test_main_erases_nothing_when_the_confirmation_is_declined(
        self, monkeypatch,
    ):
        tool = _load_tool()
        formatted = []
        monkeypatch.setattr(tool, "download_chdk", lambda: None)
        monkeypatch.setattr(tool, "find_removable_disks", lambda: _disks(3))
        monkeypatch.setattr("builtins.input", _answerer(["1", "n"]))
        monkeypatch.setattr(tool, "_run", _fake_run(PARTITIONED_LAYOUT))
        monkeypatch.setattr(
            tool, "format_card", lambda disk: formatted.append(disk),
        )
        with pytest.raises(SystemExit) as excinfo:
            tool.main()
        assert excinfo.value.code == 0
        assert formatted == []

    def test_main_erases_exactly_the_disk_that_was_confirmed(
        self, monkeypatch,
    ):
        """The confirmed disk and the erased disk have to be the same one.

        pick_disk names a disk in its prompt and returns a disk, and nothing
        downstream re-checks that those agree: format_card erases whatever it
        is handed. So this follows the chosen disk all the way from the
        selection to the erase, through main(), rather than stopping at the
        prompt text.
        """
        tool = _load_tool()
        formatted = []
        monkeypatch.setattr(tool, "download_chdk", lambda: None)
        monkeypatch.setattr(tool, "find_removable_disks", lambda: _disks(3))
        ask = _answerer(["2", "y"])
        monkeypatch.setattr("builtins.input", ask)
        monkeypatch.setattr(tool, "_run", _fake_run(PARTITIONED_LAYOUT))
        # The identity salvage and card classification are other tests'
        # subject. Stubbed so the only thing this one can fail on is which
        # disk reaches format_card.
        monkeypatch.setattr(
            tool, "read_existing_own_txt", lambda disk: (None, None),
        )
        monkeypatch.setattr(
            tool, "format_card", lambda disk: formatted.append(disk) or "/Volumes/X",
        )
        monkeypatch.setattr(tool, "extract_chdk", lambda *a, **k: None)
        monkeypatch.setattr(tool, "patch_boot_sector", lambda *a, **k: None)
        monkeypatch.setattr(tool, "get_mount_point", lambda *a, **k: "/Volumes/X")
        monkeypatch.setattr(tool, "write_camera_side", lambda *a, **k: None)
        monkeypatch.setattr(tool, "eject_card", lambda *a, **k: None)
        try:
            tool.main()
        except SystemExit as exc:
            assert exc.code in (0, None), exc.code
        erase = [p for p in ask.prompts if "ERASE" in p]
        assert erase, ask.prompts
        assert "/dev/disk6" in erase[0], erase[0]
        assert formatted == ["/dev/disk6"], (formatted, erase)

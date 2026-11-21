# SPDX-License-Identifier: MIT
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import omarchy_macho as macho  # noqa: E402
from macho_fixture import BASE, PAGE, macho_stage1  # noqa: E402

RAW = b"aura stage 1" + b"\0" * 64


class MachOLayoutTests(unittest.TestCase):
    def test_an_aura_boot_object_passes_and_reports_its_kcrz(self):
        image = macho_stage1(RAW)
        segments, entry = macho.layout(image)
        self.assertEqual(entry, BASE + PAGE + 0x800)
        self.assertEqual(macho.read_only_size(image), PAGE + 2 * PAGE)
        self.assertEqual(macho.text_exec_range(image), (PAGE, 3 * PAGE))
        self.assertEqual(sorted(segments), sorted(macho.REQUIRED))

    def test_what_kmutil_or_iboot_would_mishandle_is_refused(self):
        cases = {
            "raw image": RAW,
            "not a fileset": macho_stage1(RAW, filetype=2),
            "SPTM segment": macho_stage1(RAW, extra_segments=[("__TEXT_BOOT_EXEC", PAGE)]),
            "entry outside __TEXT_EXEC": macho_stage1(RAW, entry_offset=3 * PAGE),
            "truncated": macho_stage1(RAW)[:-1],
        }
        for name, image in cases.items():
            with self.subTest(name), self.assertRaises(macho.MachOError):
                macho.layout(image)

    def test_a_missing_required_segment_is_refused(self):
        image = bytearray(macho_stage1(RAW))
        index = image.index(b"__PRELINK_INFO")
        image[index:index + 14] = b"__PRELINK_INFX"
        with self.assertRaisesRegex(macho.MachOError, "no __PRELINK_INFO"):
            macho.layout(bytes(image))


class ExecuteWindowTests(unittest.TestCase):
    """Recovery's expandExecuteWindow rules, checked before any disk change."""

    def test_an_aura_boot_object_can_be_padded(self):
        macho.check_execute_window(macho_stage1(RAW))

    def test_what_recovery_cannot_pad_is_refused(self):
        cases = {
            "does not enter aura at its entry point": macho_stage1(RAW, entry_offset=0x804),
            "does not start with aura": macho_stage1(RAW, load_header=None),
            "does not fit the room kept for it": macho_stage1(RAW, load_size=(16 << 20) + 1),
            "cannot be moved": macho_stage1(RAW, commands_extra=struct.pack("<II", 0x26, 16) + bytes(8)),
            "has a symbol table": macho_stage1(RAW, commands_extra=struct.pack("<II", 0x2, 24) + b"\1" + bytes(15)),
        }
        for message, image in cases.items():
            with self.subTest(message), self.assertRaisesRegex(macho.MachOError, message):
                macho.check_execute_window(image)

    def test_an_empty_symbol_table_is_allowed(self):
        macho.check_execute_window(macho_stage1(RAW, commands_extra=struct.pack("<II", 0x2, 24) + bytes(16)))


if __name__ == "__main__":
    unittest.main()

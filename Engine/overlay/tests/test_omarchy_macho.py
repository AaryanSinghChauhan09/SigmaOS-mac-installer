# SPDX-License-Identifier: MIT
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()

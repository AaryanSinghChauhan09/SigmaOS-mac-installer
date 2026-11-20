#!/usr/bin/env python3
"""The installed system's pacman.conf names every repository Omarchy's packages come from;
[aur], which carries none of them, may be left out."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "builder"))
from verify_installed_system import REQUIRED_PACKAGES, Verification, check_packages, check_pacman

REPOSITORIES = ("omarchy", "asahi-alarm", "core", "extra", "alarm", "aur")


class CheckPacmanTest(unittest.TestCase):
    def required_repositories(self, names):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "etc").mkdir()
            (root / "etc/pacman.conf").write_text(
                "[options]\nArchitecture = aarch64\n"
                + "".join(f"\n[{name}]\nInclude = /etc/pacman.d/mirrorlist\n" for name in names))
            result = Verification()
            check_pacman(result, root)
            return result.checks["pacman-required-repositories"]

    def test_every_repository_passes(self):
        self.assertEqual(self.required_repositories(REPOSITORIES)["result"], "passed")

    def test_aur_may_be_left_out(self):
        self.assertEqual(self.required_repositories(REPOSITORIES[:-1])["result"], "passed")

    def test_a_missing_repository_fails(self):
        check = self.required_repositories(tuple(name for name in REPOSITORIES if name != "extra"))
        self.assertEqual(check, {"result": "failed", "detail": "missing repositories: extra"})



class CheckPackagesTest(unittest.TestCase):
    def required_present(self, names):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in names:
                entry = root / "var/lib/pacman/local" / f"{name}-1-1"
                entry.mkdir(parents=True)
                (entry / "desc").write_text(f"%NAME%\n{name}\n\n%VERSION%\n1-1\n\n")
            result = Verification()
            check_packages(result, root)
            return result.checks["packages-required-present"]

    def test_every_required_package_passes(self):
        self.assertEqual(self.required_present(REQUIRED_PACKAGES)["result"], "passed")

    def test_aurora_fwextract_is_required(self):
        check = self.required_present(tuple(n for n in REQUIRED_PACKAGES if n != "aurora-fwextract"))
        self.assertEqual(check, {"result": "failed", "detail": "missing packages: aurora-fwextract"})

    def test_an_older_image_carries_asahi_fwextract(self):
        names = tuple(n for n in REQUIRED_PACKAGES if n != "aurora-fwextract") + ("asahi-fwextract",)
        self.assertEqual(self.required_present(names)["result"], "passed")


if __name__ == "__main__":
    unittest.main()

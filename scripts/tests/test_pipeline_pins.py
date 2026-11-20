#!/usr/bin/env python3
"""The pipeline pins: an app or engine release moves
.github/mac-release-pipeline.json to itself and nothing else, never the app
floor, and mac-pipeline-pins.yml opens the pull request only from a published
release, on main.
"""
from __future__ import annotations

import atexit
import base64
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "mac-pipeline-pins.yml"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pins = load("pipeline_pins", ROOT / "scripts" / "pipeline_pins.py")
catalog = pins.catalog
LOCK = json.loads(catalog.SOURCE_LOCK.read_text())
NAMES = catalog.identity()
ENGINE = LOCK["downstream_overlay"]["version"]
ARCHIVE = LOCK["validation_artifact"]
COMMIT = "b" * 40
DMG_SHA = "c" * 64
LOCKED_DIGEST = f"sha256:{ARCHIVE['sha256']}"
HELD_DIGEST = pins.LANE_HOLDS["rc"]["digest"]
OTHER_DIGEST = "sha256:" + "e" * 64

try:
    OPENSSL = catalog.find_openssl()
except catalog.Refusal:
    OPENSSL = None


def make_key() -> tuple[bytes, bytes]:
    """A throwaway Ed25519 key: (seed, public key) as raw bytes."""
    der = subprocess.run([OPENSSL, "genpkey", "-algorithm", "ed25519", "-outform", "DER"],
                         capture_output=True, check=True).stdout
    seed = der[-32:]
    return seed, catalog.openssl_public_key(OPENSSL, seed)


_KEY: tuple | None = None


def key_material() -> tuple[bytes, bytes, Path]:
    """The suite's throwaway Ed25519 key, made on first use."""
    global _KEY
    if _KEY is None:
        directory = Path(tempfile.mkdtemp(prefix="omarchy-pipeline-pins-key-"))
        atexit.register(shutil.rmtree, directory, True)
        seed, trust_root = make_key()
        _KEY = (seed, trust_root, directory)
    return _KEY


def sign(document: dict, seed: bytes, directory: Path) -> bytes:
    """The signed envelope bytes of a catalog document under SEED."""
    payload = directory / "catalog.json"
    payload.write_text(json.dumps(document))
    signature = catalog.openssl_sign(OPENSSL, seed, payload)
    inner = base64.b64encode(payload.read_bytes())
    envelope = {"schema_version": 1, "catalog": inner.decode(),
                "signature": base64.b64encode(signature).decode()}
    return json.dumps(envelope).encode()


def app_release(version: str = "2.3.9", **changes) -> dict:
    dmg = f"{NAMES['INSTALLER_FILE_STEM']}-{version}.dmg"
    release = {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": True,
        "html_url": f"https://github.com/omacom/omarchy-mac-installer/releases/tag/v{version}",
        "assets": [
            {"name": dmg, "digest": f"sha256:{DMG_SHA}", "size": 25_000_000},
            {"name": f"{dmg}.sha256", "digest": f"sha256:{'d' * 64}", "size": 98},
        ],
    }
    return {**release, **changes}


def engine_release(version: str = ENGINE, **changes) -> dict:
    release = {
        "tag_name": f"engine-{version}",
        "draft": False,
        "prerelease": True,
        "html_url": f"https://github.com/omacom/omarchy-mac-installer/releases/tag/engine-{version}",
        "assets": [{"name": ARCHIVE["filename"], "digest": f"sha256:{ARCHIVE['sha256']}",
                    "size": ARCHIVE["size_bytes"]}],
    }
    return {**release, **changes}


def config(minimum: str = "2.3.8", latest: str = "2.3.8", revision: str = "a" * 40) -> dict:
    return {
        "engine": {"downstreamRevision": revision},
        "installer": {
            "minimumVersion": minimum,
            "latestVersion": latest,
            "downloadURL": f"{NAMES['INSTALLER_CATALOG_BASE']}/installer/{latest}/"
                           f"{NAMES['INSTALLER_FILE_STEM']}-{latest}.dmg",
        },
    }


class Kind(unittest.TestCase):
    def test_only_app_and_engine_releases_move_pins(self):
        self.assertEqual(pins.kind("v2.3.9"), "app")
        self.assertEqual(pins.kind(f"engine-{ENGINE}"), "engine")
        for tag in ("catalog-edge", "catalog-rc", "omarchy-mac-edge-2026.10.14-0123456789ab-0123456789ab",
                    "v2.3", "v2.3.9-rc1", "engine-", "recovery-1"):
            with self.subTest(tag):
                self.assertIsNone(pins.kind(tag))


class AppPin(unittest.TestCase):
    def test_the_pin_is_the_dmg_under_the_catalog_base(self):
        pin = pins.app_pin(app_release())
        self.assertEqual((pin["version"], pin["digest"]), ("2.3.9", DMG_SHA))
        self.assertEqual(pin["downloadURL"],
                         f"{NAMES['INSTALLER_CATALOG_BASE']}/installer/2.3.9/{NAMES['INSTALLER_FILE_STEM']}-2.3.9.dmg")

    def test_a_release_that_is_not_a_published_notarized_dmg_is_refused(self):
        release = app_release()
        dmg, checksum = release["assets"]
        for broken in (app_release(draft=True),
                       app_release(assets=[checksum]),
                       app_release(assets=[dmg]),
                       app_release(assets=[{**dmg, "digest": None}, checksum]),
                       app_release(assets=[{**dmg, "name": "Other-2.3.9.dmg"}, checksum])):
            with self.subTest(broken=broken), self.assertRaises(pins.Refusal):
                pins.app_pin(broken)


class EnginePin(unittest.TestCase):
    def test_the_pin_is_the_release_commit_of_the_locked_engine(self):
        pin = pins.engine_pin(engine_release(), COMMIT, LOCK)
        self.assertEqual((pin["version"], pin["digest"], pin["downstreamRevision"]),
                         (ENGINE, ARCHIVE["sha256"], COMMIT))

    def test_an_engine_main_does_not_pin_is_refused(self):
        archive = engine_release()["assets"][0]
        cases = {
            "another version": (engine_release("v0.9.2-omarchy.99"), COMMIT),
            "another archive": (engine_release(assets=[{**archive, "digest": f"sha256:{'0' * 64}"}]), COMMIT),
            "another size": (engine_release(assets=[{**archive, "size": 1}]), COMMIT),
            "no archive": (engine_release(assets=[]), COMMIT),
            "a draft": (engine_release(draft=True), COMMIT),
            "no commit": (engine_release(), "nope"),
        }
        for name, (release, commit) in cases.items():
            with self.subTest(name), self.assertRaises(pins.Refusal):
                pins.engine_pin(release, commit, LOCK)


class Bump(unittest.TestCase):
    def test_an_app_moves_latest_and_its_url_only(self):
        old = {**config(), "extra": {"minimumAppVersion": "2.3.0"}}
        new = pins.bump(old, pins.app_pin(app_release()))
        self.assertEqual(new["installer"]["latestVersion"], "2.3.9")
        self.assertTrue(new["installer"]["downloadURL"].endswith(f"/installer/2.3.9/{NAMES['INSTALLER_FILE_STEM']}-2.3.9.dmg"))
        self.assertEqual(new["installer"]["minimumVersion"], old["installer"]["minimumVersion"])
        self.assertEqual(new["engine"], old["engine"])
        self.assertEqual(new["extra"], old["extra"])
        self.assertEqual(old, {**config(), "extra": {"minimumAppVersion": "2.3.0"}})

    def test_an_older_app_moves_nothing(self):
        old = config(latest="2.4.0")
        self.assertEqual(pins.bump(old, pins.app_pin(app_release("2.3.9"))), old)

    def test_an_app_needs_a_floor_set_by_hand(self):
        with self.assertRaises(pins.Refusal):
            pins.bump({**config(), "installer": None}, pins.app_pin(app_release()))

    def test_an_engine_moves_its_revision_only(self):
        old = config()
        new = pins.bump(old, pins.engine_pin(engine_release(), COMMIT, LOCK))
        self.assertEqual(new, {**old, "engine": {"downstreamRevision": COMMIT}})

    def test_the_catalog_refuses_a_bump_under_the_engines_floor(self):
        # The floor stays where it is, so a bump the catalog refuses opens nothing.
        floor = LOCK["downstream_overlay"].get("minimum_app_version", "2.1.0")
        if catalog.version_tuple(floor) <= (2, 1, 0):
            self.skipTest("the pinned engine has no floor above every catalog's")
        new = pins.bump(config(minimum="2.1.0"), pins.app_pin(app_release()))
        with self.assertRaisesRegex(pins.Refusal, rf"needs app {re.escape(floor)} or later.*"
                                                  "minimumVersion moves only in a pull request of its own"):
            pins.check(new)
        pins.check(pins.bump(config(minimum=floor), pins.app_pin(app_release())))


class PublishedChannels(unittest.TestCase):
    """An app pin moves the installer every channel offers. The channel
    catalogs must already pin the engine digest the lock names: installer
    2.3.16 shipped while catalog-stable still pinned engine 20261022, and
    every install ran that older engine's Recovery manifest check."""

    def setUp(self):
        if OPENSSL is None:
            self.skipTest("OpenSSL 3 is required to sign the fixture catalogs")

    @classmethod
    def document(cls, digest: str, sequence: int = 1) -> dict:
        return {"schemaVersion": 4, "sequence": sequence,
                "models": [{"deviceIdentifier": "apple,j274", "engineDigest": digest}]}

    @classmethod
    def envelope(cls, digest: str, seed=None) -> bytes:
        own_seed, _root, directory = key_material()
        return sign(cls.document(digest), own_seed if seed is None else seed, directory)

    @classmethod
    def foreign_key(cls) -> bytes:
        other, _pub = make_key()
        return other

    @classmethod
    def live_locked(cls, channel: str) -> bytes:
        return cls.envelope(LOCKED_DIGEST)

    def check(self, live):
        pins.check(config(), pins.app_pin(app_release()), live=live, trust_root=key_material()[1])

    def test_an_app_bump_refuses_while_a_channel_pins_another_engine(self):
        live = lambda channel: self.envelope(OTHER_DIGEST) if channel == "stable" else self.live_locked(channel)
        with self.assertRaisesRegex(pins.Refusal, "catalog-stable pins engine digest"):
            self.check(live)

    def test_an_app_bump_passes_when_the_channels_pin_the_locked_digest(self):
        self.check(self.live_locked)

    def test_an_app_bump_refuses_a_missing_catalog(self):
        live = lambda channel: None if channel == "rc" else self.live_locked(channel)
        with self.assertRaisesRegex(pins.Refusal, "catalog-rc is missing.*releases/download/catalog-rc"):
            self.check(live)

    def test_an_app_bump_refuses_a_catalog_signed_by_another_key(self):
        live = lambda channel: self.envelope(LOCKED_DIGEST, seed=self.foreign_key())
        with self.assertRaisesRegex(pins.Refusal, "catalog-rc does not verify"):
            self.check(live)

    def test_an_app_bump_refuses_a_tampered_envelope(self):
        envelope = json.loads(self.envelope(OTHER_DIGEST))
        edited = json.loads(base64.b64decode(envelope["catalog"]))
        edited["models"][0]["engineDigest"] = LOCKED_DIGEST
        envelope["catalog"] = base64.b64encode(json.dumps(edited).encode()).decode()
        tampered = json.dumps(envelope).encode()
        with self.assertRaisesRegex(pins.Refusal, "catalog-rc does not verify"):
            self.check(lambda channel: tampered)

    def test_an_app_bump_refuses_malformed_models(self):
        broken = [
            {"models": []},
            {"models": ["not an object"]},
            {"models": [{"deviceIdentifier": "apple,j274"}]},
            {"models": [{"engineDigest": 1}]},
            {"models": [{"engineDigest": "sha256:short"}]},
            {"models": [{"engineDigest": LOCKED_DIGEST}, {"engineDigest": OTHER_DIGEST}]},
        ]
        for document in broken:
            bad = {"schemaVersion": 4, "sequence": 1, **document}
            envelope = sign(bad, key_material()[0], key_material()[2])
            with self.subTest(document), self.assertRaises(pins.Refusal):
                self.check(lambda channel: envelope)

    def test_the_rc_hold_passes_with_a_notice(self):
        live = lambda channel: self.envelope(HELD_DIGEST) if channel == "rc" else self.live_locked(channel)
        with redirect_stderr(io.StringIO()) as err:
            self.check(live)
        self.assertIn("catalog-rc is on the recorded hold", err.getvalue())
        self.assertIn(HELD_DIGEST, err.getvalue())
        self.assertIn(catalog.identity()["INSTALLER_GITHUB_RELEASE_LOGIN"], err.getvalue())

    def test_rc_refuses_a_digest_that_is_neither_lock_nor_hold(self):
        live = lambda channel: self.envelope(OTHER_DIGEST) if channel == "rc" else self.live_locked(channel)
        with self.assertRaisesRegex(pins.Refusal, "catalog-rc pins engine digest.*nor its recorded hold"):
            self.check(live)

    def test_stable_refuses_the_rc_hold_digest(self):
        live = lambda channel: self.envelope(HELD_DIGEST) if channel == "stable" else self.live_locked(channel)
        with self.assertRaisesRegex(pins.Refusal, "catalog-stable pins engine digest"):
            self.check(live)

    def test_the_rc_hold_matches_the_lane_holds_table(self):
        rows = [line for line in (ROOT / "docs" / "catalog-release.md").read_text().splitlines()
                if line.startswith("| rc |")]
        self.assertEqual(len(rows), 1, "docs/catalog-release.md must record exactly one rc hold")
        hold = pins.LANE_HOLDS["rc"]
        for value in (hold["engine"], hold["digest"], hold["owner"], hold["since"], hold["reason"]):
            self.assertIn(value, rows[0])

    def test_an_engine_bump_never_reads_the_published_channels(self):
        def live(channel):
            raise AssertionError("the published catalogs are not an engine pin's business")
        pins.check(config(), pins.engine_pin(engine_release(), COMMIT, LOCK),
                   live=live, trust_root=key_material()[1])


class Main(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="omarchy-pipeline-pins-"))
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / ".github").mkdir()
        (self.root / "scripts").mkdir()
        self.config = json.loads((ROOT / pins.CONFIG).read_text())
        (self.root / pins.CONFIG).write_text(json.dumps(self.config, indent=2) + "\n")
        for name in pins.TEMPLATES:
            shutil.copy(ROOT / name, self.root / name)

    def run_main(self, release: dict, commit: str = COMMIT, live=None) -> tuple[int, dict, str, str]:
        if live is None:
            live = PublishedChannels.live_locked
        path = self.root / "release.json"
        path.write_text(json.dumps(release))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = pins.main(["--release", str(path), "--tag-commit", commit, "--body", str(self.root / "body.md")],
                               root=self.root, live=live,
                               trust_root=key_material()[1] if OPENSSL else None)
        # The workflow tees stdout into $GITHUB_OUTPUT: every line must be a key=value output.
        lines = out.getvalue().splitlines()
        for line in lines:
            self.assertRegex(line, r"^[a-z]+=", f"stdout carries a line $GITHUB_OUTPUT refuses: {line}")
        outputs = dict(line.split("=", 1) for line in lines)
        return status, outputs, out.getvalue() + err.getvalue(), (self.root / pins.CONFIG).read_text()

    def need_openssl(self) -> None:
        if OPENSSL is None:
            self.skipTest("OpenSSL 3 is required to sign the fixture catalogs")

    def newer(self) -> str:
        major, minor, patch = catalog.version_tuple(self.config["installer"]["latestVersion"])
        return f"{major}.{minor}.{patch + 1}"

    def test_an_app_release_writes_the_config_and_the_pull_request(self):
        self.need_openssl()
        version = self.newer()
        status, outputs, log, written = self.run_main(app_release(version))
        self.assertEqual(status, 0, log)
        self.assertEqual(outputs, {"changed": "true", "branch": f"pins/app-{version}",
                                   "title": f"Pin app {version} in the release pipeline"})
        new = json.loads(written)
        self.assertEqual(new["installer"]["latestVersion"], version)
        self.assertEqual(new["installer"]["minimumVersion"], self.config["installer"]["minimumVersion"])
        self.assertEqual(new["engine"], self.config["engine"])
        body = (self.root / "body.md").read_text()
        self.assertIn(DMG_SHA, body)
        self.assertIn("## Merge Danger", body)
        self.assertIn(f"`installer.minimumVersion` stays {self.config['installer']['minimumVersion']}", body)
        self.assertIn("--check-published", body)

    def test_an_app_release_opens_nothing_while_stable_pins_another_engine(self):
        self.need_openssl()
        live = (lambda channel: PublishedChannels.envelope(OTHER_DIGEST)
                if channel == "stable" else PublishedChannels.live_locked(channel))
        status, _outputs, log, written = self.run_main(app_release(self.newer()), live=live)
        self.assertEqual(status, 1)
        self.assertIn("catalog-stable pins engine digest", log)
        self.assertEqual(json.loads(written), self.config)

    def test_check_published_repeats_the_app_pin_check_without_a_release(self):
        self.need_openssl()
        argv = ["--check-published"]
        stale = lambda channel: PublishedChannels.envelope(OTHER_DIGEST)
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(pins.main(argv, root=self.root, live=stale,
                                       trust_root=key_material()[1]), 1)
        self.assertIn("catalog-rc pins engine digest", err.getvalue())
        with redirect_stderr(io.StringIO()) as err, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(pins.main(argv, root=self.root, live=PublishedChannels.live_locked,
                                       trust_root=key_material()[1]), 0)
        self.assertIn("changed=false", out.getvalue())

    def test_check_published_takes_no_release(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pins.main(["--check-published", "--body", str(self.root / "body.md")], root=self.root)

    def test_an_engine_release_moves_the_config_and_the_templates(self):
        old = self.config["engine"]["downstreamRevision"]
        status, outputs, log, written = self.run_main(engine_release())
        self.assertEqual(status, 0, log)
        self.assertEqual(outputs["branch"], f"pins/engine-{ENGINE}")
        self.assertEqual(json.loads(written)["engine"], {"downstreamRevision": COMMIT})
        self.assertEqual(json.loads(written)["installer"], self.config["installer"])
        for name in pins.TEMPLATES:
            with self.subTest(name):
                template = (self.root / name).read_text()
                self.assertNotIn(old, template)
                self.assertEqual(json.loads(template)["downstream_revision"], COMMIT)
        self.assertIn(ARCHIVE["sha256"], (self.root / "body.md").read_text())

    def test_a_current_pin_or_another_release_writes_nothing(self):
        current = self.config["installer"]["latestVersion"]
        for release, commit in ((app_release(current), COMMIT),
                                (engine_release(), self.config["engine"]["downstreamRevision"]),
                                ({"tag_name": "catalog-edge", "draft": False, "assets": []}, COMMIT)):
            with self.subTest(release["tag_name"]):
                before = (self.root / pins.CONFIG).read_text()
                status, outputs, log, written = self.run_main(release, commit)
                self.assertEqual((status, outputs), (0, {"changed": "false"}), log)
                self.assertEqual(written, before)
                self.assertFalse((self.root / "body.md").exists())

    def test_a_refused_release_writes_nothing(self):
        before = (self.root / pins.CONFIG).read_text()
        status, outputs, log, written = self.run_main(app_release(self.newer(), draft=True))
        self.assertEqual(status, 1)
        self.assertIn("::error::", log)
        self.assertEqual(written, before)


def job_lines(path: Path) -> str:
    return "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))


class Workflow(unittest.TestCase):
    def setUp(self) -> None:
        self.text = job_lines(WORKFLOW)

    def test_published_releases_and_dispatches_start_it_and_nothing_else(self):
        on = self.text.split("\non:\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(re.findall(r"^  ([a-z_]+):", on, re.M), ["release", "workflow_dispatch"])
        self.assertIn("    types: [published]", on)

    def test_it_holds_only_what_a_pull_request_needs(self):
        self.assertIn("\npermissions: {}\n", self.text)
        bare = re.sub(r" +# [^\n]*", "", self.text)
        self.assertRegex(bare, r"    permissions:\n      contents: write\n      pull-requests: write\n"
                               r"      actions: write\n    steps:")
        self.assertNotIn("secrets.", self.text)
        self.assertNotIn("pull_request_target", self.text)

    def test_the_pins_move_on_main_through_the_script(self):
        self.assertIn("          ref: main\n          persist-credentials: false", self.text)
        self.assertIn("python3 -I scripts/pipeline_pins.py", self.text)
        self.assertNotIn("minimumVersion", self.text)
        self.assertIn("gh pr create --repo \"$GITHUB_REPOSITORY\" --base main", self.text)
        self.assertNotRegex(self.text, r"gh pr merge|--admin|--auto")

    def test_the_tag_reaches_the_shell_only_as_data(self):
        self.assertIn("TAG: ${{ inputs.tag || github.event.release.tag_name }}", self.text)
        scripts, indent = [], None
        for line in self.text.splitlines():
            depth = len(line) - len(line.lstrip())
            if indent is not None and line.strip() and depth <= indent:
                indent = None
            if indent is not None:
                scripts.append(line)
            elif line.strip().endswith("run: |"):
                indent = depth
        self.assertTrue(scripts)
        self.assertNotIn("${{", "\n".join(scripts))

    def test_no_release_waits_behind_another(self):
        self.assertIn("  group: mac-pipeline-pins-${{ inputs.tag || github.event.release.tag_name }}\n"
                      "  cancel-in-progress: false", self.text)

    def test_the_pull_request_gets_its_required_checks(self):
        self.assertIn('gh workflow run checks.yml --repo "$GITHUB_REPOSITORY" --ref "$BRANCH"', self.text)
        self.assertIn("workflow_dispatch:", (ROOT / ".github" / "workflows" / "checks.yml").read_text())

    def test_the_bumped_config_is_tested_before_the_pull_request_opens(self):
        steps = re.findall(r"      - name: ([^\n]+)", self.text)
        self.assertLess(steps.index("Test the bumped config"), steps.index("Open the pull request"))


if __name__ == "__main__":
    unittest.main()

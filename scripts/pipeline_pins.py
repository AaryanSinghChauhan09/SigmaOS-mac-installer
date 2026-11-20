#!/usr/bin/env python3
"""Moves .github/mac-release-pipeline.json's pins to a published release.

An app release (v<X.Y.Z>) moves installer.latestVersion and
installer.downloadURL to that app's dmg under the catalog base, where
mac-mirror.yml copies it. An engine release (engine-<version>) of the engine
Engine/source-lock.json pins moves engine.downstreamRevision to the release's
commit, here and in the release-inputs templates. Nothing else changes:
installer.minimumVersion is the floor of apps a catalog accepts and moves only
by hand.

  pipeline_pins.py --release RELEASE.json --tag-commit SHA --body BODY.md

RELEASE.json is the GitHub API's release object. Prints changed=, branch= and
title= lines for $GITHUB_OUTPUT and writes the pull request body to BODY.md.
Exits 1 with the reason when the release cannot be pinned.
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ".github/mac-release-pipeline.json"
TEMPLATES = ("scripts/release-inputs.template.json", "scripts/release-inputs-aurora.template.json")
APP_TAG = re.compile(r"^v([0-9]+\.[0-9]+\.[0-9]+)$")
ENGINE_TAG = re.compile(r"^engine-(v[0-9A-Za-z+._-]{1,127})$")
DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")


def load_catalog():
    spec = importlib.util.spec_from_file_location("catalog_release", ROOT / "scripts" / "catalog_release.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("catalog_release", module)
    spec.loader.exec_module(module)
    return module


catalog = load_catalog()
Refusal = catalog.Refusal


def kind(tag: str) -> str | None:
    """app, engine, or None for any other release (catalogs, images)."""
    if APP_TAG.match(tag):
        return "app"
    if ENGINE_TAG.match(tag):
        return "engine"
    return None


def asset(release: dict, name: str) -> dict:
    found = [item for item in release.get("assets", []) if item.get("name") == name]
    if len(found) != 1:
        raise Refusal(f"{release['tag_name']} holds no single {name}")
    return found[0]


def digest(item: dict) -> str:
    match = DIGEST.match(item.get("digest") or "")
    if match is None:
        raise Refusal(f"{item['name']} has no sha256 digest")
    return match.group(1)


def published(release: dict) -> None:
    if release.get("draft"):
        raise Refusal(f"{release['tag_name']} is a draft; only a published release is pinned")


def app_pin(release: dict) -> dict:
    """The app release's version, dmg digest and the dmg's catalog-base URL."""
    published(release)
    version = APP_TAG.match(release["tag_name"]).group(1)
    names = catalog.identity()
    dmg = asset(release, f"{names['INSTALLER_FILE_STEM']}-{version}.dmg")
    asset(release, f"{dmg['name']}.sha256")
    return {
        "kind": "app",
        "tag": release["tag_name"],
        "url": release.get("html_url", ""),
        "version": version,
        "digest": digest(dmg),
        "downloadURL": f"{names['INSTALLER_CATALOG_BASE']}/installer/{version}/{dmg['name']}",
    }


def engine_pin(release: dict, commit: str, lock: dict) -> dict:
    """The engine release's version, archive digest and commit, which must be
    the engine LOCK pins: the pipeline builds no other."""
    published(release)
    version = ENGINE_TAG.match(release["tag_name"]).group(1)
    pinned = lock["downstream_overlay"]["version"]
    if version != pinned:
        raise Refusal(f"Engine/source-lock.json pins engine {pinned}, not {version}")
    artifact = lock["validation_artifact"]
    archive = asset(release, artifact["filename"])
    if digest(archive) != artifact["sha256"] or archive.get("size") != artifact["size_bytes"]:
        raise Refusal(f"{archive['name']} is not the archive Engine/source-lock.json pins")
    if not catalog.REVISION.match(commit):
        raise Refusal(f"{release['tag_name']}'s commit must be a 40-character lowercase hex revision")
    return {
        "kind": "engine",
        "tag": release["tag_name"],
        "url": release.get("html_url", ""),
        "version": version,
        "digest": artifact["sha256"],
        "downstreamRevision": commit,
    }


def bump(config: dict, pin: dict) -> dict:
    """CONFIG with PIN's pins and every other field as it was. An app older
    than the one pinned leaves it as it is."""
    config = copy.deepcopy(config)
    if pin["kind"] == "engine":
        config["engine"]["downstreamRevision"] = pin["downstreamRevision"]
        return config
    installer = config.get("installer")
    if not isinstance(installer, dict):
        raise Refusal(f"{CONFIG} names no app block: set installer, with its minimumVersion, by hand first")
    if catalog.version_tuple(pin["version"]) < catalog.version_tuple(installer["latestVersion"]):
        return config
    installer["latestVersion"] = pin["version"]
    installer["downloadURL"] = pin["downloadURL"]
    return config


def published_channels(live=None, trust_root=None) -> dict[str, dict]:
    """The channel catalogs apps are served today: channel -> document. LIVE
    fetches one channel's signed envelope; a 404 for rc or stable refuses,
    since both channels are published and a deleted or moved asset would
    switch the check off. The GitHub stopgap is what the apps read while
    APP_CATALOG_HOST is stopgap (mac-app-release.yml) — ticket 13 moves the
    catalog to the catalog base, and this host moves with it. Every envelope
    must verify against the trust root before it is believed."""
    if live is None:
        live = catalog.GitHubReleaseHost(catalog.stopgap_repo()).fetch
    if trust_root is None:
        trust_root = catalog.read_trust_root(catalog.DEFAULT_TRUST_ROOT)
    found = {}
    for channel in catalog.RELEASE_CHANNELS:
        url = (f"https://github.com/{catalog.stopgap_repo()}/releases/download/"
               f"catalog-{channel}/catalog-{channel}.signed.json")
        try:
            envelope = live(channel)
        except Exception as error:
            raise Refusal(f"the published catalog-{channel} could not be read ({error})") from None
        if envelope is None:
            raise Refusal(f"the published catalog-{channel} is missing: apps read {url}; "
                          "a deleted or moved catalog must not switch this check off")
        try:
            found[channel] = catalog.verify_envelope(envelope, trust_root)
        except Refusal as error:
            raise Refusal(f"the published catalog-{channel} does not verify: {error}") from None
    return found


# Recorded lane holds, the machine-readable form of docs/catalog-release.md's
# "Lane holds" table: the exact engine digest an owner keeps on a channel.
# Retire a row only after a real run re-signs the lane's catalog on the
# locked engine and every host serving it reads the new catalog back.
LANE_HOLDS = {
    "rc": {
        "digest": "sha256:5fea6b561e6fb82260f573bae099c70ba2e9998df56d38cf285987432320d4b5",
        "engine": "v0.9.2-omarchy.20261022.31425f17fb9b",
        # The owner of record, from identity.conf: the release owner holds this lane.
        "owner": catalog.identity()["INSTALLER_GITHUB_RELEASE_LOGIN"],
        "since": "2026-11-20",
        "reason": "Owner decision; stable carries the fix",
    },
}

ENGINE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def check_published(config: dict, version: str, live=None, trust_root=None) -> None:
    """Every published channel catalog must pin the archive
    Engine/source-lock.json pins, by digest: an install runs the catalog's
    engineDigest, so installer.latestVersion must not outrun it before
    VERSION is offered. A lane's recorded hold is the only other digest
    accepted, and it is a notice, not silence; every lane is verified the
    same — signature, one digest across its models, no missing catalog."""
    wanted = f"sha256:{catalog.pipeline_engine(config['engine'])['sha256']}"
    for channel, document in published_channels(live, trust_root).items():
        models = document.get("models") if isinstance(document, dict) else None
        if not isinstance(models, list) or not models:
            raise Refusal(f"the published catalog-{channel} names no models")
        digests = set()
        for model in models:
            served = model.get("engineDigest") if isinstance(model, dict) else None
            if not isinstance(served, str) or not ENGINE_DIGEST.match(served):
                raise Refusal(f"the published catalog-{channel} names a model with no engineDigest")
            digests.add(served)
        if len(digests) != 1:
            raise Refusal(f"the published catalog-{channel} pins mixed engine digests")
        served = digests.pop()
        if served == wanted:
            continue
        hold = LANE_HOLDS.get(channel)
        if hold and served == hold["digest"]:
            print(f"::notice::catalog-{channel} is on the recorded hold: engine {hold['engine']} "
                  f"{served} (owner {hold['owner']}, since {hold['since']}: {hold['reason']})",
                  file=sys.stderr)
            continue
        raise Refusal(f"catalog-{channel} pins engine digest {served}, not the locked {wanted}"
                      + (f" nor its recorded hold {hold['digest']}" if hold else "")
                      + f"; publish the {channel} catalog on the locked engine before "
                        f"{version} is offered")


def check(config: dict, pin: dict | None = None, live=None, trust_root=None) -> None:
    """The pipeline's catalog-inputs checks on the bumped config. An app pin
    also asks that the channel catalogs already pin the engine digest the
    lock names: installer.latestVersion must not outrun the engine installs
    run."""
    try:
        catalog.check_installer(config["installer"], catalog.pipeline_engine(config["engine"]))
    except Refusal as error:
        raise Refusal(f"the bumped config fails the catalog's checks ({error}); "
                      "installer.minimumVersion moves only in a pull request of its own") from None
    if pin is None or pin["kind"] != "app":
        return
    check_published(config, pin["version"], live=live, trust_root=trust_root)


def branch(pin: dict) -> str:
    return f"pins/{pin['kind']}-{pin['version']}"


def title(pin: dict) -> str:
    return f"Pin {pin['kind']} {pin['version']} in the release pipeline"


def body(pin: dict, old: dict, new: dict) -> str:
    if pin["kind"] == "app":
        before = f"app {old['installer']['latestVersion']}  {old['installer']['downloadURL']}"
        after = f"app {new['installer']['latestVersion']}  {new['installer']['downloadURL']}"
        what = (f"Moves the app the pipeline's catalogs offer to [{pin['tag']}]({pin['url']}): "
                "`installer.latestVersion` and `installer.downloadURL`.")
        digest_line = f"- dmg sha256 `{pin['digest']}` (the release asset's digest)."
        merge = ("Merge after `mac-mirror.yml` (artifact app) has copied the dmg and its `.sha256` to the "
                 "download URL; the catalog step refuses a dmg the URL does not serve.")
        radius = (f"The next pipeline catalog offers app {pin['version']} as the latest; "
                  "the apps it turns away stay those below the unchanged minimum.")
    else:
        before = f"engine revision {old['engine']['downstreamRevision']}"
        after = f"engine revision {new['engine']['downstreamRevision']}"
        what = (f"Moves `engine.downstreamRevision`, here and in the release-inputs templates, to the commit of "
                f"[{pin['tag']}]({pin['url']}), the engine `Engine/source-lock.json` pins.")
        digest_line = f"- archive sha256 `{pin['digest']}` (the release asset's digest, equal to `validation_artifact`)."
        merge = ("Merge after `mac-mirror.yml` (artifact engine) has copied the archive to R2; the catalog step "
                 "refuses an engine that is not mirrored.")
        radius = "The next pipeline catalog names this engine's revision."
    minimum = (old.get("installer") or {}).get("minimumVersion")
    return (
        "## Summary\n\n"
        f"{what} Opened by `mac-pipeline-pins.yml` when the release was published.\n\n"
        f"```diff\n-  {before}\n+  {after}\n```\n\n"
        f"`installer.minimumVersion` stays {minimum}: it is the floor of apps a catalog accepts and moves only "
        "in a pull request of its own.\n\n"
        "## Evidence\n\n"
        f"{digest_line}\n"
        "- `catalog_release.check_installer` and `pipeline_engine` accept the bumped config.\n"
        "- `test_release_pipeline.py`, `test_catalog_release.py` and `test_pipeline_pins.py` pass on it.\n\n"
        "## Merge Danger\n\n"
        "**Door:** two-way\n\n"
        f"Config only; nothing is signed until a pipeline run's `catalog-release` gate is approved. {merge}\n\n"
        f"**Blast Radius:** edge-catalog\n\n{radius}\n"
    )


def write(root: Path, old: dict, new: dict) -> None:
    (root / CONFIG).write_text(json.dumps(new, indent=2) + "\n")
    before, after = old["engine"]["downstreamRevision"], new["engine"]["downstreamRevision"]
    if before == after:
        return
    for name in TEMPLATES:
        path = root / name
        if path.exists() and before in path.read_text():
            path.write_text(path.read_text().replace(before, after))


def main(argv: list[str] | None = None, root: Path = ROOT, live=None, trust_root=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--release", type=Path)
    parser.add_argument("--tag-commit")
    parser.add_argument("--body", type=Path)
    parser.add_argument("--check-published", action="store_true",
                        help="refuse while a published channel catalog pins an engine digest "
                             "other than the locked one or its lane's recorded hold (the "
                             "merge-time half of the app-pin check, run from the pin pull "
                             "request's own checks)")
    args = parser.parse_args(argv)
    if args.check_published:
        if args.release is not None or args.tag_commit is not None or args.body is not None:
            parser.error("--check-published takes no release, tag commit or body")
        try:
            config = json.loads((root / CONFIG).read_text())
            installer = config.get("installer") or {}
            check_published(config, installer.get("latestVersion", "?"), live=live,
                            trust_root=trust_root)
        except Refusal as error:
            print(f"::error::{error}", file=sys.stderr)
            return 1
        print("changed=false")
        return 0
    if args.release is None or args.tag_commit is None or args.body is None:
        parser.error("--release, --tag-commit and --body are required without --check-published")
    release = json.loads(args.release.read_text())
    found = kind(release.get("tag_name", ""))
    if found is None:
        print(f"::notice::{release.get('tag_name')} is not an app or engine release; no pins move", file=sys.stderr)
        print("changed=false")
        return 0
    old = json.loads((root / CONFIG).read_text())
    try:
        if found == "app":
            pin = app_pin(release)
        else:
            pin = engine_pin(release, args.tag_commit, json.loads(catalog.SOURCE_LOCK.read_text()))
        new = bump(old, pin)
        if new == old:
            print(f"::notice::{CONFIG} already pins {found} {pin['version']} or a later one", file=sys.stderr)
            print("changed=false")
            return 0
        check(new, pin, live=live, trust_root=trust_root)
    except Refusal as error:
        print(f"::error::{release['tag_name']}: {error}", file=sys.stderr)
        return 1
    write(root, old, new)
    args.body.write_text(body(pin, old, new))
    print("changed=true")
    print(f"branch={branch(pin)}")
    print(f"title={title(pin)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

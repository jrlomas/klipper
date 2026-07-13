#!/usr/bin/env python3
# Consent outbox, feedback, and signed catalog activation tests.

import io
import json
import os
import pathlib
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

from atlas.decode import decode_klippy_log  # noqa: E402
from atlas.diagnosis import Matcher  # noqa: E402
from atlas.kb import (ConsentError, KnowledgeOutbox,  # noqa: E402
                      SignedCatalogInstaller, assemble_bundle)


ROOT = pathlib.Path(__file__).resolve().parent.parent


def _bundle():
    timeline = decode_klippy_log(
        "Start printer at X (100.0 5.0)\n"
        "MCU 'mcu' shutdown: Timer too close\n")
    return assemble_bundle(timeline, Matcher([]).diagnose(timeline))


def test_consent_is_incident_bound_single_use_and_deduplicated():
    with tempfile.TemporaryDirectory() as tmp:
        now = [100.0]
        outbox = KnowledgeOutbox(
            pathlib.Path(tmp) / "outbox.sqlite3", wall_clock=lambda: now[0])
        bundle = _bundle()
        token = outbox.grant(bundle)
        outbox.enqueue(bundle, token)
        try:
            outbox.enqueue(bundle, token)
        except ConsentError:
            pass
        else:
            raise AssertionError("consent token was reusable")
        token2 = outbox.grant(bundle)
        outbox.enqueue(bundle, token2)
        queued = outbox.queued()
        assert len(queued) == 1 and queued[0]["observations"] == 2
        assert queued[0]["payload"]["redacted"] is True
        outbox.record_feedback(bundle.content_hash, True, False)
        outbox.mark_sent(bundle.content_hash)
        assert outbox.queued() == []
        assert (pathlib.Path(tmp) / "outbox.sqlite3").stat().st_mode & 0o777 == 0o600
        outbox.close()
        print("PASS: consent is incident-bound/single-use and submissions dedup")


def _archive(path, version, unsafe=False):
    with tarfile.open(path, "w:gz") as tar:
        name = "../escape" if unsafe else "manifest.json"
        data = json.dumps({"schema_version": 1, "version": version}).encode()
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))


def _sign(path):
    signature = str(path) + ".sig"
    subprocess.run([
        sys.executable, str(ROOT / "scripts" / "sign_image.py"), "blob",
        str(path), "--key", str(ROOT / "keys" / "helix_dev_signing.key"),
        "-o", signature], check=True, capture_output=True)
    return signature


def test_signed_catalog_activation_and_rollback():
    with tempfile.TemporaryDirectory() as tmp:
        destination = pathlib.Path(tmp) / "active"
        installer = SignedCatalogInstaller(
            destination, public_key=ROOT / "keys" / "helix_dev_signing.pub")
        one = pathlib.Path(tmp) / "one.tgz"
        two = pathlib.Path(tmp) / "two.tgz"
        _archive(one, "1")
        _archive(two, "2")
        assert installer.install(one, _sign(one))["version"] == "1"
        assert installer.install(two, _sign(two))["version"] == "2"
        installer.rollback()
        active = json.loads((destination / "manifest.json").read_text())
        assert active["version"] == "1"
        with two.open("ab") as handle:
            handle.write(b"tamper")
        try:
            installer.install(two, str(two) + ".sig")
        except ValueError as exc:
            assert "signature" in str(exc)
        else:
            raise AssertionError("tampered catalog activated")
        print("PASS: signed catalog activation rejects tamper and rolls back")


def test_catalog_rejects_path_traversal_even_with_valid_signature():
    with tempfile.TemporaryDirectory() as tmp:
        archive = pathlib.Path(tmp) / "bad.tgz"
        _archive(archive, "bad", unsafe=True)
        installer = SignedCatalogInstaller(
            pathlib.Path(tmp) / "active", verifier=lambda archive, sig: True)
        try:
            installer.install(archive, "ignored")
        except ValueError as exc:
            assert "unsafe path" in str(exc)
        else:
            raise AssertionError("path traversal archive extracted")
        assert not (pathlib.Path(tmp).parent / "escape").exists()
        print("PASS: signed archive extraction still rejects path traversal")


def main():
    test_consent_is_incident_bound_single_use_and_deduplicated()
    test_signed_catalog_activation_and_rollback()
    test_catalog_rejects_path_traversal_even_with_valid_signature()
    print("ALL PASS")


if __name__ == "__main__":
    main()

"""Canary artifact safeguards using disposable keys and synthetic runtime inputs.

These tests never claim compatibility or real E2E evidence. Source Git checks are
mocked in fixture preparation only, so a non-runnable test image is never trusted
as a real pinned release input. No real signing key, daemon or Unraid host is used.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import lzma
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from unraid.lab import bundle
from unraid.tests.test_canary_image import fake_binary

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("canary_artifacts", ROOT / "unraid/scripts/canary-artifacts.py")
ART = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ART)


class CanaryArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="polyseat-artifact-test-")
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.kit = self.dir / "kit"
        self.key = self.dir / "throwaway-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key)],
                       stdin=subprocess.DEVNULL, check=True)
        self.pub = self.key.with_name(self.key.name + ".pub")
        _, self.fp = ART.public_key(self.pub)
        self.package = ART.PACKAGE.build("2026.10.30.canary", self.dir)
        self.payload = self.dir / "payload"
        (self.payload / "bin").mkdir(parents=True)
        (self.payload / "share/canary").mkdir(parents=True)
        (self.payload / "bin/incus").write_text("synthetic test-only client\n")
        (self.payload / "bin/incusd").write_text("synthetic test-only daemon\n")
        binary = fake_binary()
        image = ART.IMAGE.build_image(binary, binary_sha256=hashlib.sha256(binary).hexdigest(), version="1.36.1")
        (self.payload / "share/canary/busybox.tar.gz").write_bytes(image)
        self.archive, self.manifest = bundle.build(
            self.payload, self.dir, name="polyseat-incus", version="7.5.1-polyseat.ci", incus_version="7.5.1",
            min_unraid="7.2.0", min_glibc="2.38", needed_libs=["libc.so.6"])
        self.digest = self.archive.name.removesuffix(".tar.gz")
        self.commit = "a" * 40
        self.provenance = self.dir / "provenance.json"
        self.provenance.write_bytes(ART.canonical({"schema": 1, "repository": "sNyteXx/Polyseat-Unraid",
            "commit": self.commit, "origin": "local", "build_id": "synthetic-test-only", "archive_sha256": self.digest}))
        self.plan = self.dir / "plan.md"
        self.plan.write_text("Synthetic fixture plan: no host actions approved.\n")
        self.evidence = self.dir / "evidence"
        self.evidence.mkdir()
        (self.evidence / "abi-report.json").write_text('{"ok":true}\n')
        (self.evidence / "build-info.txt").write_text("SYNTHETIC_TEST_ONLY=1\n")
        (self.evidence / "e2e-result.txt").write_text("PASS: controller end-to-end on real incusd (synthetic UNIT TEST ONLY)\n")
        (self.evidence / "SBOM-runtime.cdx.json").write_bytes(ART.canonical({"metadata": {"component": {
            "hashes": [{"alg": "SHA-256", "content": self.digest}]}}}))
        (self.evidence / "canary-rehearsal.json").write_bytes(ART.canonical({
            "schema": 1, "commit": self.commit, "archive_sha256": self.digest,
            "controller_sha256": hashlib.sha256(self.package.read_bytes()).hexdigest(),
            "platform": "disposable-linux", "status": "passed", "steps": sorted(ART.REHEARSAL_STEPS)}))
        self.prep_args = argparse.Namespace(archive=self.archive, manifest=self.manifest, controller=self.package,
            plan=self.plan, provenance=self.provenance, evidence_dir=self.evidence, out=self.kit,
            expires_in=3600, acknowledge_artifacts_only=True)
        self.seal_args = argparse.Namespace(kit=self.kit, trusted_key=self.pub, fingerprint=self.fp,
            revoked_keys=None, signing_key=self.key, acknowledge_artifacts_only=True)

    def prepare(self):
        with patch.object(ART, "source_check") as source_check, contextlib.redirect_stdout(io.StringIO()):
            ART.prepare(self.prep_args)
        source_check.assert_called_once()

    def seal(self):
        with contextlib.redirect_stdout(io.StringIO()):
            ART.seal(self.seal_args)

    def verify(self, *, require_rehearsal=False, key=None, fingerprint=None, revoked=None):
        return ART.verify_kit(self.kit, key or self.pub, fingerprint or self.fp, revoked,
                              require_rehearsal=require_rehearsal)

    def resign_manifest(self, mutator=None):
        path = self.kit / ART.MANIFEST
        spec = json.loads(path.read_text())
        if mutator:
            mutator(spec)
        path.write_bytes(ART.canonical(spec))
        signature = self.kit / (ART.MANIFEST + ".sig")
        signature.unlink()
        subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(self.key), "-n", ART.KIT_NAMESPACE, str(path)],
                       stdin=subprocess.DEVNULL, check=True)
        (self.kit / "SHA256SUMS").write_bytes(ART.checksum_text(self.kit))

    def test_real_package_is_deterministic_canary_and_source_stays_closed(self):
        second = self.dir / "second"
        second.mkdir()
        built = ART.PACKAGE.build("2026.10.30.canary", second)
        self.assertEqual(self.package.read_bytes(), built.read_bytes())
        entries = ART.PACKAGE.inspect_package(built.read_bytes(), "canary")
        self.assertTrue(any(m.name == ART.PACKAGE.CONTROLLER for m, _ in entries))
        source = (ROOT / "unraid/plugin/src/emhttp/scripts/polyseatctl").read_text()
        self.assertIn('POLYSEAT_RELEASE_GATE="closed"', source)
        with self.assertRaises(ValueError):
            ART.PACKAGE.build("2026.10.30.canary", second)

    def test_default_builder_cannot_make_canary(self):
        result = subprocess.run([str(ROOT / "unraid/scripts/build-plugin-package.sh"), "2026.10.30", str(self.dir), "canary"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("separately approved", result.stderr)

    def test_gate_check_allows_guardian_env_inheritance_but_rejects_a_second_definition(self):
        extra_env = b'\nenv -i PATH="$SAFE_PATH" POLYSEAT_RELEASE_GATE="$POLYSEAT_RELEASE_GATE" /bin/bash guardian\n'
        entries = ART.PACKAGE.inspect_package(self.package.read_bytes(), "canary")
        for duplicate in (False, True):
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
                for member, content in entries:
                    if member.name == ART.PACKAGE.CONTROLLER:
                        content += extra_env
                        if duplicate:
                            content += b'POLYSEAT_RELEASE_GATE="canary"\n'
                        member.size = len(content)
                    tar.addfile(member, io.BytesIO(content) if content is not None else None)
            data = lzma.compress(raw.getvalue())
            if duplicate:
                with self.assertRaisesRegex(ValueError, "release gate mismatch"):
                    ART.PACKAGE.inspect_package(data, "canary")
            else:
                ART.PACKAGE.inspect_package(data, "canary")

    def test_gate_render_changes_only_the_single_source_definition(self):
        source = (b'POLYSEAT_RELEASE_GATE="closed" # source gate\n'
                  b'env -i PATH="$SAFE_PATH" POLYSEAT_RELEASE_GATE="$POLYSEAT_RELEASE_GATE" /bin/bash guardian\n'
                  b"message='POLYSEAT_RELEASE_GATE=\"closed\"'\n")
        rendered = ART.PACKAGE.render_gate(source, "closed", "canary")
        self.assertEqual(rendered.splitlines()[0], b'POLYSEAT_RELEASE_GATE="canary" # source gate')
        self.assertEqual(rendered.splitlines()[1:], source.splitlines()[1:])
        for wrong in (source + b'POLYSEAT_RELEASE_GATE="open"\n',
                      source.replace(b'POLYSEAT_RELEASE_GATE="closed" # source gate', b'POLYSEAT_RELEASE_GATE="closed" echo bad')):
            with self.assertRaises(ValueError):
                ART.PACKAGE.render_gate(wrong, "closed", "canary")

    def test_owner_seal_cli_refuses_ci_even_with_valid_disposable_inputs(self):
        self.prepare()
        result = subprocess.run([sys.executable, str(ROOT / "unraid/scripts/canary-artifacts.py"), "seal",
            "--kit", str(self.kit), "--trusted-key", str(self.pub), "--fingerprint", self.fp,
            "--signing-key", str(self.key), "--acknowledge-artifacts-only"],
            env={**os.environ, "CI": "true"}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("CI must not seal", result.stderr)
        self.assertFalse(list(self.kit.glob("*.sig")))

    def test_prepare_requires_ack_and_truthful_origin(self):
        self.prep_args.acknowledge_artifacts_only = False
        with self.assertRaisesRegex(ValueError, "acknowledge"):
            self.prepare()
        self.prep_args.acknowledge_artifacts_only = True
        spec = json.loads(self.provenance.read_text())
        spec["run_id"] = 12
        self.provenance.write_bytes(ART.canonical(spec))
        with self.assertRaisesRegex(ValueError, "impersonate"):
            self.prepare()
        self.assertFalse(self.kit.exists())

    def test_prepare_rejects_failed_abi_e2e_and_unbound_sbom(self):
        for name, bad in (("abi-report.json", b'{"ok":false}'), ("e2e-result.txt", b"FAIL: no e2e"),
                          ("SBOM-runtime.cdx.json", b'{"metadata":{}}')):
            path = self.evidence / name
            original = path.read_bytes()
            path.write_bytes(bad)
            with self.assertRaises(ValueError):
                self.prepare()
            path.write_bytes(original)
            self.assertFalse(self.kit.exists())

    def test_source_verification_is_not_optional_in_real_prepare(self):
        with self.assertRaises(subprocess.CalledProcessError):
            ART.prepare(self.prep_args)  # a fake commit cannot be packaged as source proof
        self.assertFalse(self.kit.exists())

    def test_seal_and_verify_bind_all_artifacts_without_issuing_approval(self):
        self.prepare()
        self.seal()
        result = self.verify()
        self.assertEqual(result["integrity"], "verified")
        self.assertFalse(result["unraid_test_approved"])
        self.assertTrue(result["canary_rehearsal"])
        spec = ART.read_kit(self.kit)
        self.assertEqual(spec["image"]["binary_sha256"], hashlib.sha256(fake_binary()).hexdigest())
        self.assertEqual(spec["scope"]["data_dir"], "/var/lib/polyseat-unraid-canary")
        for path in self.kit.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"OPENSSH PRIVATE KEY", path.read_bytes())
                self.assertNotIn("approval", path.name)

    def test_unsigned_or_missing_signature_is_not_trusted(self):
        self.prepare()
        with self.assertRaises(OSError):
            self.verify()
        self.seal()
        (self.kit / (ART.MANIFEST + ".sig")).unlink()
        with self.assertRaises(OSError):
            self.verify()

    def test_tampering_and_extra_files_rejected(self):
        self.prepare()
        self.seal()
        for name in ("PLAN.md", self.package.name, self.archive.name):
            path = self.kit / name
            original = path.read_bytes()
            path.write_bytes(original + b"tampered")
            with self.assertRaises(ValueError):
                self.verify()
            path.write_bytes(original)
        extra = self.kit / "unlisted.txt"
        extra.write_text("extra")
        with self.assertRaises(ValueError):
            self.verify()

    def test_symlink_and_hardlink_are_rejected(self):
        self.prepare()
        self.seal()
        plan = self.kit / "PLAN.md"
        data = plan.read_bytes()
        plan.unlink()
        plan.symlink_to(self.plan)
        with self.assertRaises(ValueError):
            self.verify()
        plan.unlink()
        plan.write_bytes(data)
        other = self.dir / "hardlink"
        other.hardlink_to(plan)
        with self.assertRaises(ValueError):
            self.verify()

    def test_kit_public_key_cannot_bootstrap_trust_and_fingerprint_must_match(self):
        self.prepare()
        self.seal()
        with self.assertRaisesRegex(ValueError, "bootstrap"):
            self.verify(key=self.kit / "artifact-signer.pub")
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self.verify(fingerprint="SHA256:not-the-key")

    def test_wrong_signer_and_revocation_fail_closed(self):
        self.prepare()
        self.seal()
        other = self.dir / "other-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)],
                       stdin=subprocess.DEVNULL, check=True)
        other_pub = other.with_name(other.name + ".pub")
        _, fp = ART.public_key(other_pub)
        with self.assertRaisesRegex(ValueError, "signature"):
            self.verify(key=other_pub, fingerprint=fp)
        revoked = self.dir / "revoked_keys"
        revoked.write_bytes(self.pub.read_bytes())
        with self.assertRaisesRegex(ValueError, "revocation"):
            self.verify(revoked=revoked)
        revoked.unlink()
        revoked.mkdir()
        with self.assertRaises(ValueError):
            self.verify(revoked=revoked)

    def test_expired_future_and_excessive_validity_rejected_even_if_signed(self):
        self.prepare()
        self.seal()
        now = int(time.time())
        for created, expires in ((now - 120, now - 1), (now + 120, now + 240), (now, now + ART.MAX_TTL + 1)):
            self.resign_manifest(lambda spec: spec.update(created_at=created, expires_at=expires))
            with self.assertRaises(ValueError):
                self.verify()

    def test_signatures_are_namespace_separated(self):
        self.prepare()
        self.seal()
        path = self.kit / self.package.name
        path.with_name(path.name + ".sig").unlink()
        subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(self.key), "-n", "polyseat-unraid-bundle", str(path)],
                       stdin=subprocess.DEVNULL, check=True)
        self.resign_manifest(lambda spec: spec.update(files=ART.file_records(self.kit)))
        with self.assertRaisesRegex(ValueError, "controller-v1"):
            self.verify()

    def test_disabled_preview_aborts_before_downloads_and_has_no_activation(self):
        self.prepare()
        self.seal()
        preview = self.kit / "polyseat-unraid-canary.plg.disabled"
        xml = ET.fromstring(preview.read_bytes())
        entries = xml.findall("FILE")
        self.assertEqual(entries[0].get("Run"), "/bin/bash")
        self.assertIn("exit 2", entries[0].find("INLINE").text)
        self.assertNotIn("activate", preview.read_text())
        self.assertFalse(any(e.get("Run") for e in entries[1:]))
        self.assertFalse(list(self.kit.glob("*.plg")))

    def test_rehearsal_is_required_when_requested_and_bound_to_exact_package(self):
        self.prepare()
        self.seal()
        path = self.kit / "evidence/canary-rehearsal.json"
        path.unlink()
        self.resign_manifest(lambda spec: spec.update(files=ART.file_records(self.kit)))
        with self.assertRaisesRegex(ValueError, "rehearsal still missing"):
            self.verify(require_rehearsal=True)
        evidence = {"schema": 1, "commit": self.commit, "archive_sha256": self.digest,
                    "controller_sha256": hashlib.sha256(self.package.read_bytes()).hexdigest(),
                    "platform": "disposable-linux", "status": "passed", "steps": sorted(ART.REHEARSAL_STEPS)}
        path.write_bytes(ART.canonical(evidence))
        self.resign_manifest(lambda spec: spec.update(files=ART.file_records(self.kit)))
        self.assertTrue(self.verify(require_rehearsal=True)["canary_rehearsal"])
        evidence["controller_sha256"] = "0" * 64
        path.write_bytes(ART.canonical(evidence))
        self.resign_manifest(lambda spec: spec.update(files=ART.file_records(self.kit)))
        with self.assertRaisesRegex(ValueError, "exact-artifact"):
            self.verify(require_rehearsal=True)

    def test_prepare_cannot_substitute_stale_or_missing_canary_rehearsal(self):
        path = self.evidence / "canary-rehearsal.json"
        original = json.loads(path.read_text())
        for change in ({"commit": "b" * 40}, {"archive_sha256": "0" * 64},
                       {"controller_sha256": "0" * 64}, {"steps": ["canary-smoke"]}, {"status": "failed"}):
            path.write_bytes(ART.canonical({**original, **change}))
            with self.assertRaisesRegex(ValueError, "exact-artifact"):
                self.prepare()
            self.assertFalse(self.kit.exists())
        path.unlink()
        with self.assertRaisesRegex(ValueError, "canary rehearsal"):
            self.prepare()

    def test_payload_comparison_discloses_labels_but_never_hides_payload_changes(self):
        right_dir = self.dir / "right"
        right_dir.mkdir()
        right_archive, right_manifest = bundle.build(self.payload, right_dir, name="polyseat-incus",
            version="7.5.1-polyseat.localdev", incus_version="7.5.1", min_unraid="7.2.0", min_glibc="2.38", needed_libs=["libc.so.6"])
        args = argparse.Namespace(left_archive=self.archive, left_manifest=self.manifest,
            right_archive=right_archive, right_manifest=right_manifest, output=None)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(ART.compare(args), 0)
        self.assertEqual(json.loads(output.getvalue())["labels"]["right"]["version"], "7.5.1-polyseat.localdev")
        (self.payload / "bin/incusd").write_text("changed ELF source/path is still a genuine byte difference")
        changed_dir = self.dir / "changed"
        changed_dir.mkdir()
        args.right_archive, args.right_manifest = bundle.build(self.payload, changed_dir, name="polyseat-incus",
            version="7.5.1-polyseat.localdev", incus_version="7.5.1", min_unraid="7.2.0", min_glibc="2.38", needed_libs=["libc.so.6"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(ART.compare(args), 3)
        self.assertEqual(json.loads(output.getvalue())["changed_payload_files"], ["payload/bin/incusd"])


if __name__ == "__main__":
    unittest.main()

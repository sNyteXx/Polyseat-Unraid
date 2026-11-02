"""CI runtime reuse: content-addressed key and fail-closed verification (offline; API answers from fixtures)."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
KEY = REPO / "unraid/scripts/ci-runtime-key.py"
REUSE = REPO / "unraid/scripts/ci-runtime-reuse.py"
RUNNER = {"ImageOS": "ubuntu24", "ImageVersion": "20261026.1", "RUNNER_ARCH": "X64", "RUNNER_OS": "Linux"}


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


keymod = load(KEY, "ci_runtime_key")


class KeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="polyseat-key-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        for rel in (*keymod.INPUT_FILES, "unraid/build-pins.env", "unraid/docs/acceptance.md"):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / rel, self.repo / rel)
        for rel in keymod.INPUT_DIRS:
            shutil.copytree(REPO / rel, self.repo / rel)

    def key(self, env=None):
        return keymod.key_of(keymod.inputs(self.repo, env or RUNNER))

    def test_deterministic_and_matches_the_cli(self):
        self.assertEqual(self.key(), self.key())
        out = subprocess.run([sys.executable, "-I", str(KEY), "--repo", str(self.repo)], env={**RUNNER, "PATH": "/usr/bin:/bin"},
                             capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, self.key())

    def test_every_runtime_input_changes_the_key(self):
        base = self.key()
        for rel in (*keymod.INPUT_FILES, "unraid/abi/unraid-7.3.3-x86_64.sonames", "unraid/patches/incus/host-veth-mac.patch"):
            with self.subTest(rel=rel):
                path = self.repo / rel
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                self.assertNotEqual(self.key(), base)
                path.write_bytes(original)
        pins = self.repo / "unraid/build-pins.env"
        text = pins.read_text()
        pins.write_text(text.replace("LXC_RELEASE=v6.0.6", "LXC_RELEASE=v6.0.7"))
        self.assertNotEqual(self.key(), base)
        pins.write_text(text)
        for name, value in (("ImageVersion", "20261102.1"), ("ImageOS", "ubuntu26"), ("RUNNER_ARCH", "ARM64")):
            self.assertNotEqual(self.key({**RUNNER, name: value}), base, name)

    def test_seat_pins_docs_and_controller_do_not_invalidate_the_runtime(self):
        base = self.key()
        pins = self.repo / "unraid/build-pins.env"
        pins.write_text(pins.read_text().replace("SEAT_ARCHIVE_DATE=2026/10/29", "SEAT_ARCHIVE_DATE=2026/10/30"))
        (self.repo / "unraid/docs/acceptance.md").write_text("changed")
        self.assertEqual(self.key(), base)

    def test_missing_input_refuses(self):
        (self.repo / "unraid/lab/bundle.py").unlink()
        with self.assertRaises(ValueError):
            self.key()

    def test_input_list_covers_every_build_reference(self):
        listed = set(keymod.INPUT_FILES)
        core = (REPO / "unraid/scripts/lib/build-core.sh").read_text()
        for ref in sorted(set(re.findall(r"\$CORE_REPO/(unraid/[A-Za-z0-9_./-]+)", core))):
            if ref == "unraid/plugin/src/emhttp/scripts/polyseatctl":
                continue  # a consistency check (INCUSD_REQUIRED_PROGRAMS), re-run on every reuse, not a byte input
            if ref == "unraid/build-pins.env":
                continue  # covered key by key (runtime pins), see test_every_runtime_input_changes_the_key
            with self.subTest(ref=ref):
                self.assertTrue(ref in listed or any(ref.startswith(d) for d in keymod.INPUT_DIRS), ref)
        for script in ("check-bundle-abi.py", "make-runtime-bundle.py", "make-runtime-sbom.py", "make_proof_sbom.py",
                       "prepare-incus-deps.py", "make-canary-image.py"):
            for module in re.findall(r"^from unraid\.lab import ([a-z_]+)", (REPO / "unraid/scripts" / script).read_text(), re.M):
                self.assertIn(f"unraid/lab/{module}.py", listed, script)
        self.assertIn("make-canary-image.py", (REPO / "unraid/scripts/prepare-canary-image.sh").read_text())


class ReuseTests(unittest.TestCase):
    REPOSITORY = "sNyteXx/Polyseat-Unraid"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="polyseat-reuse-")
        self.addCleanup(self.tmp.cleanup)
        d = self.d = Path(self.tmp.name)
        proof = d / "proof"
        (proof / "bundle").mkdir(parents=True)
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w:gz") as tar:
            info = tarfile.TarInfo("payload/bin/incusd")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"ELF!"))
        sha = hashlib.sha256(raw.getvalue()).hexdigest()
        self.sha = sha
        (proof / f"bundle/{sha}.tar.gz").write_bytes(raw.getvalue())
        (proof / f"bundle/{sha}.manifest.json").write_text(json.dumps({"archive_sha256": sha}))
        (proof / "bundle.env").write_text(f"BUNDLE_SHA256={sha}\n")
        (proof / "build-info.txt").write_text("x\n")
        sums = "".join(f"{hashlib.sha256((proof / n).read_bytes()).hexdigest()}  {n}\n"
                       for n in (f"bundle/{sha}.tar.gz", "bundle.env", "build-info.txt"))
        (proof / "SHA256SUMS").write_text(sums)
        stage = d / "stage"
        (stage / "go-bin").mkdir(parents=True)
        (stage / "go-bin/incusd").write_text("bin")
        (stage / "prefix/lib").mkdir(parents=True)
        (stage / "prefix/lib/libraft.so.0").write_text("lib")
        (stage / "prefix/lib/libraft.so").symlink_to("libraft.so.0")
        (stage / "prefix/tools").mkdir(parents=True)
        (stage / "prefix/tools/unsquashfs").write_text("tool")
        (stage / "incus/test/deps").mkdir(parents=True)
        (stage / "incus/test/deps/import-busybox").write_text("#!/bin/sh\n")
        self.key_json = d / "key.json"
        self.key_json.write_text(json.dumps({"key": "k" * 64, "inputs": {"schema": 1, "files": {}, "pins": {}, "runner": RUNNER}}))
        self.snap = d / "snap"
        self.cli(["snapshot", "--proof", proof, "--stage", stage, "--out", self.snap])
        self.cli(["seal", "--snapshot", self.snap, "--key-json", self.key_json, "--repository", self.REPOSITORY,
                  "--run-id", "41212926028", "--run-attempt", "1", "--head-sha", "f" * 40,
                  "--artifact-id", "13045000000", "--artifact-digest", "sha256:" + "d" * 64])
        self.api = {
            f"/repos/{self.REPOSITORY}/actions/runs/41212926028": {
                "repository": {"full_name": self.REPOSITORY}, "path": ".github/workflows/incus-feasibility.yml",
                "head_sha": "f" * 40, "run_attempt": 1, "status": "completed", "conclusion": "success"},
            f"/repos/{self.REPOSITORY}/actions/artifacts/13045000000": {
                "name": "incus-build-proof-NOT-FOR-UNRAID", "expired": False, "workflow_run": {"id": 41212926028},
                "digest": "sha256:" + "d" * 64}}

    def cli(self, args, check=True):
        return subprocess.run([sys.executable, "-I", str(REUSE), *map(str, args)], capture_output=True, text=True,
                              check=check, env={"PATH": "/usr/bin:/bin"})

    def verify(self):
        fixture = self.d / "api.json"
        fixture.write_text(json.dumps(self.api))
        res = self.cli(["verify", "--snapshot", self.snap, "--key-json", self.key_json, "--repository", self.REPOSITORY,
                        "--api-fixture", fixture], check=False)
        return res.returncode, json.loads(res.stdout)

    def test_sealed_snapshot_of_a_green_producer_is_reusable_and_restorable(self):
        rc, verdict = self.verify()
        self.assertEqual(rc, 0, verdict)
        self.assertEqual(verdict["bundle_sha256"], self.sha)
        self.cli(["restore", "--snapshot", self.snap, "--proof-out", self.d / "out/proof", "--stage-out", self.d / "out/stage"])
        self.assertEqual((self.d / "out/proof/bundle.env").read_text(), f"BUNDLE_SHA256={self.sha}\n")
        self.assertTrue((self.d / "out/stage/prefix/lib/libraft.so").is_symlink())

    def assertNotReusable(self, reason_part):
        rc, verdict = self.verify()
        self.assertEqual(rc, 3, verdict)
        self.assertFalse(verdict["reusable"])
        self.assertIn(reason_part, verdict["reason"])

    def test_tampered_bundle_byte(self):
        archive = self.snap / f"proof/bundle/{self.sha}.tar.gz"
        data = bytearray(archive.read_bytes())
        data[-1] ^= 1
        archive.write_bytes(bytes(data))
        self.assertNotReusable("snapshot content differs")

    def test_extra_or_missing_file(self):
        (self.snap / "stage/go-bin/extra").write_text("x")
        self.assertNotReusable("snapshot content differs")
        (self.snap / "stage/go-bin/extra").unlink()
        (self.snap / "stage/go-bin/incusd").unlink()
        self.assertNotReusable("snapshot content differs")

    def test_key_drift(self):
        data = json.loads(self.key_json.read_text())
        data["inputs"]["runner"]["ImageVersion"] = "next"
        self.key_json.write_text(json.dumps(data))
        self.assertNotReusable("key or key inputs differ")

    def test_unsealed_snapshot(self):
        (self.snap / "runtime-reuse.json").unlink()
        self.assertNotReusable("no sealed reuse record")

    def test_producer_failed_other_commit_attempt_or_workflow(self):
        run = self.api[f"/repos/{self.REPOSITORY}/actions/runs/41212926028"]
        for field, value in (("conclusion", "failure"), ("head_sha", "e" * 40), ("run_attempt", 2),
                             ("path", ".github/workflows/other.yml"), ("status", "in_progress")):
            with self.subTest(field=field):
                saved = run[field]
                run[field] = value
                self.assertNotReusable("producer run")
                run[field] = saved

    def test_producer_artifact_expired_or_other_digest(self):
        art = self.api[f"/repos/{self.REPOSITORY}/actions/artifacts/13045000000"]
        for field, value in (("expired", True), ("digest", "sha256:" + "0" * 64), ("name", "something-else")):
            with self.subTest(field=field):
                saved = art[field]
                art[field] = value
                self.assertNotReusable("producer proof artifact")
                art[field] = saved

    def test_api_unreachable_means_rebuild(self):
        del self.api[f"/repos/{self.REPOSITORY}/actions/runs/41212926028"]
        self.assertNotReusable("API")

    def test_escaping_symlink_is_refused(self):
        (self.snap / "stage/prefix/lib/evil").symlink_to("/etc/passwd")
        self.assertNotReusable("symlink leaves the snapshot")


class WorkflowCacheTests(unittest.TestCase):
    """actions/cache only finds an entry if restore and save use the same path (it is part of the cache version):
    run 41215993159 missed the entry saved by 41214824348 because the save used another directory."""

    WORKFLOWS = (".github/workflows/incus-feasibility.yml", ".github/workflows/unraid-preflight-lint.yml")
    PATH = "${{ runner.temp }}/runtime-snapshot"
    PREFIX = "polyseat-runtime-v1-${{ steps.key.outputs.key }}-"
    KEY = PREFIX + "${{ github.run_id }}-${{ github.run_attempt }}"

    @staticmethod
    def cache_steps(text):
        steps = []
        for match in re.finditer(r"uses: actions/cache/(restore|save)@[0-9a-f]{40}[^\n]*\n\s+with:\n((?:\s{10}\S[^\n]*\n)+)", text):
            fields = dict(line.strip().split(": ", 1) for line in match.group(2).splitlines())
            steps.append((match.group(1), fields))
        return steps

    def test_restore_and_save_share_path_and_key_scheme(self):
        kinds = []
        for rel in self.WORKFLOWS:
            text = (REPO / rel).read_text()
            self.assertNotIn("runtime-snapshot-new", text, rel)
            self.assertNotIn("cache-hit", text, rel)  # never 'true' with prefix lookups; use cache-matched-key
            for kind, fields in self.cache_steps(text):
                kinds.append((rel, kind))
                with self.subTest(rel=rel, kind=kind):
                    self.assertEqual(fields["path"], self.PATH)
                    self.assertEqual(fields["key"], self.KEY)
                    if kind == "restore":
                        self.assertEqual(fields["restore-keys"], self.PREFIX)
                        self.assertIn("MATCHED: ${{ steps.cache.outputs.cache-matched-key }}", text)
                        self.assertIn('"polyseat-runtime-v1-$KEY-"*)', text)
        self.assertEqual(sorted(kinds), sorted([(self.WORKFLOWS[0], "restore"), (self.WORKFLOWS[0], "save"),
                                                (self.WORKFLOWS[1], "restore")]))

    def test_snapshot_seal_and_save_use_one_directory(self):
        text = (REPO / self.WORKFLOWS[0]).read_text()
        self.assertIn('--out "$RUNNER_TEMP/runtime-snapshot" > /dev/null', text)
        self.assertIn('seal --snapshot "$RUNNER_TEMP/runtime-snapshot" ', text)
        self.assertIn('snap="$RUNNER_TEMP/runtime-snapshot"', text)


if __name__ == "__main__":
    unittest.main()

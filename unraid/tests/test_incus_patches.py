"""Polyseat's source patches to the pinned Incus: pinned, minimal, and fail-closed when applied."""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PATCH = REPO / "unraid/patches/incus/host-veth-mac.patch"
CORE = REPO / "unraid/scripts/lib/build-core.sh"
PINS = dict(line.split("=", 1) for line in (REPO / "unraid/build-pins.env").read_text().splitlines()
            if line and not line.startswith("#"))


class HostVethMacPatch(unittest.TestCase):
    """Tower run 9: a bridge without an explicit address took Incus' random host-side veth MAC (br0, 10 minutes)."""

    def test_pinned_and_limited_to_the_veth_pair_creation(self):
        data = PATCH.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), PINS["INCUS_PATCH_HOST_VETH_MAC_SHA256"])
        text = data.decode()
        self.assertEqual(re.findall(r"^\+\+\+ b/(\S+)", text, re.M), ["internal/server/device/device_utils_network.go"])
        added = [line[1:] for line in text.splitlines() if line.startswith("+") and not line.startswith("+++")]
        removed = [line for line in text.splitlines() if line.startswith("-") and not line.startswith("---")]
        self.assertEqual(removed, [])  # purely additive
        self.assertIn('\t"crypto/rand"', added)
        self.assertIn("\thostMAC[0] = 0xfe", added)
        self.assertIn("\tveth.Address = hostMAC", added)
        self.assertIn("veth.Peer.Address = hwaddr", text)  # anchored right after the peer MAC is set

    def apply(self, sha, patch_text):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = tmp / "incus"
            src.mkdir()
            (src / "f.txt").write_text("one\ntwo\n")
            subprocess.run(["git", "init", "-q", str(src)], check=True)
            repo = tmp / "repo"
            (repo / "unraid/patches/incus").mkdir(parents=True)
            (repo / "unraid/patches/incus/host-veth-mac.patch").write_text(patch_text)
            script = ('die() { echo "DIE: $*" >&2; exit 2; }; . "$1"; CORE_REPO="$2"; '
                      'INCUS_PATCH_HOST_VETH_MAC_SHA256="$3" core_apply_incus_patches "$4"; cat "$4.polyseat-patches.txt"; cat "$4/f.txt"')
            return subprocess.run(["sh", "-c", script, "x", str(CORE), str(repo), sha, str(src)],
                                  capture_output=True, text=True)

    GOOD = "--- a/f.txt\n+++ b/f.txt\n@@ -1,2 +1,3 @@\n one\n+inserted\n two\n"

    def test_applies_only_a_patch_with_the_pinned_hash_that_fits_the_source(self):
        sha = hashlib.sha256(self.GOOD.encode()).hexdigest()
        res = self.apply(sha, self.GOOD)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, f"{sha}  host-veth-mac.patch\none\ninserted\ntwo\n")
        res = self.apply("0" * 64, self.GOOD)
        self.assertEqual(res.returncode, 2)
        self.assertIn("does not match its pin", res.stderr)
        bad = self.GOOD.replace(" one\n", " ONE\n")
        res = self.apply(hashlib.sha256(bad.encode()).hexdigest(), bad)
        self.assertEqual(res.returncode, 2)
        self.assertIn("does not apply to the pinned source", res.stderr)

    def test_build_applies_patches_first_and_records_them_in_the_proof(self):
        core = CORE.read_text()
        body = core.split("core_build_incus() { # incus_src\n", 1)[1]
        self.assertTrue(body.startswith("  core_apply_incus_patches \"$1\"\n"))
        self.assertIn('cp "$src.polyseat-patches.txt" "$output/incus-source-patches.txt"', core)
        self.assertIn("incus-source-patches.txt", core.split("> SHA256SUMS", 1)[0].rsplit("sha256sum --", 1)[1])

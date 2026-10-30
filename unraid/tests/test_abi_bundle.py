"""ABI closure and deterministic bundle builder tests (offline, /tmp only)."""

from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

from unraid.lab import abi, bundle
from unraid.lab.archive import verify

HERE = Path(__file__).resolve().parent
ABI = HERE.parent / "abi"


def baseline():
    sonames = abi.parse_baseline_sonames(
        (ABI / "unraid-7.3.3-x86_64.sonames").read_text(encoding="utf-8"))
    symvers = abi.parse_baseline_symvers(
        (ABI / "unraid-7.3.3-x86_64.symvers").read_text(encoding="utf-8"))
    return sonames, symvers


def fixture_elf(name: str, path: str) -> abi.Elf:
    dynamic = abi.parse_dynamic((HERE / "fixtures" / f"{name}-elf-dynamic.txt").read_text())
    versions = abi.parse_version_needs((HERE / "fixtures" / f"{name}-elf-versions.txt").read_text())
    return abi.Elf(path, dynamic["needed"], dynamic["soname"], versions, dynamic["runpath"])


class AbiTests(unittest.TestCase):
    def test_parse_real_incusd_needs(self):
        elf = fixture_elf("incusd", "bin/incusd")
        self.assertIn("liblxc.so.1", elf.needed)
        self.assertIn("libcowsql.so.0", elf.needed)
        self.assertEqual(abi.glibc_max([elf.versions]), "2.38")
        self.assertIn("LIBUDEV_199", elf.versions["libudev.so.1"])

    def test_current_ci_binary_is_not_installable_on_unraid(self):
        sonames, symvers = baseline()
        report = abi.check([fixture_elf("incusd", "bin/incusd")], sonames, symvers)
        self.assertFalse(report.ok)
        self.assertEqual(report.missing_libraries, ["libcowsql.so.0", "liblxc.so.1"])
        self.assertEqual(report.missing_symbol_versions, {})
        self.assertLessEqual(abi._vkey(report.glibc_max), abi._vkey("2.44"))

    def test_bundling_the_missing_libraries_closes_the_set(self):
        sonames, symvers = baseline()
        libs = [abi.Elf("lib/liblxc.so.1", ["libcap.so.2", "libseccomp.so.2", "libc.so.6"],
                        "liblxc.so.1", {}),
                abi.Elf("lib/libcowsql.so.0", ["libuv.so.1", "libsqlite3.so.0",
                                               "liblz4.so.1", "libraft.so.0"],
                        "libcowsql.so.0", {}),
                abi.Elf("lib/libraft.so.0", ["libuv.so.1", "liblz4.so.1"],
                        "libraft.so.0", {})]
        report = abi.check([fixture_elf("incusd", "bin/incusd"), *libs], sonames, symvers)
        self.assertTrue(report.ok, report.as_dict())
        self.assertIn("libudev.so.1", report.host_libraries)
        self.assertEqual(sorted(report.bundled_libraries),
                         ["libcowsql.so.0", "liblxc.so.1", "libraft.so.0"])

    def test_selinux_and_apparmor_links_are_flagged_missing(self):
        sonames, symvers = baseline()
        lxc = abi.Elf("lib/liblxc.so.1", ["libselinux.so.1", "libapparmor.so.1"],
                      "liblxc.so.1", {})
        report = abi.check([lxc], sonames, symvers)
        self.assertEqual(report.missing_libraries, ["libapparmor.so.1", "libselinux.so.1"])

    def test_newer_symbol_version_than_host_defines_is_rejected(self):
        sonames, symvers = baseline()
        elf = abi.Elf("bin/x", ["libudev.so.1", "libc.so.6"], None,
                      {"libudev.so.1": {"LIBUDEV_999"}, "libc.so.6": {"GLIBC_2.99"}})
        report = abi.check([elf], sonames, symvers)
        self.assertFalse(report.ok)
        self.assertEqual(report.missing_symbol_versions["libudev.so.1"], ["LIBUDEV_999"])
        self.assertEqual(report.missing_symbol_versions["libc.so.6"], ["GLIBC_2.99"])

    def test_unprofiled_host_library_versions_cannot_be_proven(self):
        sonames, symvers = baseline()
        sonames = sonames | {"libmystery.so.1"}
        elf = abi.Elf("bin/x", ["libmystery.so.1"], None, {"libmystery.so.1": {"MYS_1"}})
        report = abi.check([elf], sonames, symvers)
        self.assertFalse(report.ok)

    def test_runpath_must_be_origin_relative(self):
        sonames, symvers = baseline()
        elf = abi.Elf("bin/x", [], None, {}, ["/usr/lib64/evil"])
        self.assertFalse(abi.check([elf], sonames, symvers).ok)
        elf = abi.Elf("bin/x", [], None, {}, ["$ORIGIN/../lib"])
        self.assertTrue(abi.check([elf], sonames, symvers).ok)

    def test_malformed_names_and_baseline_fail_closed(self):
        with self.assertRaises(abi.AbiError):
            abi.parse_dynamic("(NEEDED) Shared library: [../../etc/passwd]")
        with self.assertRaises(abi.AbiError):
            abi.parse_baseline_symvers("libc.so.6 a b")


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="polyseat-bundle-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = self.root / "payload-src"
        (self.payload / "bin").mkdir(parents=True)
        (self.payload / "lib").mkdir()
        (self.payload / "bin" / "incusd").write_bytes(b"#!/bin/sh\nexit 0\n")
        (self.payload / "bin" / "incusd").chmod(0o755)
        (self.payload / "lib" / "liblxc.so.1").write_bytes(b"lib")
        (self.payload / "lib" / "liblxc.so.1").chmod(0o600)
        self.out = self.root / "out"
        self.out.mkdir()

    def build(self, out=None, **kwargs):
        args = dict(name="polyseat-incus", version="7.5.1-test", incus_version="7.5.1",
                    min_unraid="7.2.0", min_glibc="2.38", needed_libs=["libudev.so.1"])
        args.update(kwargs)
        return bundle.build(self.payload, out or self.out, **args)

    def test_bundle_is_verified_and_normalised(self):
        archive, manifest_path = self.build()
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(archive.name, manifest["archive_sha256"] + ".tar.gz")
        modes = {f["path"]: f["mode"] for f in manifest["files"]}
        self.assertEqual(modes, {"payload/bin/incusd": 0o755, "payload/lib/liblxc.so.1": 0o644})
        with tarfile.open(archive, "r:gz") as tar:
            verify(tar, manifest)
            for member in tar:
                self.assertEqual((member.uid, member.gid, member.mtime), (0, 0, 0))

    def test_bundle_is_reproducible(self):
        first = self.root / "a"
        second = self.root / "b"
        first.mkdir()
        second.mkdir()
        a, _ = self.build(first)
        os.utime(self.payload / "bin" / "incusd", (123, 123))
        b, _ = self.build(second)
        self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_symlink_and_special_payload_files_are_rejected(self):
        os.symlink("/etc/passwd", self.payload / "bin" / "link")
        with self.assertRaises(bundle.BundleError):
            self.build()

    def test_unsafe_metadata_is_rejected(self):
        for kwargs in ({"version": "../x"}, {"name": "Bad Name"},
                       {"needed_libs": ["../libc"]}, {"min_glibc": "2;rm"}):
            with self.assertRaises(bundle.BundleError):
                self.build(**kwargs)

    def test_hidden_file_cannot_be_smuggled(self):
        (self.payload / "bin" / ".hidden").write_bytes(b"x")
        with self.assertRaises(bundle.BundleError):
            self.build()

    def test_built_without_is_recorded_and_validated(self):
        _, manifest_path = self.build()
        self.assertEqual(json.loads(manifest_path.read_text())["built_without"], ["apparmor", "selinux"])
        for bad in (("tomoyo",), ("apparmor", "apparmor")):
            with self.assertRaises(bundle.BundleError):
                self.build(built_without=bad)

    def test_existing_output_is_not_overwritten(self):
        self.build()
        with self.assertRaises(bundle.BundleError):
            self.build()


if __name__ == "__main__":
    unittest.main()

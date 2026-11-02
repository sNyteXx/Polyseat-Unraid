"""Offline canary image tests: no binary execution, daemon, network or host changes."""
import gzip
import hashlib
import importlib.util
import io
from pathlib import Path
import struct
import tarfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/make-canary-image.py"
SPEC = importlib.util.spec_from_file_location("canary_image", SCRIPT)
IMAGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IMAGE)


def fake_binary():
    """Deliberately non-runnable ELF fixture; never used as real compatibility evidence."""
    data = bytearray(70000)
    struct.pack_into("<16sHHIQQQIHHHHHH", data, 0,
                     b"\x7fELF\x02\x01\x01" + b"\0" * 9, 2, 62, 1, 0, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    struct.pack_into("<IIQQQQQQ", data, 64, 1, 5, 0, 0, 0, len(data), len(data), 4096)
    data[1000:1016] = b"BusyBox v1.36.1\0\0"
    return bytes(data)


class CanaryImageTests(unittest.TestCase):
    def setUp(self):
        self.binary = fake_binary()
        self.sha = hashlib.sha256(self.binary).hexdigest()

    def build(self, binary=None, sha=None, version="1.36.1"):
        return IMAGE.build_image(binary or self.binary, binary_sha256=sha or self.sha, version=version)

    def test_image_is_deterministic_and_exactly_validated(self):
        first = self.build()
        self.assertEqual(first, self.build())
        report = IMAGE.validate_image(first, binary_sha256=self.sha, version="1.36.1")
        self.assertEqual(report["binary_sha256"], self.sha)
        self.assertEqual(report["source_date_epoch"], 0)
        self.assertEqual(report["image_creation_date"], 1)
        with tarfile.open(fileobj=io.BytesIO(first), mode="r:gz") as tar:
            self.assertFalse(any(m.isdev() or m.isfifo() for m in tar))
            self.assertIn(b"\ncreation_date: 1\n", tar.extractfile("metadata.yaml").read())
            self.assertTrue(all(member.mtime == 0 for member in tar.getmembers()))
            self.assertEqual(tar.extractfile("rootfs/sbin/init").read(), IMAGE.INIT)
            self.assertEqual(tar.getmember("rootfs/bin/sh").linkname, "busybox")

    def test_incus_requires_a_fixed_positive_creation_date(self):
        image = self.build()
        for replacement in (b"", b"creation_date: 0\n", b"creation_date: -1\n",
                            b"creation_date: null\n", b"creation_date: 2\n",
                            b"creation_date: 1\ncreation_date: 1\n"):
            with self.subTest(creation_date=replacement):
                target = io.BytesIO()
                with gzip.GzipFile(filename="", fileobj=target, mode="wb", mtime=0, compresslevel=9) as gz:
                    with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as out:
                        with tarfile.open(fileobj=io.BytesIO(image), mode="r:gz") as source:
                            for member in source:
                                content = source.extractfile(member).read() if member.isreg() else None
                                if member.name == "metadata.yaml":
                                    content = content.replace(b"creation_date: 1\n", replacement)
                                    member.size = len(content)
                                out.addfile(member, io.BytesIO(content) if content is not None else None)
                with self.assertRaisesRegex(ValueError, "fixed positive creation_date"):
                    IMAGE.validate_image(target.getvalue())

    def test_bad_hash_and_mismatching_version_block(self):
        for kw in ({"sha": "0" * 64}, {"version": "1.37.0"}, {"version": "x"}):
            with self.assertRaises(ValueError):
                self.build(**kw)

    def test_dynamic_interpreter_or_wrong_architecture_block(self):
        for offset, value, fmt in ((64, 2, "<I"), (64, 3, "<I"), (18, 183, "<H")):
            data = bytearray(self.binary)
            struct.pack_into(fmt, data, offset, value)
            with self.assertRaises(ValueError):
                self.build(bytes(data), hashlib.sha256(data).hexdigest())

    def test_bad_header_bounds_and_elf_magic_block(self):
        for offset, value, fmt in ((32, 1000000, "<Q"), (54, 0, "<H"), (0, 0, "<I")):
            data = bytearray(self.binary)
            struct.pack_into(fmt, data, offset, value)
            with self.assertRaises(ValueError):
                self.build(bytes(data), hashlib.sha256(data).hexdigest())

    def test_tampering_extra_trailers_and_different_binary_hash_block(self):
        image = self.build()
        with self.assertRaises(ValueError):
            IMAGE.validate_image(image + b"trailer")
        with self.assertRaises(ValueError):
            IMAGE.validate_image(image, binary_sha256="0" * 64)

    def test_host_device_extra_payload_or_changed_init_block(self):
        image = self.build()
        for extra in ("rootfs/dev/nvidia0", "rootfs/etc/evil", "rootfs/sbin/init"):
            target = io.BytesIO()
            with tarfile.open(fileobj=target, mode="w:gz") as out:
                with tarfile.open(fileobj=io.BytesIO(image), mode="r:gz") as source:
                    for member in source:
                        if member.name == extra:
                            continue
                        content = source.extractfile(member) if member.isreg() else None
                        out.addfile(member, content)
                entry = tarfile.TarInfo(extra)
                if extra.endswith("nvidia0"):
                    entry.type = tarfile.CHRTYPE
                    entry.devmajor, entry.devminor = 195, 0
                    out.addfile(entry)
                else:
                    payload = b"#!/bin/sh\nmodprobe vfio-pci\n"
                    entry.size = len(payload)
                    entry.mode = 0o755
                    out.addfile(entry, io.BytesIO(payload))
            with self.assertRaises(ValueError):
                IMAGE.validate_image(target.getvalue())


if __name__ == "__main__":
    unittest.main()

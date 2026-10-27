"""Verify exact PNG content and read-only source/state behavior."""
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from ops.images.prepare_png_cache import (SIGNATURE, build_cache, chunk,
                                          inspect_png, recompress_png)


def synthetic_png():
    width, height = 128, 128
    row = b"\x00" + bytes((n * 17) % 256 for n in range(width * 3))
    raw = row * height
    stream = zlib.compress(raw, level=0)
    return (SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"tEXt", b"Comment\x00preserve this metadata")
            + chunk(b"IDAT", stream[:100]) + chunk(b"IDAT", stream[100:]) + chunk(b"IEND", b""))


class PngCacheTests(unittest.TestCase):
    def test_all_non_idat_chunks_and_filtered_pixels_are_identical(self):
        original = synthetic_png()
        optimized, digest = recompress_png(original)
        before, raw_before = inspect_png(original)
        after, raw_after = inspect_png(optimized)
        self.assertLess(len(optimized), len(original))
        self.assertEqual(raw_before, raw_after)
        self.assertEqual([c for c in before if c[0] != b"IDAT"],
                         [c for c in after if c[0] != b"IDAT"])
        self.assertEqual(len(digest), 64)

    def test_truncated_corrupt_and_animated_pngs_are_rejected(self):
        valid = synthetic_png()
        corrupt = bytearray(valid)
        corrupt[-5] ^= 1
        animated = valid[:33] + chunk(b"acTL", struct.pack(">II", 2, 0)) + valid[33:]
        for data in [valid[:-1], bytes(corrupt), b"not PNG", animated]:
            with self.subTest(size=len(data)), self.assertRaises((ValueError, zlib.error)):
                recompress_png(data)

    def test_invalid_zlib_and_nonconsecutive_idat_are_rejected(self):
        original = synthetic_png()
        chunks, _ = inspect_png(original)
        broken = SIGNATURE + b"".join(chunk(k, b"bad zlib" if k == b"IDAT" else p) for k, p in chunks)
        separated = SIGNATURE + b"".join(chunk(k, p) + (chunk(b"tEXt", b"x\x00y") if i == 2 else b"")
                                        for i, (k, p) in enumerate(chunks))
        for data in [broken, separated]:
            with self.assertRaises((ValueError, zlib.error)):
                recompress_png(data)

    def test_cache_build_preserves_sources_and_only_writes_separate_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "data" / "CPTAC" / "vis_check" / "original.png"
            source.parent.mkdir(parents=True)
            original = synthetic_png()
            source.write_bytes(original)
            item = {"image_id": "image_a", "dataset": "CPTAC", "image_path": "/old/location/original.png"}
            stat_before = source.stat()
            report = build_cache([item], root / "data", root / "cache", root / "report.json")
            self.assertEqual(report["checked"], 1)
            self.assertTrue(report["all_content_verified"])
            self.assertEqual(report["cached"], 1)
            cache = root / "cache" / "_star_image_files" / "CPTAC" / "vis_check" / "original.png"
            self.assertEqual(inspect_png(cache.read_bytes())[1], inspect_png(original)[1])
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(source.stat().st_mtime_ns, stat_before.st_mtime_ns)
            self.assertEqual(json.loads((root / "report.json").read_text())["checked"], 1)
            with self.assertRaises(ValueError):
                build_cache([item], root / "data", root / "data" / "cache", root / "report.json")

    def test_no_savings_removes_only_that_cached_copy_and_keeps_original(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "data" / "CPTAC" / "vis_check" / "small.png"
            source.parent.mkdir(parents=True)
            optimized, _ = recompress_png(synthetic_png())
            source.write_bytes(optimized)
            cache = root / "cache" / "_star_image_files" / "CPTAC" / "vis_check" / "small.png"
            cache.parent.mkdir(parents=True)
            cache.write_bytes(b"old cached file")
            item = {"image_id": "small", "dataset": "CPTAC", "image_path": "small.png"}
            report = build_cache([item], root / "data", root / "cache", root / "report.json")
            self.assertEqual(report["cached"], 0)
            self.assertFalse(cache.exists())
            self.assertEqual(source.read_bytes(), optimized)


if __name__ == "__main__":
    unittest.main()

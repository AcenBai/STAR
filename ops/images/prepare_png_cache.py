"""Build smaller PNG copies without changing a single filtered image byte.

Uses only the Python standard library. Originals, manifests and user state are
read-only. Nginx falls back to the original when a cached copy does not exist.
"""
import argparse
import binascii
import hashlib
import json
import os
from pathlib import Path
import struct
import time
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_STREAM = 64 * 1024 * 1024
DATASETS = {"CPTAC", "EAY131", "R2Seg"}


def chunk(kind, payload):
    return (struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", binascii.crc32(kind + payload) & 0xffffffff))


def inspect_png(data):
    if not data.startswith(SIGNATURE):
        raise ValueError("Invalid PNG signature")
    chunks = []
    position = len(SIGNATURE)
    while position < len(data):
        if position + 12 > len(data):
            raise ValueError("Truncated PNG chunk")
        length = struct.unpack(">I", data[position:position + 4])[0]
        end = position + 12 + length
        if end > len(data):
            raise ValueError("Truncated PNG payload")
        kind = data[position + 4:position + 8]
        payload = data[position + 8:end - 4]
        crc = struct.unpack(">I", data[end - 4:end])[0]
        if binascii.crc32(kind + payload) & 0xffffffff != crc:
            raise ValueError("PNG CRC mismatch")
        chunks.append((kind, payload))
        position = end
    if (not chunks or chunks[0][0] != b"IHDR" or len(chunks[0][1]) != 13
            or chunks[-1] != (b"IEND", b"")
            or sum(kind == b"IHDR" for kind, _ in chunks) != 1
            or sum(kind == b"IEND" for kind, _ in chunks) != 1):
        raise ValueError("Invalid PNG structure")
    if any(kind in {b"acTL", b"fcTL", b"fdAT"} for kind, _ in chunks):
        raise ValueError("Animated PNG is not supported")
    indices = [i for i, (kind, _) in enumerate(chunks) if kind == b"IDAT"]
    if not indices or indices != list(range(indices[0], indices[-1] + 1)):
        raise ValueError("PNG IDAT chunks must be consecutive")
    stream = b"".join(payload for kind, payload in chunks if kind == b"IDAT")
    decoder = zlib.decompressobj()
    raw = decoder.decompress(stream, MAX_STREAM + 1)
    if len(raw) > MAX_STREAM or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("Invalid or oversized PNG zlib stream")
    return chunks, raw


def recompress_png(data):
    chunks, raw = inspect_png(data)
    # Level 7 keeps the one-off build inexpensive on the small production VM.
    compressed = zlib.compress(raw, level=7)
    replacement = b"".join(chunk(b"IDAT", compressed[i:i + 65536])
                           for i in range(0, len(compressed), 65536))
    result = bytearray(SIGNATURE)
    inserted = False
    for kind, payload in chunks:
        if kind == b"IDAT":
            if not inserted:
                result.extend(replacement)
                inserted = True
        else:
            result.extend(chunk(kind, payload))
    result = bytes(result)
    # This is stronger than a visual comparison: filters, pixels, dimensions,
    # color profiles, alpha and every non-IDAT chunk must be exactly identical.
    checked, checked_raw = inspect_png(result)
    if checked_raw != raw or ([c for c in checked if c[0] != b"IDAT"]
                              != [c for c in chunks if c[0] != b"IDAT"]):
        raise ValueError("Recompressed PNG content changed")
    return result, hashlib.sha256(raw).hexdigest()


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def build_cache(items, data_dir, cache_root, report_path, pause=0):
    data_dir = Path(data_dir).resolve()
    cache_root = Path(cache_root).resolve()
    if (cache_root == data_dir or cache_root.is_relative_to(data_dir)
            or data_dir.is_relative_to(cache_root)):
        raise ValueError("Cache and original data trees must be separate")
    entries = []
    source_stats = {}
    total_source = total_delivered = cached_count = 0
    for number, item in enumerate(items, 1):
        dataset = item["dataset"]
        filename = Path(item["image_path"]).name
        if dataset not in DATASETS or Path(filename).suffix.lower() != ".png":
            raise ValueError("Unexpected image dataset or extension")
        relative = Path(dataset) / "vis_check" / filename
        source = (data_dir / relative).resolve(strict=True)
        if not source.is_relative_to(data_dir) or not source.is_file():
            raise ValueError("Image is outside the original data directory")
        before = source.stat()
        original = source.read_bytes()
        after = source.stat()
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise ValueError("Original image changed during cache generation")
        source_stats[source] = (after.st_mtime_ns, after.st_size)
        optimized, stream_sha = recompress_png(original)
        destination = cache_root / "_star_image_files" / relative
        if destination.is_symlink() or not destination.resolve().is_relative_to(cache_root):
            raise ValueError("Cache destination must remain inside the cache directory")
        smaller = len(optimized) < len(original)
        if smaller:
            atomic_write(destination, optimized)
            if destination.read_bytes() != optimized:
                raise ValueError("Cached file verification failed")
            delivered = optimized
            cached_count += 1
        else:
            destination.unlink(missing_ok=True)
            delivered = original
        total_source += len(original)
        total_delivered += len(delivered)
        entries.append({"image_id": item["image_id"], "cached": smaller,
                        "source_bytes": len(original), "delivered_bytes": len(delivered),
                        "source_sha256": hashlib.sha256(original).hexdigest(),
                        "delivered_sha256": hashlib.sha256(delivered).hexdigest(),
                        "filtered_stream_sha256": stream_sha})
        if number % 250 == 0:
            print(json.dumps({"checked": number, "cached": cached_count,
                              "saved_bytes": total_source - total_delivered}), flush=True)
        if pause:
            time.sleep(pause)
    for source, expected in source_stats.items():
        stat = source.stat()
        if (stat.st_mtime_ns, stat.st_size) != expected:
            raise ValueError("Original image changed before cache generation completed")
    report = {"checked": len(entries), "cached": cached_count,
              "source_bytes": total_source, "delivered_bytes": total_delivered,
              "saved_percent": round(100 * (1 - total_delivered / total_source), 2) if total_source else 0,
              "all_content_verified": True, "entries": entries}
    atomic_write(Path(report_path), json.dumps(report, indent=2).encode())
    print(json.dumps({k: v for k, v in report.items() if k != "entries"}), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--groups", type=Path)
    parser.add_argument("--priority-group")
    parser.add_argument("--pause", type=float, default=0.02)
    args = parser.parse_args()
    items = json.loads(args.manifest.read_text())
    if args.priority_group:
        if not args.groups:
            parser.error("--priority-group requires --groups")
        groups = json.loads(args.groups.read_text())["groups"]
        priority = set(groups[args.priority_group]["image_ids"])
        items.sort(key=lambda item: item["image_id"] not in priority)
    build_cache(items, args.data_dir, args.cache_root, args.report, max(0, args.pause))


if __name__ == "__main__":
    main()

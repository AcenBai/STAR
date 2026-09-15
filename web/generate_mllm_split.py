from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "manifests" / "images.json"
ANSWERS_PATH = PROJECT_ROOT / "answers" / "sg2162.jsonl"
RADIOLOGIST_GROUPS_PATH = PROJECT_ROOT / "assignments" / "radiologist_groups.json"
OUTPUT_PATH = PROJECT_ROOT / "assignments" / "mllm_split.json"


def latest_complete_answers() -> dict[str, dict]:
    latest = {}
    with ANSWERS_PATH.open("r", encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row.get("user_id") == "sg2162" and row.get("stage") == "complete":
                latest[row["image_id"]] = row
    return latest


def radiologist_image_ids() -> set[str]:
    groups = json.loads(RADIOLOGIST_GROUPS_PATH.read_text(encoding="utf-8"))["groups"]
    return {
        image_id
        for group in groups.values()
        for image_id in group["image_ids"]
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze the all-No-issue MLLM benchmark split.")
    parser.add_argument("--force", action="store_true", help="Replace an existing split file.")
    args = parser.parse_args()
    if OUTPUT_PATH.exists() and not args.force:
        raise SystemExit("MLLM split already exists; pass --force to replace it.")

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    answers = latest_complete_answers()
    image_ids = [
        item["image_id"]
        for item in manifest
        if answers.get(item["image_id"], {}).get("q4_quality_issue") == "No issue"
    ]
    if len(image_ids) != 10565 or len(image_ids) != len(set(image_ids)):
        raise SystemExit(f"Expected 10,565 unique No-issue images; found {len(image_ids)}.")

    radiologist_ids = radiologist_image_ids()
    missing = radiologist_ids.difference(image_ids)
    if missing:
        raise SystemExit(f"Radiologist split has {len(missing)} images outside the MLLM split.")

    output = {
        "version": 1,
        "name": "STAR MLLM benchmark",
        "prompt_spec": "assignments/mllm_prompts.json",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ordering": "manifests/images.json order filtered by inclusion rule",
        "source": {
            "annotator": "sg2162",
            "answers_file": str(ANSWERS_PATH.relative_to(PROJECT_ROOT)),
            "answers_sha256": hashlib.sha256(ANSWERS_PATH.read_bytes()).hexdigest(),
            "manifest_file": str(MANIFEST_PATH.relative_to(PROJECT_ROOT)),
            "manifest_sha256": hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest(),
        },
        "inclusion": {"q4_quality_issue": "No issue"},
        "image_count": len(image_ids),
        "radiologist_subset": {
            "file": str(RADIOLOGIST_GROUPS_PATH.relative_to(PROJECT_ROOT)),
            "unique_image_count": len(radiologist_ids),
            "validated_subset": True,
        },
        "image_ids": image_ids,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT_PATH.with_suffix(OUTPUT_PATH.suffix + ".tmp")
    temporary.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    temporary.replace(OUTPUT_PATH)
    print(json.dumps({"image_count": len(image_ids), "radiologist_subset_count": len(radiologist_ids)}, indent=2))


if __name__ == "__main__":
    main()

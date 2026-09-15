from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "manifests" / "images.json"
ANSWERS_PATH = PROJECT_ROOT / "answers" / "sg2162.jsonl"
OUTPUT_PATH = PROJECT_ROOT / "assignments" / "radiologist_groups.json"
MLLM_SPLIT_PATH = PROJECT_ROOT / "assignments" / "mllm_split.json"
REGISTRY_PATH = PROJECT_ROOT / "assignments" / "group_registry.json"
USERS_DIR = PROJECT_ROOT / "users"
SEED = "STAR_final_two_phase_groups_v4"
GROUP_IDS = ("group_a", "group_b", "group_c", "group_d", "group_e")
SHARED_SIZE = 500
EXCLUSIVE_SIZE = 500
SHARED_CELL_TARGETS = {
    ("Chest", "EAY131", "Yes"): 37, ("Chest", "EAY131", "No"): 84,
    ("Chest", "CPTAC", "Yes"): 1, ("Chest", "CPTAC", "No"): 2,
    ("Chest", "R2Seg", "No"): 93,
    ("Abdomen", "EAY131", "Yes"): 41, ("Abdomen", "EAY131", "No"): 105,
    ("Abdomen", "CPTAC", "Yes"): 6, ("Abdomen", "CPTAC", "No"): 18,
    ("Abdomen", "R2Seg", "No"): 47,
    ("Pelvis", "EAY131", "Yes"): 9, ("Pelvis", "EAY131", "No"): 13,
    ("Pelvis", "CPTAC", "Yes"): 3, ("Pelvis", "CPTAC", "No"): 8,
    ("Pelvis", "R2Seg", "No"): 32,
    ("Others", "EAY131", "No"): 1,
}
EXCLUSIVE_CELL_TARGETS = {
    ("Chest", "EAY131", "Yes"): 186, ("Chest", "EAY131", "No"): 421,
    ("Chest", "CPTAC", "Yes"): 2, ("Chest", "CPTAC", "No"): 6,
    ("Chest", "R2Seg", "No"): 468,
    ("Abdomen", "EAY131", "Yes"): 201, ("Abdomen", "EAY131", "No"): 522,
    ("Abdomen", "CPTAC", "Yes"): 34, ("Abdomen", "CPTAC", "No"): 92,
    ("Abdomen", "R2Seg", "No"): 237,
    ("Pelvis", "EAY131", "Yes"): 49, ("Pelvis", "EAY131", "No"): 66,
    ("Pelvis", "CPTAC", "Yes"): 11, ("Pelvis", "CPTAC", "No"): 44,
    ("Pelvis", "R2Seg", "No"): 158,
    ("Others", "EAY131", "No"): 3,
}


def stable_key(value: str) -> str:
    return hashlib.sha256(f"{SEED}:{value}".encode("utf-8")).hexdigest()


def latest_complete_answers() -> dict[str, dict]:
    latest = {}
    with ANSWERS_PATH.open("r", encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row.get("user_id") == "sg2162" and row.get("stage") == "complete":
                latest[row["image_id"]] = row
    return latest


def sampling_lymph_status(record: dict) -> str:
    if record["dataset"] in {"EAY131", "CPTAC"} and record.get("q_lymph_node") == "Yes":
        return "Yes"
    return "No"


def sampling_cell(record: dict) -> tuple[str, str, str]:
    return record["region"], record["dataset"], sampling_lymph_status(record)


def select_sample(records: list[dict], targets: dict[tuple[str, str, str], int], prefix: str) -> list[dict]:
    selected = []
    for cell, target in targets.items():
        candidates = [record for record in records if sampling_cell(record) == cell]
        if len(candidates) < target:
            raise SystemExit(f"Not enough candidates for {cell}: need {target}, found {len(candidates)}.")
        site_buckets = defaultdict(list)
        for record in candidates:
            site_buckets[record.get("q2_site") or ""].append(record)
        ordered_sites = sorted(site_buckets, key=lambda site: (-len(site_buckets[site]), stable_key(site)))
        represented = ordered_sites[:min(target, len(ordered_sites))]
        allocation = {site: int(site in represented) for site in ordered_sites}
        remaining = target - sum(allocation.values())
        capacities = {site: len(site_buckets[site]) - allocation[site] for site in ordered_sites}
        total_capacity = sum(capacities.values())
        fractions = []
        for site, capacity in capacities.items():
            raw = remaining * capacity / total_capacity if total_capacity else 0
            additional = min(capacity, int(raw))
            allocation[site] += additional
            fractions.append((raw - additional, stable_key(site), site))
        still_needed = target - sum(allocation.values())
        for _, _, site in sorted(fractions, reverse=True):
            if not still_needed:
                break
            if allocation[site] < len(site_buckets[site]):
                allocation[site] += 1
                still_needed -= 1
        if still_needed:
            raise SystemExit(f"Could not fill sample quota for {cell}.")
        for site, count in allocation.items():
            ordered = sorted(site_buckets[site], key=lambda row: stable_key(f"{prefix}:{row['image_id']}"))
            selected.extend(ordered[:count])
    if len(selected) != sum(targets.values()) or len(selected) != len({row["image_id"] for row in selected}):
        raise SystemExit(f"{prefix} sample has duplicate images or the wrong size.")
    return selected


def allocate_exclusive(records: list[dict]) -> dict[str, list[dict]]:
    groups = {group_id: [] for group_id in GROUP_IDS}
    lymph_counts = {group_id: defaultdict(int) for group_id in GROUP_IDS}
    dataset_counts = {group_id: defaultdict(int) for group_id in GROUP_IDS}
    region_counts = {group_id: defaultdict(int) for group_id in GROUP_IDS}
    by_cell = defaultdict(list)
    for record in records:
        by_cell[sampling_cell(record)].append(record)
    for cell in sorted(by_cell, key=repr):
        items = sorted(by_cell[cell], key=lambda row: stable_key(f"allocate:{row['image_id']}"))
        base, remainder = divmod(len(items), len(GROUP_IDS))
        cursor = 0
        for group_id in GROUP_IDS:
            assigned = items[cursor:cursor + base]
            groups[group_id].extend(assigned)
            lymph_counts[group_id][cell[2]] += len(assigned)
            dataset_counts[group_id][cell[1]] += len(assigned)
            region_counts[group_id][cell[0]] += len(assigned)
            cursor += base
        recipients = sorted(
            GROUP_IDS,
            key=lambda gid: (
                len(groups[gid]),
                lymph_counts[gid][cell[2]],
                dataset_counts[gid][cell[1]],
                region_counts[gid][cell[0]],
                stable_key(f"remainder:{cell}:{gid}"),
            ),
        )[:remainder]
        for group_id in recipients:
            groups[group_id].append(items[cursor])
            lymph_counts[group_id][cell[2]] += 1
            dataset_counts[group_id][cell[1]] += 1
            region_counts[group_id][cell[0]] += 1
            cursor += 1
    if any(len(group) != EXCLUSIVE_SIZE for group in groups.values()):
        raise SystemExit({group_id: len(group) for group_id, group in groups.items()})
    for group_id in GROUP_IDS:
        groups[group_id].sort(key=lambda row: stable_key(f"phase-2-order:{group_id}:{row['image_id']}"))
    return groups


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze blinded radiologist assignments with overlap.")
    parser.add_argument("--force", action="store_true", help="Replace existing assignment files.")
    args = parser.parse_args()
    if (OUTPUT_PATH.exists() or REGISTRY_PATH.exists()) and not args.force:
        raise SystemExit("Assignment files already exist; pass --force to replace them.")
    if args.force and REGISTRY_PATH.exists():
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        active_claims = [
            group_id for group_id, group in registry.get("groups", {}).items()
            if group.get("assigned_user_id")
            and (USERS_DIR / f"{group['assigned_user_id']}.json").exists()
        ]
        if active_claims:
            raise SystemExit(f"Refusing to replace assignments with active users: {active_claims}")

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest_by_id = {item["image_id"]: item for item in manifest}
    included = []
    for image_id, answer in latest_complete_answers().items():
        if answer.get("q4_quality_issue") != "No issue":
            continue
        item = manifest_by_id.get(image_id)
        if item is None:
            raise SystemExit(f"Completed image is missing from manifest: {image_id}")
        included.append({
            "image_id": image_id,
            "dataset": item["dataset"],
            "region": answer.get("q1_region"),
            "q_lymph_node": answer.get("q_lymph_node"),
            "q2_site": answer.get("q2_site"),
        })

    expected_regions = {"Chest": 4578, "Abdomen": 4589, "Pelvis": 1385, "Others": 13}
    actual_regions = {region: sum(row["region"] == region for row in included) for region in expected_regions}
    if actual_regions != expected_regions or len(included) != 10565:
        raise SystemExit(f"Source cohort changed: got {actual_regions} and {len(included)} images.")

    shared = select_sample(included, SHARED_CELL_TARGETS, "phase-1")
    shared.sort(key=lambda row: stable_key(f"phase-1-order:{row['image_id']}"))
    shared_ids = {row["image_id"] for row in shared}
    exclusive_sample = select_sample(
        [row for row in included if row["image_id"] not in shared_ids],
        EXCLUSIVE_CELL_TARGETS,
        "phase-2-sample",
    )
    exclusive = allocate_exclusive(exclusive_sample)
    groups = {}
    exclusive_ids = []
    for index, group_id in enumerate(GROUP_IDS):
        group_exclusive = exclusive[group_id]
        exclusive_image_ids = [row["image_id"] for row in group_exclusive]
        exclusive_ids.extend(exclusive_image_ids)
        groups[group_id] = {
            "label": f"Group {chr(ord('A') + index)}",
            "phase_1_image_count": SHARED_SIZE,
            "phase_2_image_count": EXCLUSIVE_SIZE,
            "image_count": SHARED_SIZE + EXCLUSIVE_SIZE,
            "phase_1_image_ids": [row["image_id"] for row in shared],
            "phase_2_image_ids": exclusive_image_ids,
            "image_ids": [row["image_id"] for row in shared] + exclusive_image_ids,
        }
    if len(exclusive_ids) != 2500 or len(set(exclusive_ids)) != 2500:
        raise SystemExit("Exclusive groups overlap or have the wrong size.")
    if set(exclusive_ids).intersection(shared_ids):
        raise SystemExit("Shared and exclusive assignments overlap.")
    sampled_ids = set(exclusive_ids).union(shared_ids)
    if len(sampled_ids) != 3000:
        raise SystemExit("Shared and exclusive assignments do not form a 3,000-image sample.")
    if MLLM_SPLIT_PATH.exists():
        mllm_ids = set(json.loads(MLLM_SPLIT_PATH.read_text(encoding="utf-8"))["image_ids"])
        if not sampled_ids.issubset(mllm_ids):
            raise SystemExit("Radiologist sample is not a subset of the MLLM benchmark split.")

    output = {
        "version": 4,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed": SEED,
        "blinded": True,
        "automatic_phase_transition": True,
        "phase_1_image_count": SHARED_SIZE,
        "phase_2_image_count_per_group": EXCLUSIVE_SIZE,
        "stratification_fields": ["region", "dataset", "q2_site", "q_lymph_node"],
        "source": {
            "annotator": "sg2162",
            "answers_file": str(ANSWERS_PATH.relative_to(PROJECT_ROOT)),
            "answers_sha256": hashlib.sha256(ANSWERS_PATH.read_bytes()).hexdigest(),
            "inclusion": {"q4_quality_issue": "No issue"},
            "eligible_image_count": len(included),
            "sampled_unique_image_count": len(sampled_ids),
        },
        "phase_1_image_ids": [row["image_id"] for row in shared],
        "groups": groups,
        "images": sorted(
            [row for row in included if row["image_id"] in sampled_ids],
            key=lambda row: row["image_id"],
        ),
    }
    registry = {
        "version": 4,
        "groups": {
            group_id: {
                "label": group["label"],
                "image_count": group["image_count"],
                "phase_1_image_count": group["phase_1_image_count"],
                "phase_2_image_count": group["phase_2_image_count"],
                "assigned_user_id": None,
                "assigned_at": None,
            }
            for group_id, group in groups.items()
        },
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    for path, data in ((OUTPUT_PATH, output), (REGISTRY_PATH, registry)):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    print(json.dumps({gid: {"phase_1": SHARED_SIZE, "phase_2": EXCLUSIVE_SIZE} for gid in GROUP_IDS}, indent=2))


if __name__ == "__main__":
    main()

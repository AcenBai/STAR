from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SITES_PATH = PROJECT_ROOT / "questions" / "q2_sites.json"
OUTPUT_PATH = PROJECT_ROOT / "assignments" / "mllm_prompts.json"
REGIONS = ["Chest", "Abdomen", "Pelvis", "Others"]
LYMPH_NODE_CHOICES = ["Yes", "No", "Unsure"]
QUALITY_ISSUES = [
    "No issue",
    "Multiple acquisitions visible",
    "Slice misalignment or discontinuity",
    "Limited field of view / anatomical context",
    "Red overlay appears mismatched with tumor",
    "Other issue",
    "Unsure",
]
TERMINAL_CHOICES = {"Other", "Unsure"}


def is_lymph_node(choice: str) -> bool:
    return "lymph node" in choice.lower()


def filtered_choices(sites: dict[str, list[str]], region: str, lymph_node: str) -> list[str]:
    if region == "Others":
        return ["Lymph node", "Bone", "Soft tissue", "Other", "Unsure"]
    choices = sites[region]
    terminal = [choice for choice in choices if choice in TERMINAL_CHOICES]
    if lymph_node == "Yes":
        return [choice for choice in choices if is_lymph_node(choice)] + terminal
    if lymph_node == "No":
        return [
            choice for choice in choices
            if not is_lymph_node(choice) and choice not in TERMINAL_CHOICES
        ] + terminal
    return choices


def main() -> None:
    sites = json.loads(SITES_PATH.read_text(encoding="utf-8"))
    q3_choices = {
        region: {
            lymph_node: filtered_choices(sites, region, lymph_node)
            for lymph_node in LYMPH_NODE_CHOICES
        }
        for region in REGIONS
    }
    output = {
        "version": 1,
        "name": "STAR MLLM Q1-Q4 prompt protocol",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "benchmark_split": "assignments/mllm_split.json",
        "language": "English",
        "interaction": {
            "mode": "sequential",
            "image_visibility": "Show the same benchmark image for Q1, Q2, Q3, and Q4.",
            "answer_policy": "Choose exactly one supplied option and return no explanation.",
            "q3_dependency": "Generate Q3 choices from the model's own Q1 and Q2 answers using q3_choice_matrix.",
            "prohibited_inputs": [
                "ground-truth site",
                "raw tracking ID",
                "filename or filesystem path",
                "dataset label",
                "human answers or accuracy feedback"
            ]
        },
        "system_prompt": (
            "You are answering blinded tumor anatomical localization questions from a medical image. "
            "The tumor is highlighted in red. Use only the visible image pixels. Do not use or infer "
            "answers from an image ID, filename, filesystem path, dataset name, metadata, prior "
            "annotation, or accuracy feedback. For each question, choose exactly one of the supplied "
            "options. Return only the selected option, with no explanation."
        ),
        "questions": [
            {
                "number": "Q1",
                "field": "q1_region",
                "title": "Region",
                "prompt": "Which region is the tumor highlighted in red located in? Choose only one.",
                "choices": REGIONS,
            },
            {
                "number": "Q2",
                "field": "q_lymph_node",
                "title": "Lymph node",
                "prompt": "Is the tumor highlighted in red located in a lymph node? Choose only one.",
                "choices": LYMPH_NODE_CHOICES,
            },
            {
                "number": "Q3",
                "field": "q2_site",
                "title": "Site",
                "prompt_template": (
                    "Which site is the tumor highlighted in red located in? "
                    "Region: {q1_region}; lymph node: {q_lymph_node}. Choose only one."
                ),
                "choices_from": "q3_choice_matrix.{q1_region}.{q_lymph_node}",
            },
            {
                "number": "Q4",
                "field": "q4_quality_issue",
                "title": "Image quality check",
                "prompt": (
                    "Does this image contain any visual issue that could make anatomical "
                    "interpretation unreliable? Choose only one."
                ),
                "choices": QUALITY_ISSUES,
            },
        ],
        "q3_choice_matrix": q3_choices,
        "result_schema": {
            "image_id": "opaque benchmark image ID supplied by the evaluator",
            "q1_region": "exactly one Q1 choice",
            "q_lymph_node": "exactly one Q2 choice",
            "q2_site": "exactly one eligible Q3 choice",
            "q4_quality_issue": "exactly one Q4 choice",
        },
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT_PATH.with_suffix(OUTPUT_PATH.suffix + ".tmp")
    temporary.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    temporary.replace(OUTPUT_PATH)
    print(f"Wrote {OUTPUT_PATH.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()

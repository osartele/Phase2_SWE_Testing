import json
from pathlib import Path

manifest_path = Path("C:/Users/osart/Phase2_SWE_Testing-data_ingestion/phase2/data/processed/manifest.jsonl")

print("=== Patching Manifest ===")
with open(manifest_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

patched_count = 0
with open(manifest_path, "w", encoding="utf-8") as f:
    for line in lines:
        record = json.loads(line)
        
        # Grab the numerical ID that the folders were actually cloned as
        num_id = record.get("repository", {}).get("repo_id")
        if num_id:
            # Force the framework's internal logic to use this exact number
            record["repository_repo_id"] = str(num_id)
            patched_count += 1
            
        f.write(json.dumps(record, sort_keys=True) + "\n")

print(f"Success! Patched {patched_count} records to map to your numerical folders.")
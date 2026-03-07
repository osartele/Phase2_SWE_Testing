import json
import subprocess
from pathlib import Path

manifest_path = "C:/Users/osart/Phase2_SWE_Testing-data_ingestion/phase2/data/processed/manifest.jsonl"
repos_dir = Path("C:/Users/osart/Phase2_SWE_Testing-data_ingestion/repos")

print("=== Starting Git Checkout ===")

with open(manifest_path, "r", encoding="utf-8") as f:
    for line in f:
        record = json.loads(line)
        
        # Grab the repo folder name and the historical commit hash
        repo_id = str(record.get("repository_repo_id") or record.get("repository", {}).get("repo_id"))
        base_commit = record.get("base_commit")

        if repo_id == "None" or not base_commit:
            continue

        target_dir = repos_dir / repo_id

        if target_dir.exists():
            print(f"[REWIND] Setting {repo_id} to commit {base_commit}...")
            # Forces the local folder to travel back in time to the exact dataset commit
            subprocess.run(
                ["git", "checkout", "--force", "--detach", base_commit], 
                cwd=target_dir, 
                capture_output=True
            )

print("=== Checkout Complete ===")

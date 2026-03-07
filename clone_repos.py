import json
import subprocess
from pathlib import Path

manifest_path = "C:/Users/osart/Phase2_SWE_Testing-data_ingestion/phase2/data/processed/manifest.jsonl"
repos_dir = Path("C:/Users/osart/Phase2_SWE_Testing-data_ingestion/repos")

print("=== Starting Mass Repository Clone ===")

with open(manifest_path, "r", encoding="utf-8") as f:
    for line in f:
        record = json.loads(line)
        
        # The native schema mapping guarantees this key exists
        repo_url = record.get("repository_url")
        
        # The framework's native coercer usually keeps the ID nested, or flat if we modified it
        repo_id = str(record.get("repository_repo_id") or record.get("repository", {}).get("repo_id"))

        if repo_id == "None" or not repo_url:
            continue

        target_dir = repos_dir / repo_id

        if target_dir.exists():
            print(f"[SKIP] Folder {repo_id} already exists.")
        else:
            print(f"[CLONE] Downloading {repo_id} from {repo_url}...")
            # Executes the git clone command safely
            subprocess.run(["git", "clone", repo_url, str(target_dir)])

print("=== Cloning Complete ===")
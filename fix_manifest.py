import json
from pathlib import Path

manifest_path = Path("phase2/data/processed/manifest.jsonl")
lines = manifest_path.read_text().splitlines()
fixed_lines = []

for line in lines:
    data = json.loads(line)
    data["base_commit"] = "HEAD"  # Force use of HEAD
    fixed_lines.append(json.dumps(data))

manifest_path.write_text("\n".join(fixed_lines) + "\n")
print("Manifest patched: All base_commits set to HEAD")
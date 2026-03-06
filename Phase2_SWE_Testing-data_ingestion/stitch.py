import glob
import json

input_files = "C:/Users/osart/phase2_repos/manifests/*.json"
output_file = "C:/Users/osart/OneDrive - Umich/SWTProject/Phase2_SWE_Testing/phase2/data/processed/manifest.jsonl"

json_files = glob.glob(input_files)

with open(output_file, "w", encoding="utf-8") as outfile:
    for file_path in json_files:
        with open(file_path, "r", encoding="utf-8") as infile:
            data = json.load(infile)
            
            # --- DATA NORMALIZATION ---
            # Flatten the nested repository keys so the framework can read them
            if "repository" in data:
                if "url" in data["repository"]:
                    data["repository_url"] = data["repository"]["url"]
                if "repo_id" in data["repository"]:
                    data["repository_repo_id"] = data["repository"]["repo_id"]
                    
            # Write the normalized object as a single line
            outfile.write(json.dumps(data) + "\n")

print(f"Success! Normalized and stitched {len(json_files)} files into manifest.jsonl")
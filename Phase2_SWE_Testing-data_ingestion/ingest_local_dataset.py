import json
from pathlib import Path

# Import the native pipeline functions directly from your dataset module
from src.phase2.dataset import _coerce_record, _normalize_payload, RAW_CACHE_REL, MANIFEST_REL

def main():
    print("=== Starting Local Dataset Ingestion ===")
    workdir = Path.cwd()
    raw_dir = Path("C:/Users/osart/phase2_repos/manifests")
    manifest_target = workdir / MANIFEST_REL

    # Ensure the processed directory exists
    manifest_target.parent.mkdir(parents=True, exist_ok=True)

    # Grab all JSON files except the cache index
    json_files = [f for f in raw_dir.glob("*.json") if f.name != "cache_index.json"]
    print(f"Found {len(json_files)} local dataset files.")

    records = []
    for file_path in json_files:
        try:
            # 1. Extract the commit hash from the prefix of the filename
            base_commit = file_path.name.split('_')[0]

            # 2. Read and parse the JSON
            raw_data = json.loads(file_path.read_text(encoding="utf-8"))
            record = _coerce_record(raw_data, source=str(file_path))

            # 3. Normalize the payload using the pipeline's native schema mapping
            normalized = _normalize_payload(record, dataset_url=file_path.name, source=str(file_path))

            # 4. Inject the local cache path and the critical base_commit
            normalized["source_cache_path"] = file_path.as_posix()
            normalized["base_commit"] = base_commit

            records.append(normalized)
        except Exception as e:
            print(f"Error processing {file_path.name}: {e}")

    # Sort records to maintain deterministic execution order
    records.sort(key=lambda row: row.get("dataset_url", ""))

    # Write the compiled manifest
    with manifest_target.open("w", encoding="utf-8") as f:
        for row in records:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    print(f"=== Ingestion Complete ===")
    print(f"Successfully wrote {len(records)} mapped records to {manifest_target}")

if __name__ == "__main__":
    main()
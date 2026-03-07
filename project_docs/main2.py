import io
import pandas as pd
import requests


def main():
    url = "https://anonymous.4open.science/r/classes2test/dataset/13899/13899_8.json"

    # Fetch with a browser-like user agent to avoid 403 responses
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()

    # Load JSON payload into DataFrame from memory
    df = pd.read_json(io.BytesIO(resp.content))
    print(df.head())

    # Show rows that contain at least one non-null value
    print("\nNon-null rows:")
    print(df.dropna(how="all"))


if __name__ == "__main__":
    main()

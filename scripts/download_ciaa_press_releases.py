import requests
import csv
import os

INDEX_URL = (
    "https://ngm-store.jawafdehi.org/indices/2026-03-31/index.ciaa-press-releases.json"
)
OUTPUT_DIR = "ngm/ciaa_dataset/data"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "ciaa-press-releases.csv")

FIELDS = ["press_id", "publication_date", "title", "full_text", "source_url"]


def fetch_all_manuscripts(index_url):
    manuscripts = []
    url = index_url
    seen_urls = set()
    while url:
        if url in seen_urls:
            raise RuntimeError(f"Cyclic pagination detected at: {url}")
        seen_urls.add(url)
        print(f"Fetching: {url}")
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        manuscripts.extend(data.get("manuscripts", []))
        url = data.get("next")
    return manuscripts


def save_metadata_csv(manuscripts, output_file):
    os.makedirs(os.path.dirname(output_file), exist_ok=True)

    # Deduplicate by press_id (keep first occurrence)
    seen = set()
    unique_manuscripts = []
    for m in manuscripts:
        press_id = m.get("metadata", {}).get("press_id", "")
        if press_id and press_id not in seen:
            seen.add(press_id)
            unique_manuscripts.append(m)

    print(
        f"Total manuscripts: {len(manuscripts)}, Unique: {len(unique_manuscripts)}, Duplicates removed: {len(manuscripts) - len(unique_manuscripts)}"
    )

    # Overwrite the file on each run (mode "w")
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for m in unique_manuscripts:
            meta = m.get("metadata", {})
            row = {field: meta.get(field, "") for field in FIELDS}
            writer.writerow(row)


def main():
    manuscripts = fetch_all_manuscripts(INDEX_URL)
    save_metadata_csv(manuscripts, OUTPUT_FILE)
    print(f"Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()

import json
import os
import concurrent.futures
from datetime import datetime
from typing import Dict, List, Tuple
from cloudpathlib import AnyPath
from dotenv import load_dotenv


def get_base_url() -> str:
    """Get the base URL from environment, defaulting to the production ngm store."""
    return os.getenv("NGM_STORE_BASE_URL", "https://ngm-store.newnepal.org").rstrip("/")


class Indexer:
    """Base class for all indexers."""

    def __init__(self, root_path: str, base_url: str):
        self.root_path = AnyPath(root_path)
        self.base_url = base_url
        self.source_name = "unknown"

    def relative_path(self, file_path) -> str:
        """Helper to get relative path string from a path object relative to root_path."""
        # Using string replacement as different backend paths might handle relative_to differently
        root_str = str(self.root_path).rstrip("/")
        file_str = str(file_path)
        if file_str.startswith(root_str):
            rel = file_str.replace(root_str, "", 1).lstrip("/")
            return rel
        return file_path.name

    def build_url(self, file_path) -> str:
        """Construct the full public URL for a file."""
        return f"{self.base_url}/{self.relative_path(file_path)}"

    def index(self) -> Tuple[str, List[Dict]]:
        """
        Executes the indexing logic.
        Returns:
            Tuple containing the source name and a list of index entry dictionaries.
        """
        raise NotImplementedError("Subclasses must implement index()")


class CIAAAnnualReportsIndexer(Indexer):
    def __init__(self, root_path: str, base_url: str):
        super().__init__(root_path, base_url)
        self.source_name = "ciaa_annual_reports"

    def index(self) -> Tuple[str, List[Dict]]:
        entries = []
        pdf_dir = self.root_path / "uploads" / "ciaa" / "annual-reports" / "pdf"
        metadata_dir = (
            self.root_path / "uploads" / "ciaa" / "annual-reports" / "metadata"
        )

        if not pdf_dir.exists():
            return self.source_name, entries

        for pdf_path in pdf_dir.glob("*.pdf"):
            file_id = pdf_path.stem
            metadata_path = metadata_dir / f"{file_id}.json"

            metadata = {}
            if metadata_path.exists():
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except Exception as e:
                    print(f"Warning: Failed to read metadata for {file_id}: {e}")

            entries.append(
                {
                    "url": self.build_url(pdf_path),
                    "file_name": pdf_path.name,
                    "metadata": metadata,
                }
            )

        return self.source_name, entries


class KanunPatrikaIndexer(Indexer):
    def __init__(self, root_path: str, base_url: str):
        super().__init__(root_path, base_url)
        self.source_name = "kanun_patrika"

    def index(self) -> Tuple[str, List[Dict]]:
        entries = []
        pdf_dir = self.root_path / "uploads" / "supreme-court" / "kanun-patrika"

        if not pdf_dir.exists():
            return self.source_name, entries

        for pdf_path in pdf_dir.glob("*.pdf"):
            entries.append(
                {
                    "url": self.build_url(pdf_path),
                    "file_name": pdf_path.name,
                    "metadata": {},
                }
            )

        return self.source_name, entries


def main():

    files_store_env = os.getenv("FILES_STORE")
    if not files_store_env:
        print("Error: FILES_STORE environment variable must be set.")
        exit(1)

    files_store = str(files_store_env)

    base_url = get_base_url()

    indexers = [
        CIAAAnnualReportsIndexer(files_store, base_url),
        KanunPatrikaIndexer(files_store, base_url),
    ]

    global_index = {}

    # Run indexers in parallel
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_to_indexer = {
            executor.submit(indexer.index): indexer for indexer in indexers
        }
        for future in concurrent.futures.as_completed(future_to_indexer):
            try:
                source_name, entries = future.result()
                global_index[source_name] = entries
                print(f"Indexed {len(entries)} items for {source_name}")
            except Exception as exc:
                indexer = future_to_indexer[future]
                print(f"{indexer.source_name} generated an exception: {exc}")

    # Write indexes
    root_path = AnyPath(files_store)
    today_str = datetime.now().strftime("%Y-%m-%d")

    index_json_str = json.dumps(global_index, ensure_ascii=False, indent=2)

    try:
        # Latest index at root
        index_file = root_path / "index.json"
        index_file.write_text(index_json_str, encoding="utf-8")
        print(f"Successfully wrote {index_file}")

        # Date-specific index in the 'index' folder
        index_dir = root_path / "index"
        if not index_dir.exists():
            index_dir.mkdir(parents=True)

        date_index_file = index_dir / f"index.{today_str}.json"
        date_index_file.write_text(index_json_str, encoding="utf-8")
        print(f"Successfully wrote {date_index_file}")
    except Exception as e:
        print(f"Failed to write index files: {e}")
        exit(1)


if __name__ == "__main__":
    load_dotenv()

    main()

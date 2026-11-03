import os


class IngestionConfig:
    """Standalone defaults for this package - no dependency on any host
    app's config system. A host app can still override per-call via the
    max_file_size_mb/zip_max_entries kwargs on parse_file()."""

    def __init__(self):
        self.max_file_size_mb: int = int(os.getenv("INGESTION_MAX_FILE_SIZE_MB", "15"))
        self.zip_max_entries: int = int(os.getenv("INGESTION_ZIP_MAX_ENTRIES", "200"))


cfg = IngestionConfig()

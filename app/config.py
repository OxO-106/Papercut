import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

# Library folder: PDFs, parsed results and crops. Point this at a cloud-synced
# folder to share processed papers with other machines.
LIBRARY = Path(os.environ.get("PAPER_READER_LIBRARY", ROOT.parent / "library")).resolve()
PAPERS_DIR = LIBRARY / "papers"

SCHEMA_VERSION = 1
CROP_DPI = 200

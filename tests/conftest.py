import sys
from pathlib import Path

# Tests import the app package from the project root. They never import
# app.jobs or app.main: importing those starts the processing worker.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

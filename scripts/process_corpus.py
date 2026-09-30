"""Parse every PDF in a folder into the library and print quality stats.

Usage: .venv\\Scripts\\python scripts\\process_corpus.py ..\\Papers
"""

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import library  # noqa: E402
from app.parse import parse_paper  # noqa: E402


def main(folder: str) -> None:
    pdfs = sorted(Path(folder).glob("*.pdf"), key=lambda p: (len(p.stem), p.stem))
    print(f"{'file':<14} {'id':<17} {'pages':>5} {'secs':>5} {'blocks':>6} {'sents':>5} {'lowcov':>6} {'figs':>4} {'tabs':>4} {'eqs':>4}  title")
    for pdf in pdfs:
        paper_id = library.ingest(pdf, pdf.name)
        t = time.time()
        paper = parse_paper(paper_id)
        old = library.load_paper(paper_id)
        if old:
            library.carry_over(old, paper)  # keep AI labels and edits
        library.save_paper(paper_id, paper)
        secs = time.time() - t
        kinds = Counter(b["type"] for b in paper["blocks"])
        low = sum(1 for s in paper["sentences"].values() if s["coverage"] < 0.8)
        print(
            f"{pdf.name:<14} {paper_id:<17} {paper['meta']['pages']:>5} {secs:>5.0f} {len(paper['blocks']):>6} "
            f"{len(paper['sentences']):>5} {low:>6} {kinds['figure']:>4} {kinds['table']:>4} {kinds['equation']:>4}  "
            f"{paper['meta']['title'][:60]}",
            flush=True,
        )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parents[2] / "Papers"))

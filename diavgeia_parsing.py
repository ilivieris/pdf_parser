"""Στέλνει κάθε PDF ενός φακέλου στο /extract και κατεβάζει το εξαγόμενο κείμενο.

Η υπηρεσία ανεβάζει το αρχείο στο MinIO, το διαβάζει πίσω από το bucket, κάνει το parsing
και γράφει το αποτέλεσμα ξανά στο MinIO, επιστρέφοντας presigned download link.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

# Τα Windows terminals τρέχουν συχνά σε cp1252, όπου κάθε ελληνικό print σκάει με
# UnicodeEncodeError. Το κλειδώνουμε σε UTF-8 πριν τυπωθεί οτιδήποτε.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# Ρυθμίσεις — μπορούν να παρακαμφθούν από τα ορίσματα της γραμμής εντολών.
BASE_URL = "http://localhost:8000"
PDF_DIR = Path("diavgeia_sample/test")
OUT_DIR = Path("extracted")
POST_PROCESSING = "none"  # "none" | "clean" | "markdown"

# Κατάληξη του τοπικού αρχείου ανά mode, ίδια με αυτή που δίνει η υπηρεσία στο object.
OUTPUT_SUFFIXES = {"none": ".txt", "clean": "_clean.txt", "markdown": ".md"}

# Το clean/markdown περνάει από το OpenAI και σε μεγάλα έγγραφα θέλει λεπτά, όχι δευτερόλεπτα.
REQUEST_TIMEOUT = 900


def find_pdfs(directory: Path) -> list[Path]:
    """Βρίσκει όλα τα PDF αρχεία, συμπεριλαμβανομένων των subdirectories."""
    return sorted(directory.rglob("*.pdf"))


def check_service(base_url: str) -> None:
    """Preflight: σταματάει νωρίς με καθαρό μήνυμα αν λείπει η υπηρεσία ή το MinIO.

    Χωρίς αυτό, ένα MinIO εκτός λειτουργίας εμφανίζεται ως ένα 502 ανά αρχείο.
    """
    try:
        response = requests.get(f"{base_url}/health", timeout=30)
    except requests.RequestException as exc:
        raise SystemExit(f"✗ Η υπηρεσία δεν απαντάει στο {base_url}: {exc}") from exc

    body = response.json()
    if response.status_code != 200:
        failed = [c for c in body.get("checks", []) if c["status"] == "fail"]
        detail = "; ".join(f"{c['name']}: {c['detail']}" for c in failed) or body.get("status")
        raise SystemExit(f"✗ Η υπηρεσία είναι degraded — {detail}")

    print(f"✓ {body['service']} v{body['version']} healthy ({base_url})")


def extract_pdf(pdf_path: Path, base_url: str, post_processing: str) -> dict:
    """Ανεβάζει ένα PDF στο extraction endpoint και επιστρέφει το JSON της απάντησης."""
    with pdf_path.open("rb") as handle:
        response = requests.post(
            f"{base_url}/extract",
            files={"file": (pdf_path.name, handle, "application/pdf")},
            data={"post_processing": post_processing},
            headers={"accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )

    response.raise_for_status()

    return response.json()


def download(url: str, target: Path) -> int:
    """Κατεβάζει το presigned link στο `target` και επιστρέφει το πλήθος χαρακτήρων."""
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    # Το object γράφτηκε ως UTF-8· το δηλώνουμε ρητά ώστε το requests να μη μαντέψει
    # άλλο encoding και μας γυρίσει σπασμένα ελληνικά.
    response.encoding = "utf-8"
    text = response.text

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")

    return len(text)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf-dir", type=Path, default=PDF_DIR, help="Φάκελος με τα PDF.")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR, help="Πού αποθηκεύονται τα κείμενα.")
    parser.add_argument(
        "--post-processing",
        choices=sorted(OUTPUT_SUFFIXES),
        default=POST_PROCESSING,
        help="Τρόπος επεξεργασίας του κειμένου μετά την εξαγωγή.",
    )
    parser.add_argument("--base-url", default=BASE_URL, help="Base URL της υπηρεσίας.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # 1. Βεβαιώσου ότι η υπηρεσία και το MinIO είναι όρθια.
    check_service(args.base_url)

    # 2. Βρες όλα τα PDFs.
    pdfs = find_pdfs(args.pdf_dir)
    if not pdfs:
        print(f"Δεν βρέθηκε κανένα PDF στο {args.pdf_dir}.")
        return 1

    print(f"Βρέθηκαν {len(pdfs)} PDF αρχεία (post_processing={args.post_processing}).\n")

    # 3. Ανέβασε, εξήγαγε και κατέβασε το καθένα.
    failures = 0
    for index, pdf_path in enumerate(pdfs, start=1):
        print(f"[{index}/{len(pdfs)}] {pdf_path}")

        try:
            result = extract_pdf(pdf_path, args.base_url, args.post_processing)

            target = args.out_dir / f"{pdf_path.stem}{OUTPUT_SUFFIXES[args.post_processing]}"
            char_count = download(result["download_url"], target)

            print(f"  ✓ {target} ({char_count:,} χαρακτήρες)")
            if result.get("note"):
                print(f"  ! Note: {result['note']}")

        except requests.HTTPError as exc:
            failures += 1
            detail = ""
            if exc.response is not None:
                try:
                    detail = f" — {exc.response.json().get('detail', '')}"
                except ValueError:
                    detail = f" — {exc.response.text[:200]}"
            print(f"  ✗ HTTP {exc.response.status_code if exc.response is not None else '?'}{detail}")

        except requests.RequestException as exc:
            failures += 1
            print(f"  ✗ Σφάλμα δικτύου: {exc}")

        except Exception as exc:
            failures += 1
            print(f"  ✗ {exc.__class__.__name__}: {exc}")

    print(f"\nΟλοκληρώθηκαν {len(pdfs) - failures}/{len(pdfs)} — έξοδος στο {args.out_dir}/")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

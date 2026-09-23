from pathlib import Path
import requests

# Ρυθμίσεις
PDF_DIR = Path("diavgeia_sample/test")
ENDPOINT = "http://localhost:8000/extract"
POST_PROCESSING = "none"  # "none" | "clean" | "markdown"


def find_pdfs(directory: Path) -> list[Path]:
    """Βρίσκει όλα τα PDF αρχεία, συμπεριλαμβανομένων των subdirectories."""
    return list(directory.rglob("*.pdf"))


def extract_pdf(pdf_path: Path, post_processing: str = POST_PROCESSING):
    """Ανεβάζει ένα PDF στο extraction endpoint.

    Το API το αποθηκεύει στο MinIO, το διαβάζει πίσω από το bucket, κάνει το parsing
    και γράφει το αποτέλεσμα ξανά στο MinIO, επιστρέφοντας presigned download link.
    """
    with pdf_path.open("rb") as handle:
        response = requests.post(
            ENDPOINT,
            files={"file": (pdf_path.name, handle, "application/pdf")},
            data={"post_processing": post_processing},
            headers={"accept": "application/json"},
            timeout=600,
        )

    response.raise_for_status()

    return response.json()


def main():
    # 1. Βρες όλα τα PDFs
    pdfs = find_pdfs(PDF_DIR)

    print(f"Βρέθηκαν {len(pdfs)} PDF αρχεία.\n")

    # 2. Επεξεργασία κάθε PDF
    for index, pdf_path in enumerate(pdfs, start=1):
        print(f"[{index}/{len(pdfs)}] Processing: {pdf_path}")

        try:
            result = extract_pdf(pdf_path)

            print("  ✓ Success")
            print(f"  Download URL: {result['download_url']}")
            if result.get("note"):
                print(f"  Note: {result['note']}")

        except requests.RequestException as e:
            print(f"  ✗ HTTP Error: {e}")

        except Exception as e:
            print(f"  ✗ Error: {e}")


if __name__ == "__main__":
    main()

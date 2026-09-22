from pathlib import Path
import requests

# Ρυθμίσεις
PDF_DIR = Path("diavgeia_sample/test")
ENDPOINT = "http://localhost:8000/extract"


def find_pdfs(directory: Path) -> list[Path]:
    """Βρίσκει όλα τα PDF αρχεία, συμπεριλαμβανομένων των subdirectories."""
    return list(directory.rglob("*.pdf"))


def extract_pdf(pdf_path: Path):
    """Στέλνει ένα PDF στο extraction endpoint."""
    # docker-compose.yml κάνει mount ολόκληρο το repo root στο
    # /app/extracted/workspace, άρα τα paths πρέπει να έχουν αυτό το prefix
    # για να επιλυθούν σωστά μέσα στο container (DOCUMENT_OUTPUT_ROOT=/app/extracted).
    path_for_api = f"workspace/{pdf_path.as_posix()}"

    payload = {
        "path": path_for_api,
        "post_processing": "clean"
    }

    response = requests.post(
        ENDPOINT,
        json=payload,
        headers={
            "accept": "*/*",
            "Content-Type": "application/json",
        },
        timeout=300,
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
            print(f"  Response: {result}")

        except requests.RequestException as e:
            print(f"  ✗ HTTP Error: {e}")

        except Exception as e:
            print(f"  ✗ Error: {e}")


if __name__ == "__main__":
    main()
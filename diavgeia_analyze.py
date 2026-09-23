"""🧠 Ανεβάζει κάθε .txt ενός φακέλου στο /analyze και τυπώνει τα αποτελέσματα.

Το αρχείο στέλνεται ως upload και **δεν αποθηκεύεται πουθενά** — ούτε στο MinIO ούτε
ως αρχείο στον server (μόνο ένα προσωρινό αρχείο όσο διαρκεί το parsing, που σβήνεται).
Άρα δεν χρειάζεται ο φάκελός σου να είναι ορατός από την υπηρεσία: δουλεύει και με
remote API, χωρίς mounts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

# 🪟 Τα Windows terminals τρέχουν συχνά σε cp1252, όπου κάθε ελληνικό print (και κάθε emoji)
# σκάει με UnicodeEncodeError. Το κλειδώνουμε σε UTF-8 πριν τυπωθεί οτιδήποτε.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# ⚙️ Ρυθμίσεις — μπορούν να παρακαμφθούν από τα ορίσματα της γραμμής εντολών.
BASE_URL = "http://localhost:8000"
TXT_DIR = Path("extracted")

# ⏳ Το /analyze κάνει LLM κλήσεις και semantic search· σε μεγάλα έγγραφα θέλει λεπτά.
REQUEST_TIMEOUT = 900

# ✅/❓ Σύμβολο για το `confirmed` flag: τι πέρασε ντετερμινιστικό έλεγχο και τι είναι
# απλώς πρόταση του μοντέλου.
CONFIRMED_MARK = {True: "✅", False: "❓"}

# 🎯 Σύμβολο ανά επίπεδο confidence.
CONFIDENCE_MARK = {"high": "🟢", "medium": "🟡", "low": "🔴"}

# ✂️ Μέγιστο μήκος note στην περίληψη.
NOTE_MAX_CHARS = 200


def find_txt_files(directory: Path) -> list[Path]:
    """🔎 Βρίσκει όλα τα .txt αρχεία, συμπεριλαμβανομένων των subdirectories."""
    return sorted(directory.rglob("*.txt"))


def check_service(base_url: str) -> None:
    """🩺 Preflight: σταματάει νωρίς με καθαρό μήνυμα αν λείπει η υπηρεσία ή μια εξάρτηση."""
    try:
        response = requests.get(f"{base_url}/health", timeout=30)
    except requests.RequestException as exc:
        raise SystemExit(f"💥 Η υπηρεσία δεν απαντάει στο {base_url}: {exc}") from exc

    body = response.json()
    if response.status_code != 200:
        failed = [c for c in body.get("checks", []) if c["status"] == "fail"]
        detail = "; ".join(f"{c['name']}: {c['detail']}" for c in failed) or body.get("status")
        raise SystemExit(f"🚨 Η υπηρεσία είναι degraded — {detail}")

    print(f"✅ {body['service']} v{body['version']} healthy 💚 ({base_url})")


def analyze(txt_path: Path, base_url: str) -> dict:
    """📨 Ανεβάζει ένα αρχείο στο /analyze και επιστρέφει το JSON της απάντησης."""
    with txt_path.open("rb") as handle:
        response = requests.post(
            f"{base_url}/analyze",
            files={"file": (txt_path.name, handle, "text/plain; charset=utf-8")},
            headers={"accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )

    response.raise_for_status()

    return response.json()


def print_decision_type(result: dict) -> None:
    """🏷️ Ο τύπος πράξης κατά το επίσημο catalogue της Διαύγειας."""
    decision = result.get("decision_type")
    if not decision:
        print("   🏷️  Τύπος πράξης: — (δεν ταξινομήθηκε)")
        return

    mark = CONFIRMED_MARK[decision["confirmed"]]
    confidence = CONFIDENCE_MARK.get(decision["confidence"], "⚪")
    print(f"   🏷️  Τύπος πράξης: {decision['label']} [{decision['uid']}] {mark} {confidence}")

    if decision.get("alternative_uids"):
        print(f"       🔀 Εναλλακτικά: {', '.join(decision['alternative_uids'])}")


def print_cpv(result: dict, limit: int) -> None:
    """💶 CPV κωδικοί — τι προμηθεύεται/αναθέτει η πράξη."""
    matches = result.get("cpv", [])
    print(f"   💶 CPV: {len(matches)}")

    for match in matches[:limit]:
        mark = CONFIRMED_MARK[match["confirmed"]]
        source = "🔎 regex" if match["source"] == "regex" else "🤖 llm"
        print(f"       • {match['code']} {mark} {source}")

    if len(matches) > limit:
        print(f"       … και {len(matches) - limit} ακόμη ➕")


def print_budget_codes(result: dict, limit: int) -> None:
    """🧾 Κωδικοί προϋπολογισμού (ΚΑΕ/ΑΛΕ)."""
    matches = result.get("budget_codes", [])
    print(f"   🧾 ΚΑΕ/ΑΛΕ: {len(matches)}")

    for match in matches[:limit]:
        mark = CONFIRMED_MARK[match["confirmed"]]
        source = "🔎 regex" if match["source"] == "regex" else "🤖 llm"
        description = f" — {match['description']}" if match.get("description") else ""
        print(f"       • {match['kind']} {match['code']} {mark} {source}{description}")

    if len(matches) > limit:
        print(f"       … και {len(matches) - limit} ακόμη ➕")


def print_skills(result: dict, limit: int) -> None:
    """🧠 Δεξιότητες από την ταξινομία ESCO."""
    matches = result.get("skills", [])
    print(f"   🧠 Δεξιότητες: {len(matches)}")

    for match in matches[:limit]:
        confidence = CONFIDENCE_MARK.get(match["confidence"], "⚪")
        taxonomy = "🇪🇺 ESCO" if match["taxonomy"] == "ESCO" else "💻 DigComp"
        print(f"       • {taxonomy} · {match['label']} {confidence}")
        if match.get("source_uri"):
            print(f"         🔗 {match['source_uri']}")

    if len(matches) > limit:
        print(f"       … και {len(matches) - limit} ακόμη ➕")


def print_notes(result: dict) -> None:
    """⚠️ Τα notes εμφανίζονται μόνο όταν κάτι παραλείφθηκε ή απέτυχε.

    Κόβονται στο NOTE_MAX_CHARS: ένα αποτυχημένο OpenAI call επιστρέφει ολόκληρο το σώμα
    του σφάλματος, που αλλιώς πνίγει την περίληψη. Ολόκληρο το κείμενο υπάρχει στο --json.
    """
    for key, icon in (("extraction_note", "⚠️"), ("skills_note", "⚠️")):
        note = result.get(key)
        if not note:
            continue
        if len(note) > NOTE_MAX_CHARS:
            note = f"{note[:NOTE_MAX_CHARS].rstrip()}… ✂️ (δες --json)"
        print(f"   {icon}  {key}: {note}")


def print_result(result: dict, limit: int) -> None:
    """🖨️ Τυπώνει μία περίληψη ανά αρχείο."""
    print(f"   🆔 artifact_id: {result['artifact_id']}")
    print_decision_type(result)
    print_cpv(result, limit)
    print_budget_codes(result, limit)
    print_skills(result, limit)
    print_notes(result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=TXT_DIR,
        help="📂 Φάκελος με τα .txt (ή ένα μεμονωμένο .txt αρχείο).",
    )
    parser.add_argument("--base-url", default=BASE_URL, help="🌐 Base URL της υπηρεσίας.")
    parser.add_argument("--limit", type=int, default=5, help="🔢 Πόσα items ανά κατηγορία τυπώνονται.")
    parser.add_argument("--json", action="store_true", help="🧾 Τυπώνει ολόκληρο το raw JSON.")
    parser.add_argument("--save", type=Path, help="💾 Φάκελος για αποθήκευση ενός .json ανά αρχείο.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    print("🧠 diavgeia_analyze — semantic analysis των εξαγόμενων κειμένων\n")

    # 1️⃣ Βεβαιώσου ότι η υπηρεσία είναι όρθια.
    check_service(args.base_url)

    # 2️⃣ Βρες τα .txt (δέχεται και μεμονωμένο αρχείο, βολικό για γρήγορο έλεγχο).
    if args.path.is_file():
        txt_files = [args.path]
    else:
        txt_files = find_txt_files(args.path)

    if not txt_files:
        print(f"🤷 Δεν βρέθηκε κανένα .txt στο {args.path}.")
        return 1

    print(f"📂 Βρέθηκαν {len(txt_files)} .txt αρχεία στο {args.path} 🗂️\n")

    # 3️⃣ Ανάλυσε το καθένα.
    failures = 0
    for index, txt_path in enumerate(txt_files, start=1):
        size_kb = txt_path.stat().st_size / 1024
        print(f"[{index}/{len(txt_files)}] 📄 {txt_path} ⬆️ ({size_kb:,.1f} KB)")

        try:
            result = analyze(txt_path, args.base_url)

            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print_result(result, args.limit)

            if args.save:
                args.save.mkdir(parents=True, exist_ok=True)
                target = args.save / f"{txt_path.stem}.json"
                target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"   💾 {target}")

            print("   ✅ OK\n")

        except requests.HTTPError as exc:
            failures += 1
            status = exc.response.status_code if exc.response is not None else "?"
            detail = ""
            if exc.response is not None:
                try:
                    detail = f" — {exc.response.json().get('detail', '')}"
                except ValueError:
                    detail = f" — {exc.response.text[:200]}"
            print(f"   ❌ HTTP {status}{detail}")
            if status == 413:
                print("   💡 Το αρχείο ξεπερνά το MAX_UPLOAD_SIZE_BYTES του server.")
            print()

        except requests.RequestException as exc:
            failures += 1
            print(f"   🌐 Σφάλμα δικτύου: {exc}\n")

        except Exception as exc:
            failures += 1
            print(f"   💥 {exc.__class__.__name__}: {exc}\n")

    # 4️⃣ Σύνοψη.
    ok = len(txt_files) - failures
    icon = "🎉" if failures == 0 else "⚠️"
    print(f"{icon} Ολοκληρώθηκαν {ok}/{len(txt_files)} αναλύσεις.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

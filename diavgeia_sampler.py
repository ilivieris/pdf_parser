#!/usr/bin/env python3
"""
Diavgeia stratified sampler.

Για κάθε τύπο πράξης (decisionTypeUid) τραβάει δείγμα εγγράφων, κατανεμημένο σε
διαφορετικά έτη, και αποθηκεύει:
  <out>/pdf/<ADA>.pdf          το έγγραφο
  <out>/meta/<ADA>.json        πλήρης εγγραφή του API (περιλαμβάνει extraFieldValues = ground truth φόρμας)
  <out>/manifest.jsonl         μία γραμμή ανά έγγραφο (ada, type, year, org, subject, sha256, ...)

Χρήση:
  pip install requests            # + pypdf αν θες --probe
  python diavgeia_sampler.py --dry-run --types Β.2.1 --per-type 5
  python diavgeia_sampler.py --per-type 15 --out diavgeia_sample
  python diavgeia_sampler.py --types Β.2.1 Α.2 Γ.2 --per-type 20 --probe

Επανεκκίνηση: ό,τι υπάρχει ήδη στο manifest παραλείπεται.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import random
import sys
import time
from pathlib import Path

import requests

BASE = "https://diavgeia.gov.gr/luminapi/opendata"
UA = "diavgeia-sampler/1.0 (research use)"
MAX_OFFSET = 9000  # defensive cap: page*size, για να αποφύγουμε deep paging


class Client:
    def __init__(self, sleep=0.4, timeout=60, retries=4):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept": "application/json"})
        self.sleep, self.timeout, self.retries = sleep, timeout, retries

    def get(self, url, params=None):
        for attempt in range(self.retries):
            try:
                r = self.s.get(url, params=params, timeout=self.timeout)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
                r.raise_for_status()
                time.sleep(self.sleep)
                return r
            except requests.RequestException as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                fatal = status is not None and 400 <= status < 500 and status != 429
                if fatal or attempt == self.retries - 1:
                    raise
                time.sleep(1.5 * 2 ** attempt)


def list_types(c):
    data = c.get(f"{BASE}/types.json").json()
    items = data.get("decisionTypes", data) if isinstance(data, dict) else data
    return [t["uid"] for t in items if t.get("allowedInDecisions", True)]


def search(c, dtype, date_from, date_to, size, page, org=None):
    params = {
        "type": dtype,
        "from_issue_date": date_from,
        "to_issue_date": date_to,
        "size": size,
        "page": page,
    }
    if org:
        params["org"] = org
    d = c.get(f"{BASE}/search.json", params=params).json()
    total = d.get("info", {}).get("total")
    decisions = d.get("decisions")
    if total is None or decisions is None:
        sys.exit(f"Απρόσμενη μορφή απάντησης search. Keys: {list(d)}")
    return total, decisions


def sample_type(c, dtype, years, per_type, org, rng):
    ys = years[:]
    rng.shuffle(ys)
    ys = ys[: min(len(ys), per_type)]
    quota = math.ceil(per_type / len(ys))
    out = []
    for y in ys:
        f, t = f"{y}-01-01", f"{y}-12-31"
        total, decs = search(c, dtype, f, t, quota, 0, org)
        if total == 0:
            continue
        if total > quota:
            max_page = min((total - 1) // quota, MAX_OFFSET // quota)
            pg = rng.randint(0, max_page)
            if pg:
                _, decs = search(c, dtype, f, t, quota, pg, org)
        out.extend(decs[:quota])
    rng.shuffle(out)
    return out[:per_type]


def issue_year(d):
    v = d.get("issueDate")
    try:
        if isinstance(v, (int, float)):
            return dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).year
        if isinstance(v, str):
            return int(v[6:10]) if v[2:3] == "/" else int(v[:4])
    except (ValueError, OSError):
        pass
    return None


def probe_text(path, max_pages=20):
    """Ευρετικό: πόσο native text υπάρχει; Χαμηλό chars/page => πιθανό scan (χρειάζεται OCR)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(str(path))
        n = len(reader.pages)
        chars = sum(len((reader.pages[i].extract_text() or "").strip()) for i in range(min(n, max_pages)))
        per_page = chars / max(1, min(n, max_pages))
        return {"pages": n, "chars_per_page": round(per_page, 1), "likely_scanned": per_page < 50}
    except Exception as e:  # κατεστραμμένα/κρυπτογραφημένα PDF
        return {"error": str(e)}


def download(c, ada, url, pdf_dir):
    r = c.get(url)
    body = r.content
    ext = ".pdf" if body[:5] == b"%PDF-" else ".bin"
    path = pdf_dir / f"{ada}{ext}"
    path.write_bytes(body)
    return path, len(body), hashlib.sha256(body).hexdigest(), r.headers.get("Content-Type")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="diavgeia_sample")
    ap.add_argument("--per-type", type=int, default=15)
    ap.add_argument("--year-from", type=int, default=2012)
    ap.add_argument("--year-to", type=int, default=dt.date.today().year)
    ap.add_argument("--types", nargs="*", help="π.χ. Β.2.1 Α.2 Γ.2 (default: όλοι οι τύποι από το API)")
    ap.add_argument("--org", help="organization UID (προαιρετικό φίλτρο)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--sleep", type=float, default=0.4, help="δευτερόλεπτα ανάμεσα στα requests")
    ap.add_argument("--limit-types", type=int, help="μόνο οι πρώτοι N τύποι (για δοκιμή)")
    ap.add_argument("--probe", action="store_true", help="έλεγχος native text vs scan (θέλει pypdf)")
    ap.add_argument("--dry-run", action="store_true", help="μόνο λίστα δείγματος, χωρίς λήψη")
    a = ap.parse_args()

    rng = random.Random(a.seed)
    c = Client(sleep=a.sleep)
    out = Path(a.out)
    pdf_dir, meta_dir = out / "pdf", out / "meta"
    manifest = out / "manifest.jsonl"
    if not a.dry_run:
        pdf_dir.mkdir(parents=True, exist_ok=True)
        meta_dir.mkdir(parents=True, exist_ok=True)

    done = set()
    if manifest.exists():
        with manifest.open(encoding="utf-8") as f:
            done = {json.loads(l)["ada"] for l in f if l.strip()}

    types = a.types or list_types(c)
    if a.limit_types:
        types = types[: a.limit_types]
    years = list(range(a.year_from, a.year_to + 1))
    print(f"{len(types)} τύποι, έτη {years[0]}–{years[-1]}, {a.per_type} ανά τύπο, seed={a.seed}")

    n_ok = 0
    for dtype in types:
        try:
            sample = sample_type(c, dtype, years, a.per_type, a.org, rng)
        except requests.RequestException as e:
            print(f"[{dtype}] search απέτυχε: {e}")
            continue
        print(f"[{dtype}] {len(sample)} έγγραφα")
        if a.dry_run:
            for d in sample:
                print(f"    {d.get('ada')}  {issue_year(d)}  {(d.get('subject') or '')[:70]}")
            continue
        for d in sample:
            ada = d.get("ada")
            if not ada or ada in done:
                continue
            if d.get("status") not in (None, "PUBLISHED"):
                continue
            url = d.get("documentUrl") or f"https://diavgeia.gov.gr/doc/{ada}"
            try:
                path, size, sha, ctype = download(c, ada, url, pdf_dir)
            except requests.RequestException as e:
                print(f"    {ada}: λήψη απέτυχε ({e})")
                continue
            (meta_dir / f"{ada}.json").write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            row = {
                "ada": ada,
                "type": dtype,
                "year": issue_year(d),
                "org": d.get("organizationId"),
                "subject": d.get("subject"),
                "protocol": d.get("protocolNumber"),
                "file": str(path.relative_to(out)),
                "bytes": size,
                "sha256": sha,
                "content_type": ctype,
            }
            if a.probe and path.suffix == ".pdf":
                row["probe"] = probe_text(path)
            with manifest.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            done.add(ada)
            n_ok += 1

    if not a.dry_run:
        print(f"Ολοκληρώθηκε: {n_ok} νέα έγγραφα -> {out}")


if __name__ == "__main__":
    main()
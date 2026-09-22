from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

# Bundled snapshot of the official decision-type catalogue (see decision_types.json for
# provenance/fetch date). Only entries with allowedInDecisions=true are real, assignable
# types — the rest are grouping categories that can appear as a `parent`, never as a
# document's own type.
_REFERENCE_PATH = Path(__file__).with_name("data") / "decision_types.json"


@lru_cache(maxsize=1)
def load_decision_types() -> dict[str, str]:
    data = json.loads(_REFERENCE_PATH.read_text(encoding="utf-8"))
    return {
        item["uid"]: item["label"]
        for item in data["decisionTypes"]
        if item.get("allowedInDecisions")
    }

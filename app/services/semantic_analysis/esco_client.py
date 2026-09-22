from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterator

# Public ESCO REST API — no API key/registration required.
#
# IMPORTANT: the obvious approach (`/search?type=skill&isInScheme=<skills-scheme>&offset=N`) looks
# like it supports full pagination — it echoes back `total: 13485` and happily returns results for
# small offsets — but silently returns an EMPTY page for any offset past roughly ~100-140, with no
# error. Confirmed by hand on 2026-09-22 (offset=101 -> 100 results, offset=150 -> 0 results, same
# with or without `full=true`). Building an index from that would silently cover ~1% of ESCO while
# claiming full coverage — worse than not having it. Do not reintroduce offset-based `/search`
# pagination here without re-verifying this.
#
# What DOES work reliably: `/resource/concept?uri=<uri>&language=el` returns a node's FULL,
# untruncated child list in `_links.narrowerConcept` (sub-categories) and `_links.narrowerSkill`
# (leaf skills), in one response — e.g. isced-f/0613 alone returns all 148 of its skills at once,
# no pagination involved. So this client walks the ESCO concept hierarchy breadth-first from its
# top concepts (knowledge/skills/language-skills/attitudes-and-values) instead, collecting every
# `narrowerSkill` leaf it finds.
#
# One wrinkle: some `narrowerConcept` children are themselves skill nodes (not further category
# nodes) — `/resource/concept?uri=...` 500s for those, and `/resource/skill?uri=...` must be used
# instead. _fetch_node() tries concept first, falls back to skill on a 500/404.
#
# NOTE: DigComp is NOT exposed by this REST API at all (confirmed via the `facet=isInScheme`
# discovery call — the only schemes it knows about are skills/member-skills/skill-ict-groups/
# skill-transversal-groups/skill-language-groups). The DigComp mapping only exists in ESCO's bulk
# CSV/RDF download bundle (digCompSkillsCollection.csv), which requires filling in an email on
# https://esco.ec.europa.eu/en/use-esco/download and following an emailed link — a manual, human
# step this client can't do on its own.
API_BASE = "https://ec.europa.eu/esco/api"

TOP_CONCEPTS = (
    "http://data.europa.eu/esco/skill/L",  # γλωσσικές δεξιότητες και γνώσεις
    "http://data.europa.eu/esco/skill/K",  # γνώσεις
    "http://data.europa.eu/esco/skill/S",  # δεξιότητες
    "http://data.europa.eu/esco/skill/A",  # επαγγελματικές αρχές και αξίες
)

REQUEST_TIMEOUT = 30
# Only for genuine connection-level failures. A clean HTTP error response (e.g. the concept/skill
# type-mismatch 500 above) is deterministic — retrying it just burns time for no benefit.
RETRY_DELAYS = (2, 5, 10)


@dataclass(frozen=True)
class EscoSkill:
    uri: str
    label: str
    skill_type: str | None


def _get_json(url: str) -> dict:
    last_error: Exception | None = None
    for delay in (0, *RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
    raise RuntimeError(f"Failed to fetch {url} after {len(RETRY_DELAYS) + 1} attempts") from last_error


def _fetch_node(uri: str, language: str) -> dict | None:
    """A `narrowerConcept` link can point at either a further category or a terminal skill node —
    there's no way to tell from the link alone. Try concept first (the common case), fall back to
    skill on the type-mismatch error. Returns None if neither works (node genuinely unreachable)."""
    try:
        return _get_json(f"{API_BASE}/resource/concept?uri={uri}&language={language}")
    except urllib.error.HTTPError:
        pass
    except RuntimeError:
        return None

    try:
        return _get_json(f"{API_BASE}/resource/skill?uri={uri}&language={language}")
    except (urllib.error.HTTPError, RuntimeError):
        return None


def iter_esco_skills(*, language: str = "el") -> Iterator[EscoSkill]:
    """Breadth-first walk of the ESCO concept hierarchy from its top concepts, yielding every
    distinct skill found — either as a `narrowerSkill` leaf anywhere in the tree, or as a
    `narrowerConcept` child that turned out to be a skill node itself (see _fetch_node)."""
    seen_nodes: set[str] = set()
    seen_skills: set[str] = set()
    # queue entries: (uri, title-if-known-from-the-parent-link)
    queue: list[tuple[str, str | None]] = [(uri, None) for uri in TOP_CONCEPTS]

    def _emit(uri: str, label: str | None, skill_type: str | None) -> Iterator[EscoSkill]:
        if uri and label and uri not in seen_skills:
            seen_skills.add(uri)
            yield EscoSkill(uri=uri, label=label, skill_type=skill_type)

    while queue:
        uri, title_hint = queue.pop()
        if uri in seen_nodes:
            continue
        seen_nodes.add(uri)

        payload = _fetch_node(uri, language)
        if payload is None:
            # Couldn't resolve as either a concept or a skill. If we at least have a title from
            # the parent link, still record it as a bare leaf rather than losing it entirely.
            yield from _emit(uri, title_hint, None)
            continue

        links = payload.get("_links", {})
        is_skill_node = "hasSkillType" in links or "broaderSkill" in links

        for child in links.get("narrowerConcept", []):
            child_uri = child.get("uri")
            if child_uri and child_uri not in seen_nodes:
                queue.append((child_uri, child.get("title")))

        for skill_link in links.get("narrowerSkill", []):
            yield from _emit(skill_link.get("uri"), skill_link.get("title"), skill_link.get("skillType"))

        if is_skill_node:
            skill_type = None
            skill_type_links = links.get("hasSkillType") or []
            if skill_type_links:
                skill_type = skill_type_links[0].get("uri")
            yield from _emit(uri, payload.get("title") or title_hint, skill_type)

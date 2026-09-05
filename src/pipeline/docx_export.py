"""Headless export: the hard guard, then the fresh / legacy / tracked writers.

The GUI collects decisions (output path, prompts) and calls in here; nothing
in this module touches Qt, so every export path is testable end to end.
"""

import logging
from typing import Optional

from ..models.embedded import DocumentTier
from ..models.existing_refs import ExistingCitationMap
from ..models.project import ProjectSettings

logger = logging.getLogger(__name__)


class ExportBlocked(Exception):
    """The export was refused before any write; ``reasons`` says why."""

    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)


def fresh_append_needs_confirmation(existing: Optional[ExistingCitationMap]) -> bool:
    """True when the document has a References heading but no entry AI REFs
    could read (author-date lists, unusual numbering): a fresh export would
    append a second bibliography, which the user may explicitly accept."""
    return (existing is not None and existing.references_heading_para_idx >= 0
            and not existing.bib_entries)


def check_export_guard(existing: Optional[ExistingCitationMap], mode: str,
                       settings: ProjectSettings, allow_fresh_append: bool = False) -> list[str]:
    """Reasons an export in *mode* (``fresh`` / ``legacy`` / ``tracked``) must not run.

    An empty list means go ahead. The guard exists because the legacy path
    deletes and rebuilds the bibliography: a partial parse, a misdetected
    document or another manager's fields would otherwise lose citations.
    *allow_fresh_append* is the user's explicit confirmation for the case
    :func:`fresh_append_needs_confirmation` describes; it never overrides a
    document whose entries were actually parsed.
    """
    reasons: list[str] = []
    if existing is None:
        return reasons
    tracking = existing.tracking
    if tracking is not None and tracking.tier == DocumentTier.FAILED:
        reasons.append("Analysis of the document's existing citations failed: "
                       + "; ".join(tracking.problems))
        return reasons
    if tracking is not None and tracking.foreign_field_count:
        reasons.append(
            f"{tracking.foreign_field_count} citation field(s) from another reference "
            "manager are present. AI REFs does not edit those documents.")
    if mode == "fresh":
        if existing.bib_entries:
            reasons.append(
                "This document already has a References section with numbered entries; "
                "a fresh export would append a second one. Reload it so AI REFs can "
                "detect the existing citations.")
        elif existing.references_heading_para_idx >= 0 and not allow_fresh_append:
            reasons.append(
                "This document has a References heading but no numbered entries AI REFs "
                "can read. Exporting would append a second bibliography; confirm to "
                "treat the document as uncited.")
    if mode == "legacy" and tracking is not None and tracking.problems:
        reasons.append("The existing citations could not be read reliably: "
                       + "; ".join(tracking.problems))
    if mode == "legacy" and existing.bib_entries:
        matched = len({c.number for cites in existing.in_text_citations.values()
                       for c in cites} & set(existing.bib_entries))
        total = len(existing.bib_entries)
        if matched == 0 or matched / total < settings.min_match_ratio:
            reasons.append(
                f"Only {matched} of {total} reference entries could be matched to in-text "
                f"citations ({matched / total:.0%}). Refusing to rebuild the bibliography; "
                "check the document's citation format.")
    if existing.pending_tracked_changes and not settings.allow_export_with_tracked_changes:
        reasons.append(
            "The document has pending tracked changes around citations. Accept or reject "
            "them in Word first (or enable 'Export anyway' in settings).")
    return reasons

"""Statistics collected during document export, for the summary dialog."""

from dataclasses import dataclass, field


@dataclass
class ExportStats:
    """What happened during an export — surfaced to the user afterwards."""
    total_markers: int = 0
    resolved_markers: int = 0
    unresolved_markers: int = 0  # exported as [?]
    unresolved_sentence_ids: list[str] = field(default_factory=list)
    left_unverified: int = 0     # author-suggested citations kept as written (not confirmed)
    skipped_markers: int = 0     # markers the user chose to leave unchanged
    unmatched_markers: int = 0   # DOCX markers the pipeline never saw (e.g. in a heading)
    new_refs_added: int = 0
    existing_refs_renumbered: int = 0
    identifiers_filled: int = 0  # embedded records completed (PMC id, DOI, PMID) from supplied records
    duplicates_merged: int = 0  # new citations matched to existing bib entries
    citations_converted: int = 0  # numeric sites converted to author-date
    bibliography_size: int = 0
    output_path: str = ""
    # Tracked-document counters (embedded citation fields)
    fields_written: int = 0            # citation fields written (resolved + unresolved)
    unresolved_fields: int = 0         # fields left as [?] pending a citation
    legacy_adopted: int = 0            # plain-text citation sites wrapped into fields (-1: not tracked)
    entries_seeded_uncited: int = 0    # parsed entries nothing cited, kept anyway
    uncited_dropped: int = 0           # records whose every citation was deleted
    hand_edits_overwritten: int = 0    # numeric results the user had edited, regenerated
    hand_edits_preserved: int = 0      # author-date results the user had edited, kept
    damaged_fields: int = 0            # fields whose payload could not be read
    bibliography_regenerated: bool = False
    tables_citations: int = 0          # citation fields in tables / text boxes (not renumbered)
    unadoptable_sites: int = 0         # legacy citation sites that could not be matched to entries

    def summary_lines(self) -> list[str]:
        lines = [
            f"Markers processed: {self.total_markers}",
            f"  • Resolved with citations: {self.resolved_markers}",
        ]
        if self.unresolved_markers:
            ids = ", ".join(self.unresolved_sentence_ids[:10])
            more = "…" if len(self.unresolved_sentence_ids) > 10 else ""
            lines.append(
                f"  • Exported as [?] (unresolved): {self.unresolved_markers}"
                + (f"  ({ids}{more})" if ids else "")
            )
        if self.left_unverified:
            lines.append(
                f"  • Author-suggested citations left unverified (text unchanged): "
                f"{self.left_unverified}")
        if self.skipped_markers:
            lines.append(f"  • Left unchanged as you requested: {self.skipped_markers}")
        if self.unmatched_markers:
            lines.append(f"  • Not processed by the pipeline (text unchanged): {self.unmatched_markers}")
        lines.append(f"New references added: {self.new_refs_added}")
        if self.existing_refs_renumbered:
            lines.append(f"Existing references renumbered: {self.existing_refs_renumbered}")
        if self.identifiers_filled:
            lines.append(f"Existing records completed with identifiers (PMC id, DOI, PMID): "
                         f"{self.identifiers_filled}")
        if self.citations_converted:
            lines.append(
                f"Citation sites converted to author-date: {self.citations_converted}")
        if self.duplicates_merged:
            lines.append(
                f"Duplicates merged with existing references: {self.duplicates_merged}"
            )
        lines.append(f"Bibliography entries: {self.bibliography_size}")
        if self.fields_written:
            lines.append(f"Citations tracked as embedded fields: {self.fields_written}")
        elif self.legacy_adopted == -1:
            lines.append("Document not tracked (plain-text citations)")
        if self.unresolved_fields:
            lines.append(f"  • Unresolved fields ([?]) to fill later: {self.unresolved_fields}")
        if self.legacy_adopted > 0:
            lines.append(f"Existing citations adopted into tracked fields: {self.legacy_adopted}")
        if self.entries_seeded_uncited:
            lines.append(f"Entries kept although nothing cites them: {self.entries_seeded_uncited}")
        if self.uncited_dropped:
            lines.append(f"References dropped (no citation left): {self.uncited_dropped}")
        if self.hand_edits_overwritten:
            lines.append(f"Hand-edited citation numbers regenerated: {self.hand_edits_overwritten}")
        if self.hand_edits_preserved:
            lines.append(f"Hand-edited author-date citations preserved: {self.hand_edits_preserved}")
        if self.unadoptable_sites:
            lines.append(f"Not tracked: {self.unadoptable_sites} citation site(s) could not be "
                         "matched to a reference entry")
        if self.damaged_fields:
            lines.append(f"Damaged citation fields: {self.damaged_fields}")
        if self.bibliography_regenerated:
            lines.append("Bibliography field was missing and has been regenerated")
        if self.tables_citations:
            lines.append(f"Citations in tables/text boxes (left as they were): {self.tables_citations}")
        return lines

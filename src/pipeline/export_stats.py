"""Statistics collected during document export, for the summary dialog."""

from dataclasses import dataclass, field


@dataclass
class ExportStats:
    """What happened during an export — surfaced to the user afterwards."""
    total_markers: int = 0
    resolved_markers: int = 0
    unresolved_markers: int = 0  # exported as [?]
    unresolved_sentence_ids: list[str] = field(default_factory=list)
    new_refs_added: int = 0
    existing_refs_renumbered: int = 0
    duplicates_merged: int = 0  # new citations matched to existing bib entries
    citations_converted: int = 0  # numeric sites converted to author-date
    bibliography_size: int = 0
    output_path: str = ""

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
        lines.append(f"New references added: {self.new_refs_added}")
        if self.existing_refs_renumbered:
            lines.append(f"Existing references renumbered: {self.existing_refs_renumbered}")
        if self.citations_converted:
            lines.append(
                f"Citation sites converted to author-date: {self.citations_converted}")
        if self.duplicates_merged:
            lines.append(
                f"Duplicates merged with existing references: {self.duplicates_merged}"
            )
        lines.append(f"Bibliography entries: {self.bibliography_size}")
        return lines

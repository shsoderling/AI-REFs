"""Stage 7: Global quality assurance checks across all citation assignments."""

import logging
from ..models.sentence import SentenceRecord
from ..models.evidence import EvidenceRecord, Warning

logger = logging.getLogger(__name__)


class GlobalQA:
    """Run global QA checks across all evidence assignments."""

    def run(self, sentences: list[SentenceRecord],
            evidence_map: dict[str, EvidenceRecord]) -> dict[str, EvidenceRecord]:
        """Run all global QA checks. Returns updated evidence_map."""
        self._check_duplicate_citations(evidence_map)
        self._check_self_citation_concentration(evidence_map)
        return evidence_map

    def _check_duplicate_citations(self, evidence_map: dict[str, EvidenceRecord]):
        """Warn if the same PMID is selected for many different sentences."""
        pmid_usage = {}  # pmid -> list of sentence_ids
        for sid, ev in evidence_map.items():
            for sel in ev.selected:
                if sel.pmid:
                    pmid_usage.setdefault(sel.pmid, []).append(sid)

        for pmid, sids in pmid_usage.items():
            if len(sids) >= 3:
                for sid in sids:
                    ev = evidence_map[sid]
                    ev.warnings.append(Warning(
                        code="overused_citation",
                        message=f"PMID {pmid} is used in {len(sids)} sentences. Consider diversifying.",
                    ))
                logger.info(f"QA: PMID {pmid} used in {len(sids)} sentences")

    def _check_self_citation_concentration(self, evidence_map: dict[str, EvidenceRecord]):
        """Warn if one author dominates the selected references."""
        author_counts = {}
        total = 0
        for ev in evidence_map.values():
            for sel in ev.selected:
                if sel.authors:
                    last = sel.authors[0].last_name
                    author_counts[last] = author_counts.get(last, 0) + 1
                    total += 1

        if total >= 5:
            for author, count in author_counts.items():
                if count / total >= 0.4:
                    # Flag all evidence that uses this author
                    for ev in evidence_map.values():
                        for sel in ev.selected:
                            if sel.authors and sel.authors[0].last_name == author:
                                ev.warnings.append(Warning(
                                    code="author_concentration",
                                    message=f"Author '{author}' appears in {count}/{total} selected references.",
                                ))
                                break
                    logger.info(f"QA: Author '{author}' concentration: {count}/{total}")

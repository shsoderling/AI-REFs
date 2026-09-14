"""Stage 2: Locate citation markers in parsed sentences.

Recognises ``(REF)`` / ``(REFS)`` plus author-suggested citations such as
``(PMID: 32879322)``, ``(PMC11413553)``, ``(doi: 10.1101/...)`` and
``(Battison et al. 2024)``; see :mod:`src.utils.markers` for the grammar.
"""

import logging
from typing import Optional

from ..models.markers import MarkerConfig, MarkerType
from ..models.sentence import SentenceRecord
from ..utils.markers import find_markers

logger = logging.getLogger(__name__)


class MarkerLocator:
    """Detect and classify reference markers in sentences."""

    def __init__(self, config: Optional[MarkerConfig] = None):
        self.config = config or MarkerConfig.all_on()

    def locate(self, sentences: list[SentenceRecord]) -> list[SentenceRecord]:
        """Scan sentences for markers and populate the marker fields.

        ``markers`` holds every marker with its span and suggestions;
        ``marker_types`` records each marker's kind in document order (a
        sentence can mix (REF), (REFS) and suggested citations, and the
        per-marker splitting downstream must know which is which).
        """
        for sent in sentences:
            markers = find_markers(sent.raw_text, self.config)
            sent.markers = markers
            sent.marker_types = [m.kind for m in markers]
            sent.marker_count = len(markers)
            sent.marker_type = markers[0].kind if markers else None

        marked = [s for s in sentences if s.marker_type is not None]
        suggested = sum(
            1 for s in marked for m in s.markers if m.kind == MarkerType.SUGGESTED
        )
        logger.info(
            f"Located {len(marked)} sentences with markers "
            f"({suggested} author-suggested marker(s))"
        )
        return sentences

    def get_marked_sentences(self, sentences: list[SentenceRecord]) -> list[SentenceRecord]:
        """Return only sentences that have markers."""
        return [s for s in sentences if s.marker_type is not None]

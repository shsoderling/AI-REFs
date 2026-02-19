"""Stage 2: Locate (REF) and (REFS) markers in parsed sentences."""

import re
import logging
from ..models.sentence import SentenceRecord, MarkerType

logger = logging.getLogger(__name__)

MARKER_PATTERN = re.compile(r'\((REFS?)\)')


class MarkerLocator:
    """Detect and classify reference markers in sentences."""

    def locate(self, sentences: list[SentenceRecord]) -> list[SentenceRecord]:
        """Scan sentences for (REF)/(REFS) markers and set marker_type."""
        for sent in sentences:
            matches = MARKER_PATTERN.findall(sent.raw_text)
            if matches:
                # Use the first marker found
                marker_str = matches[0]
                if marker_str == "REFS":
                    sent.marker_type = MarkerType.REFS
                else:
                    sent.marker_type = MarkerType.REF
                sent.marker_count = len(matches)

        marked_count = sum(1 for s in sentences if s.marker_type is not None)
        logger.info(f"Located {marked_count} sentences with markers")
        return sentences

    def get_marked_sentences(self, sentences: list[SentenceRecord]) -> list[SentenceRecord]:
        """Return only sentences that contain markers."""
        return [s for s in sentences if s.marker_type is not None]

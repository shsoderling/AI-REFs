"""Citation cluster rendering driven by the style's CSL file.

The CSL ``<citation>`` element says how a numeric cluster looks: prefix and
suffix around the cluster (or around each number, as in IEEE), the
delimiter, whether consecutive numbers collapse into ranges
(``collapse="citation-number"``) and whether the numbers are superscript.
Author-date styles render "(Smith et al., 2020; Lee, 2021)".
"""

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace

from ..models.citation import CitationCandidate
from ..models.project import (
    AUTHOR_DATE_STYLES, SUPERSCRIPT_STYLES, CitationStyle, get_csl_path,
)
from .citation_numbers import format_bracket_numbers

logger = logging.getLogger(__name__)

UNRESOLVED_TEXT = "[?]"
_CSL_NS = {"csl": "http://purl.org/net/xbiblio/csl"}


@dataclass(frozen=True)
class CitationLayout:
    """How in-text citations of one style are rendered."""
    prefix: str = ""                 # around the whole cluster
    suffix: str = ""
    delimiter: str = ", "
    is_author_date: bool = False
    is_superscript: bool = False
    collapse: bool = False           # consecutive numbers become ranges (3-5)
    number_prefix: str = ""          # around each number / range (IEEE: "[" "]")
    number_suffix: str = ""

    @property
    def render_kind(self) -> str:
        """Render kind recorded in the field payload."""
        if self.is_author_date:
            return "author-date"
        if self.is_superscript:
            return "numeric-superscript"
        if (self.prefix.strip() + self.number_prefix.strip()).startswith("("):
            return "numeric-paren"
        return "numeric-bracket"


def parse_csl_layout(style: CitationStyle) -> CitationLayout:
    """Layout from the style's CSL ``<citation>``; safe defaults on any error."""
    is_author_date = style in AUTHOR_DATE_STYLES
    is_superscript = style in SUPERSCRIPT_STYLES
    prefix, suffix, delimiter, collapse = "", "", ",", False
    number_prefix = number_suffix = ""
    try:
        csl_path = get_csl_path(style)
        if csl_path.exists():
            root = ET.parse(csl_path).getroot()
            citation_el = root.find(".//csl:citation", _CSL_NS)
            if citation_el is not None:
                collapse = citation_el.get("collapse") == "citation-number"
                layout_el = citation_el.find(".//csl:layout", _CSL_NS)
                if layout_el is not None:
                    prefix = layout_el.get("prefix", "")
                    suffix = layout_el.get("suffix", "")
                    delimiter = layout_el.get("delimiter", ",")
                    if layout_el.get("vertical-align") == "sup":
                        is_superscript = True
                    if not is_superscript and not (prefix or suffix):
                        number_prefix, number_suffix = _number_affixes(layout_el)
    except Exception as exc:                        # a broken CSL must not block an export
        logger.warning(f"Could not parse CSL for {style}: {exc}")
        prefix, suffix, delimiter = ("", "", ",") if is_superscript else ("[", "]", ", ")
    if is_author_date and not (prefix or suffix):
        prefix, suffix, delimiter = "(", ")", "; "
    if is_superscript:
        prefix = suffix = number_prefix = number_suffix = ""
    return CitationLayout(prefix, suffix, delimiter, is_author_date, is_superscript,
                          collapse, number_prefix, number_suffix)


def _number_affixes(layout_el) -> tuple[str, str]:
    """Prefix/suffix around each citation number: the affixes of the
    ``<text variable="citation-number">`` element and of every ``<group>``
    enclosing it (IEEE writes ``<group prefix="[" suffix="]">``)."""
    number_el = layout_el.find(".//csl:text[@variable='citation-number']", _CSL_NS)
    if number_el is None:
        return "", ""
    parent = {child: node for node in layout_el.iter() for child in node}
    pre, suf = number_el.get("prefix", ""), number_el.get("suffix", "")
    node = parent.get(number_el)
    while node is not None and node is not layout_el:
        if node.tag == f"{{{_CSL_NS['csl']}}}group":
            pre = node.get("prefix", "") + pre
            suf = suf + node.get("suffix", "")
        node = parent.get(node)
    return pre, suf


def layout_for_existing_shape(layout: CitationLayout, is_superscript_doc: bool) -> CitationLayout:
    """Insert mode: new numeric citations follow the document's existing shape
    (superscript or bracketed) rather than the style's nominal affixes, so
    renumbered old citations and fresh ones look alike. Author-date layouts
    are returned unchanged."""
    if layout.is_author_date:
        return layout
    if is_superscript_doc:
        return replace(layout, prefix="", suffix="", delimiter=",", is_superscript=True,
                       number_prefix="", number_suffix="")
    return replace(layout, prefix="[", suffix="]", delimiter=", ", is_superscript=False,
                   number_prefix="", number_suffix="")


def render_numbers(numbers: list[int], layout: CitationLayout) -> str:
    """Numeric cluster text: sorted, deduplicated, collapsed when the style
    collapses, each chunk wrapped in the per-number affixes and the whole in
    the cluster affixes. Empty -> the unresolved placeholder."""
    ordered = sorted({int(n) for n in numbers})
    if not ordered:
        return UNRESOLVED_TEXT
    if layout.collapse:
        chunks = format_bracket_numbers(ordered).split(", ")
    else:
        chunks = [str(n) for n in ordered]
    inner = layout.delimiter.join(f"{layout.number_prefix}{c}{layout.number_suffix}" for c in chunks)
    return f"{layout.prefix}{inner}{layout.suffix}"


def render_author_date(candidates: list[CitationCandidate], layout: CitationLayout) -> str:
    labels, seen = [], set()
    for c in candidates:
        label = c.first_author_year
        if label not in seen:
            seen.add(label)
            labels.append(label)
    if not labels:
        return UNRESOLVED_TEXT
    return f"{layout.prefix}{layout.delimiter.join(labels)}{layout.suffix}"


def render_cluster(candidates: list[CitationCandidate], numbers: list[int],
                   layout: CitationLayout) -> str:
    """Visible text of one citation cluster."""
    if layout.is_author_date:
        return render_author_date(candidates, layout)
    return render_numbers(numbers, layout)

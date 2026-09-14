"""Render bibliography entries with the citation style's own CSL rules.

The AI REFs app formats every reference entry the same NLM-like way whatever
style is selected; only the in-text citation follows the CSL file.  This
module renders the CSL file's ``<bibliography>`` element instead, so an APA
document gets APA entries and a Nature document gets Nature entries, and
installs itself over the vendored formatter (``install``) so the app's own
writer picks it up.

It implements the part of CSL 1.0.2 that journal-article styles use: macros,
``choose``/``if``/``else-if``/``else``, ``group`` (with the implicit
conditional), ``names``/``name``/``label``/``substitute``, ``date``,
``number``, ``text`` (variable, macro, term, value) and the usual formatting
attributes.  Font styling is dropped, because entries are written as plain
Word runs.  Anything it cannot render falls back to the app's NLM format
rather than producing a half-built entry.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

NS = "{http://purl.org/net/xbiblio/csl}"

# English terms the bundled styles use.  A style that embeds its own <locale>
# overrides these.  (single, multiple) for forms that pluralise.
TERMS: dict[tuple[str, str], tuple[str, str]] = {
    ("and", "long"): ("and", "and"),
    ("and", "symbol"): ("&", "&"),
    ("et-al", "long"): ("et al.", "et al."),
    ("page", "long"): ("page", "pages"),
    ("page", "short"): ("p.", "pp."),
    ("issue", "long"): ("issue", "issues"),
    ("issue", "short"): ("no.", "nos."),
    ("volume", "long"): ("volume", "volumes"),
    ("volume", "short"): ("vol.", "vols."),
    ("edition", "long"): ("edition", "editions"),
    ("edition", "short"): ("ed.", "eds."),
    ("editor", "long"): ("editor", "editors"),
    ("editor", "short"): ("ed.", "eds."),
    ("chapter", "short"): ("chap.", "chaps."),
    ("number", "long"): ("number", "numbers"),
    ("number", "short"): ("no.", "nos."),
    ("section", "short"): ("sec.", "secs."),
    ("supplement", "short"): ("suppl.", "suppls."),
    ("version", "long"): ("version", "versions"),
    ("in", "long"): ("in", "in"),
    ("at", "long"): ("at", "at"),
    ("on", "long"): ("on", "on"),
    ("from", "long"): ("from", "from"),
    ("accessed", "long"): ("accessed", "accessed"),
    ("cited", "long"): ("cited", "cited"),
    ("retrieved", "long"): ("retrieved", "retrieved"),
    ("available at", "long"): ("available at", "available at"),
    ("internet", "long"): ("Internet", "Internet"),
    ("no date", "long"): ("no date", "no date"),
    ("no date", "short"): ("n.d.", "n.d."),
    ("circa", "short"): ("ca.", "ca."),
    ("preprint", "long"): ("preprint", "preprints"),
    ("manuscript", "long"): ("manuscript", "manuscripts"),
    ("personal-communication", "long"): ("personal communication", "personal communications"),
    ("presented at", "long"): ("presented at the", "presented at the"),
    ("review-of", "long"): ("review of", "review of"),
    ("online", "long"): ("online", "online"),
    ("anonymous", "long"): ("anonymous", "anonymous"),
    ("anonymous", "short"): ("anon.", "anon."),
}

MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]
MONTHS_SHORT = ["Jan.", "Feb.", "Mar.", "Apr.", "May", "Jun.",
                "Jul.", "Aug.", "Sep.", "Oct.", "Nov.", "Dec."]

NUMERIC_RE = re.compile(r"^[a-zA-Z]*\d+([-,&\s]+[a-zA-Z]*\d+)*[a-zA-Z]*$")


class CslError(Exception):
    """The style could not be rendered; the caller falls back."""


@dataclass
class Frag:
    """A rendered fragment and what it took to render it.

    ``called`` and ``filled`` drive the implicit conditional on ``cs:group``:
    a group that called at least one variable and filled none is suppressed.
    """
    text: str = ""
    called: int = 0
    filled: int = 0

    def __bool__(self) -> bool:                     # noqa: D105
        return bool(self.text)


@dataclass
class Options:
    """Name options inherited from style / bibliography / names / name."""
    et_al_min: int = 0
    et_al_use_first: int = 0
    et_al_use_last: bool = False
    initialize: bool = True
    initialize_with: Optional[str] = None
    name_as_sort_order: str = ""
    sort_separator: str = ", "
    delimiter: str = ", "
    and_: str = ""
    delimiter_precedes_last: str = "contextual"
    delimiter_precedes_et_al: str = "contextual"
    form: str = "long"
    prefix: str = ""
    suffix: str = ""

    def merged(self, el) -> "Options":
        o = Options(**self.__dict__)
        a = el.attrib
        if "et-al-min" in a:
            o.et_al_min = int(a["et-al-min"])
        if "et-al-use-first" in a:
            o.et_al_use_first = int(a["et-al-use-first"])
        if "et-al-use-last" in a:
            o.et_al_use_last = a["et-al-use-last"] == "true"
        if "initialize" in a:
            o.initialize = a["initialize"] != "false"
        if "initialize-with" in a:
            o.initialize_with = a["initialize-with"]
        if "name-as-sort-order" in a:
            o.name_as_sort_order = a["name-as-sort-order"]
        if "sort-separator" in a:
            o.sort_separator = a["sort-separator"]
        if "delimiter" in a:
            o.delimiter = a["delimiter"]
        if "name-delimiter" in a:                 # spelled this way on cs:style
            o.delimiter = a["name-delimiter"]
        if "and" in a:
            o.and_ = a["and"]
        if "delimiter-precedes-last" in a:
            o.delimiter_precedes_last = a["delimiter-precedes-last"]
        if "delimiter-precedes-et-al" in a:
            o.delimiter_precedes_et_al = a["delimiter-precedes-et-al"]
        if "form" in a:
            o.form = a["form"]
        return o


def _tag(el) -> str:
    return el.tag.replace(NS, "") if isinstance(el.tag, str) else ""


def _apply_format(text: str, el) -> str:
    """Formatting attributes that survive in plain text."""
    if not text:
        return text
    a = el.attrib
    if a.get("strip-periods") == "true":
        text = text.replace(".", "")
    case = a.get("text-case")
    if case == "uppercase":
        text = text.upper()
    elif case == "lowercase":
        text = text.lower()
    elif case == "capitalize-first":
        text = text[:1].upper() + text[1:]
    elif case == "capitalize-all":
        text = " ".join(w[:1].upper() + w[1:] for w in text.split(" "))
    elif case == "sentence":
        text = text[:1].upper() + text[1:]
    elif case == "title":
        small = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "nor", "of",
                 "on", "or", "so", "the", "to", "up", "yet", "via", "with", "from"}
        def _title_word(w: str, i: int, n: int) -> str:
            if any(c.isupper() for c in w[1:]) or w.isupper():
                return w                                   # bioRxiv, CA1, DNA
            if w.lower() in small and 0 < i < n - 1:
                return w.lower()
            return w[:1].upper() + w[1:]
        words = text.split(" ")
        text = " ".join(_title_word(w, i, len(words)) for i, w in enumerate(words))
    if a.get("quotes") == "true":
        text = f"“{text}”"
    return a.get("prefix", "") + text + a.get("suffix", "")


class CslBibliography:
    """One parsed CSL style, ready to render items."""

    def __init__(self, path: str):
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError) as exc:                 # noqa: PERF203
            raise CslError(f"{path}: {exc}") from exc
        self.root = root
        self.macros = {m.attrib.get("name", ""): m for m in root.findall(NS + "macro")}
        self.bibliography = root.find(NS + "bibliography")
        if self.bibliography is None:
            raise CslError("the style has no <bibliography> element")
        self.layout = self.bibliography.find(NS + "layout")
        if self.layout is None:
            raise CslError("the style's bibliography has no <layout>")
        self.terms = dict(TERMS)
        self._load_locale_terms(root)
        self.base = Options()
        for el in (root, self.bibliography):
            self.base = self.base.merged(el)

    # ── locale ──────────────────────────────────────────────────────
    def _load_locale_terms(self, root) -> None:
        for locale in root.findall(NS + "locale"):
            lang = locale.attrib.get("{http://www.w3.org/XML/1998/namespace}lang", "en")
            if not lang.startswith("en"):
                continue
            for term in locale.iter(NS + "term"):
                name = term.attrib.get("name", "")
                form = term.attrib.get("form", "long")
                single = term.findtext(NS + "single")
                multiple = term.findtext(NS + "multiple")
                if single is not None or multiple is not None:
                    self.terms[(name, form)] = (single or multiple or "", multiple or single or "")
                elif term.text is not None:
                    self.terms[(name, form)] = (term.text, term.text)

    def term(self, name: str, form: str = "long", plural: bool = False) -> str:
        for f in (form, "long", "short", "verb", "symbol"):
            if (name, f) in self.terms:
                single, multiple = self.terms[(name, f)]
                return multiple if plural else single
        return ""

    # ── entry point ─────────────────────────────────────────────────
    def render(self, item: dict, number: Optional[int] = None) -> str:
        ctx = {"item": dict(item)}
        if number is not None:
            ctx["item"]["citation-number"] = str(number)
        frag = self._children(self.layout, ctx, self.base, self.layout.attrib.get("delimiter", ""))
        text = _apply_format(frag.text, self.layout)
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"([.,;:])\1+", r"\1", text)
        text = text.replace(" ,", ",").replace(" .", ".").replace("..", ".")
        # A style that assumes every article has a volume leaves "2024;." behind
        # when one does not; collapse punctuation that ended up adjacent.
        text = re.sub(r"[;,:]\s*([.;])", r"\1", text)
        text = re.sub(r"[;,:]\s*$", "", text).strip()
        if self.bibliography.attrib.get("second-field-align"):
            text = re.sub(r"^(\(?\[?\d+\]?[.)]?)(?=[^\s.,;:)\]])", r"\1 ", text)
        return text

    # ── element dispatch ────────────────────────────────────────────
    def _children(self, el, ctx, opts: Options, delimiter: str = "") -> Frag:
        parts, called, filled = [], 0, 0
        for frag in self._flatten(el, ctx, opts):
            called += frag.called
            filled += frag.filled
            if frag.text:
                parts.append(frag.text)
        return Frag(delimiter.join(parts), called, filled)

    def _flatten(self, el, ctx, opts: Options):
        """Fragments for *el*'s children, with choose branches spliced in.

        CSL treats the contents of the chosen branch as children of the
        enclosing element, so the enclosing group's delimiter falls between
        them: without this, "Nat Commun" and "2020;11(1):4395" run together.
        """
        for child in el:
            if _tag(child) == "choose":
                branch = self._branch(child, ctx)
                if branch is not None:
                    yield from self._flatten(branch, ctx, opts)
            else:
                yield self._render(child, ctx, opts)

    def _branch(self, choose_el, ctx):
        for branch in choose_el:
            tag = _tag(branch)
            if tag in ("if", "else-if"):
                if self._test(branch, ctx):
                    return branch
            elif tag == "else":
                return branch
        return None

    def _render(self, el, ctx, opts: Options) -> Frag:
        tag = _tag(el)
        handler = getattr(self, f"_do_{tag.replace('-', '_')}", None)
        if handler is None:
            return Frag()
        return handler(el, ctx, opts)

    # ── elements ────────────────────────────────────────────────────
    def _do_text(self, el, ctx, opts) -> Frag:
        a = el.attrib
        if "macro" in a:
            macro = self.macros.get(a["macro"])
            if macro is None:
                return Frag()
            inner = self._children(macro, ctx, opts, macro.attrib.get("delimiter", ""))
            return Frag(_apply_format(inner.text, el), inner.called, inner.filled)
        if "term" in a:
            plural = a.get("plural") == "true"
            return Frag(_apply_format(self.term(a["term"], a.get("form", "long"), plural), el))
        if "value" in a:
            return Frag(_apply_format(a["value"], el))
        if "variable" in a:
            value = self._variable(ctx, a["variable"], a.get("form", ""))
            return Frag(_apply_format(value, el), called=1, filled=1 if value else 0)
        return Frag()

    def _do_group(self, el, ctx, opts) -> Frag:
        inner = self._children(el, ctx, opts, el.attrib.get("delimiter", ""))
        # The implicit conditional: a group that asked for variables and got
        # none renders nothing, which is what keeps "vol. , no. ," out.
        if inner.called and not inner.filled:
            return Frag("", inner.called, 0)
        if not inner.text.strip():
            return Frag("", inner.called, inner.filled)
        return Frag(_apply_format(inner.text, el), inner.called, inner.filled)

    def _do_choose(self, el, ctx, opts) -> Frag:
        branch = self._branch(el, ctx)
        if branch is None:
            return Frag()
        return self._children(branch, ctx, opts, branch.attrib.get("delimiter", ""))

    def _do_names(self, el, ctx, opts) -> Frag:
        opts = opts.merged(el)
        variables = el.attrib.get("variable", "").split()
        name_el = el.find(NS + "name")
        label_el = el.find(NS + "label")
        substitute = el.find(NS + "substitute")
        name_opts = opts.merged(name_el) if name_el is not None else opts

        rendered, called, filled = [], 0, 0
        for var in variables:
            called += 1
            people = ctx["item"].get(var) or []
            if not people:
                continue
            filled += 1
            text = self._names_text(people, name_opts, name_el)
            if label_el is not None:
                label = self.term(var, label_el.attrib.get("form", "long"), plural=len(people) > 1)
                if label:
                    text += _apply_format(label, label_el)
            rendered.append(text)
        if not rendered and substitute is not None:
            ctx.setdefault("suppress", set())
            ctx["record_vars"] = set()
            for child in substitute:
                # A substituted <names> usually has no <name> of its own: CSL
                # says it inherits the options of the names it stands in for.
                frag = (self._do_names_with(child, ctx, name_opts, name_el, label_el)
                        if _tag(child) == "names" else self._render(child, ctx, opts))
                if frag.text:
                    ctx["suppress"].update(ctx.pop("record_vars", set()))
                    return Frag(_apply_format(frag.text, el), called + frag.called, frag.filled)
            ctx.pop("record_vars", None)
        if not rendered:
            return Frag("", called, 0)
        joined = el.attrib.get("delimiter", "; ").join(rendered)
        return Frag(_apply_format(joined, el), called, filled)

    def _do_names_with(self, el, ctx, inherited: Options, parent_name_el, parent_label_el) -> Frag:
        """Render a <names> that inherits another's name options (substitute)."""
        opts = inherited.merged(el)
        name_el = el.find(NS + "name")
        if name_el is not None:
            opts = opts.merged(name_el)
        label_el = el.find(NS + "label")
        if label_el is None:
            label_el = parent_label_el
        rendered, called, filled = [], 0, 0
        for var in el.attrib.get("variable", "").split():
            called += 1
            people = ctx["item"].get(var) or []
            if not people:
                continue
            filled += 1
            text = self._names_text(people, opts, name_el or parent_name_el)
            if label_el is not None:
                label = self.term(var, label_el.attrib.get("form", "long"), plural=len(people) > 1)
                if label:
                    text += _apply_format(label, label_el)
            rendered.append(text)
        if not rendered:
            return Frag("", called, 0)
        return Frag(_apply_format(el.attrib.get("delimiter", "; ").join(rendered), el), called, filled)

    def _names_text(self, people: list, opts: Options, name_el) -> str:
        names = [self._one_name(p, opts, i) for i, p in enumerate(people)]
        total = len(names)
        truncated = False
        if opts.et_al_min and total >= opts.et_al_min and opts.et_al_use_first:
            keep = names[:opts.et_al_use_first]
            last = names[-1:] if opts.et_al_use_last and total > opts.et_al_use_first else []
            names, truncated = keep, True
        else:
            last = []

        delim = opts.delimiter
        if truncated:
            text = delim.join(names)
            if last:
                text += f"{delim}... {last[0]}"
            else:
                et_al = self.term("et-al", "long")
                sep = delim if (opts.delimiter_precedes_et_al == "always"
                                or (len(names) > 1 and opts.delimiter_precedes_et_al == "contextual")) else " "
                text += f"{sep}{et_al}" if et_al else ""
            return text
        if len(names) > 1 and opts.and_:
            and_word = self.term("and", "symbol" if opts.and_ == "symbol" else "long")
            precedes = (opts.delimiter_precedes_last == "always"
                        or (opts.delimiter_precedes_last == "contextual" and len(names) > 2)
                        or (opts.delimiter_precedes_last == "after-inverted-name"
                            and opts.name_as_sort_order in ("first", "all")))
            head = delim.join(names[:-1])
            return f"{head}{delim if precedes else ' '}{and_word} {names[-1]}"
        return delim.join(names)

    def _one_name(self, person: dict, opts: Options, index: int) -> str:
        family = (person.get("family") or "").strip()
        given = (person.get("given") or "").strip()
        literal = (person.get("literal") or "").strip()
        if literal:
            return literal
        if opts.form == "short" or not given:
            return family
        if opts.initialize_with is not None:
            parts = []
            for token in re.split(r"[\s.]+", given):
                if not token:
                    continue
                # PubMed records carry initials as one blob ("JA"): each letter
                # is its own initial, or "Smith JA" would come out "Smith J".
                if token.isalpha() and token.isupper() and len(token) > 1:
                    parts.extend(list(token))
                else:
                    parts.append(token)
            initials = "".join(p[0].upper() + opts.initialize_with for p in parts).strip()
            given_out = initials.strip()
            if not opts.initialize and len(given) > 2:
                given_out = given
        else:
            given_out = given
        inverted = (opts.name_as_sort_order == "all"
                    or (opts.name_as_sort_order == "first" and index == 0))
        if inverted:
            sep = opts.sort_separator
            # "Smith, JA" for initialised names keeps the CSL separator; a
            # trailing period from initialize-with is not doubled.
            return f"{family}{sep}{given_out}".strip().rstrip(",")
        return f"{given_out} {family}".strip()

    def _do_date(self, el, ctx, opts) -> Frag:
        var = el.attrib.get("variable", "issued")
        value = ctx["item"].get(var)
        parts = (value or {}).get("date-parts") or []
        if not parts or not parts[0]:
            return Frag("", called=1, filled=0)
        nums = parts[0]
        year = str(nums[0]) if len(nums) > 0 else ""
        month = int(nums[1]) if len(nums) > 1 else 0
        day = int(nums[2]) if len(nums) > 2 else 0

        date_parts_el = [c for c in el if _tag(c) == "date-part"]
        if not date_parts_el:
            wanted = el.attrib.get("date-parts", "year-month-day")
            form = el.attrib.get("form", "numeric")
            pieces = [year]
            if month and wanted in ("year-month", "year-month-day"):
                pieces.append(MONTHS_SHORT[month - 1] if form == "text" else f"{month:02d}")
            if day and wanted == "year-month-day":
                pieces.append(str(day))
            text = (" ".join(pieces) if form == "text" else "-".join(pieces))
            return Frag(_apply_format(text, el), called=1, filled=1)

        out = []
        for part in date_parts_el:
            name = part.attrib.get("name", "")
            form = part.attrib.get("form", "")
            if name == "year" and year:
                out.append(_apply_format(year, part))
            elif name == "month" and month:
                if form in ("numeric", "numeric-leading-zeros"):
                    text = f"{month:02d}" if form == "numeric-leading-zeros" else str(month)
                elif form == "short":
                    text = MONTHS_SHORT[month - 1]
                else:
                    text = MONTHS[month - 1]
                out.append(_apply_format(text, part))
            elif name == "day" and day:
                out.append(_apply_format(str(day), part))
        text = el.attrib.get("delimiter", "").join(p for p in out if p)
        return Frag(_apply_format(text, el), called=1, filled=1 if text else 0)

    def _do_number(self, el, ctx, opts) -> Frag:
        value = self._variable(ctx, el.attrib.get("variable", ""), "")
        return Frag(_apply_format(value, el), called=1, filled=1 if value else 0)

    def _do_label(self, el, ctx, opts) -> Frag:
        var = el.attrib.get("variable", "")
        value = self._variable(ctx, var, "")
        if not value:
            return Frag("", called=1, filled=0)
        plural = bool(re.search(r"[-–,&]", value)) if var in ("page", "number-of-pages") else False
        term = self.term(var, el.attrib.get("form", "long"), plural=plural)
        return Frag(_apply_format(term, el), called=1, filled=1 if term else 0)

    # ── variables and conditions ────────────────────────────────────
    def _variable(self, ctx, name: str, form: str) -> str:
        item = ctx["item"]
        if not name:
            return ""
        if name in ctx.get("suppress", ()):        # already shown in the author slot
            return ""
        if "record_vars" in ctx:
            ctx["record_vars"].add(name)
        if name == "page-first":
            pages = str(item.get("page") or "")
            return re.split(r"[-\u2013,]", pages)[0].strip() if pages else ""
        if name == "container-title" and form == "short":
            return str(item.get("container-title-short") or item.get("container-title") or "")
        if name in ("author", "editor", "translator"):
            people = item.get(name) or []
            return ", ".join(p.get("family", "") for p in people)
        value = item.get(name)
        if value is None and name == "citation-number":
            return ""
        if isinstance(value, dict):
            parts = (value.get("date-parts") or [[]])[0]
            return str(parts[0]) if parts else ""
        if value is None:
            return ""
        return str(value)

    def _test(self, el, ctx) -> bool:
        a = el.attrib
        match = a.get("match", "all")
        results = []
        if "variable" in a:
            results += [bool(self._has(ctx, v)) for v in a["variable"].split()]
        if "type" in a:
            item_type = ctx["item"].get("type", "")
            results += [item_type == t for t in a["type"].split()]
        if "is-numeric" in a:
            results += [bool(NUMERIC_RE.match(self._variable(ctx, v, ""))) for v in a["is-numeric"].split()]
        if "is-uncertain-date" in a:
            results += [False for _ in a["is-uncertain-date"].split()]
        if "locator" in a:
            results += [False for _ in a["locator"].split()]
        if "position" in a:
            results += [False for _ in a["position"].split()]
        if "disambiguate" in a:
            results.append(False)
        if not results:
            return True
        if match == "any":
            return any(results)
        if match == "none":
            return not any(results)
        return all(results)

    def _has(self, ctx, name: str) -> bool:
        item = ctx["item"]
        if name in ("author", "editor", "translator"):
            return bool(item.get(name))
        if name == "container-title":
            return bool(item.get("container-title") or item.get("container-title-short"))
        value = item.get(name)
        if isinstance(value, dict):
            return bool((value.get("date-parts") or [[]])[0])
        return bool(value)


# ── drop-in for the app's formatter ──────────────────────────────────

_CACHE: dict[str, CslBibliography] = {}
_APPEND_IDS = False
_ORIGINAL = None                      # the app's NLM formatter, kept for uninstall()


def _load_original():
    """The app's NLM formatter, imported lazily so patching cannot recurse."""
    global _ORIGINAL
    if _ORIGINAL is None:
        from airefs.pipeline import bib_format
        _ORIGINAL = bib_format.format_bib_entry
    return _ORIGINAL


def _style_bibliography(style) -> Optional[CslBibliography]:
    from airefs.models.project import get_csl_path
    key = getattr(style, "value", str(style))
    if key not in _CACHE:
        try:
            _CACHE[key] = CslBibliography(str(get_csl_path(style)))
        except CslError:
            return None
    return _CACHE[key]


def format_bib_entry(citation, number: int, style) -> str:
    """Drop-in replacement for ``airefs.pipeline.bib_format.format_bib_entry``."""
    from airefs.models.project import AUTHOR_DATE_STYLES
    from airefs.pipeline.csl_mapping import to_csl_item
    nlm_entry = _ORIGINAL or _load_original()

    bib = _style_bibliography(style)
    if bib is None:
        return nlm_entry(citation, number, style)
    try:
        text = bib.render(to_csl_item(citation), number)
    except Exception:                                            # noqa: BLE001
        return nlm_entry(citation, number, style)
    if not text or sum(c.isalnum() for c in text) < 6:           # nothing usable came out
        return nlm_entry(citation, number, style)

    numeric = style not in AUTHOR_DATE_STYLES
    if numeric and not re.match(rf"^\[?{number}\b", text):
        text = f"{number}. {text}"
    if _APPEND_IDS:
        if citation.doi and citation.doi.lower() not in text.lower():
            text += f" doi:{citation.doi}"
        if citation.pmid and citation.pmid not in text:
            text += f" PMID: {citation.pmid}"
    return text


def install(append_identifiers: bool = False) -> None:
    """Make the app's writer use the style's own bibliography rules.

    ``docx_export`` and ``tracked_renumber`` bind ``format_bib_entry`` at
    import time, so the name is replaced in each module that holds it.
    """
    global _APPEND_IDS, _ORIGINAL
    _APPEND_IDS = append_identifiers
    from airefs.pipeline import bib_format, docx_export, tracked_renumber
    if _ORIGINAL is None:
        _ORIGINAL = bib_format.format_bib_entry
    for module in (bib_format, docx_export, tracked_renumber):
        if hasattr(module, "format_bib_entry"):
            module.format_bib_entry = format_bib_entry


def uninstall() -> None:
    """Restore the app's NLM formatter (used by the tests)."""
    if _ORIGINAL is None:
        return
    from airefs.pipeline import bib_format, docx_export, tracked_renumber
    for module in (bib_format, docx_export, tracked_renumber):
        if hasattr(module, "format_bib_entry"):
            module.format_bib_entry = _ORIGINAL

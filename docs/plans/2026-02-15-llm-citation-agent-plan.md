# LLM Citation Agent Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the deterministic citation pipeline (stages 3-6) with a Claude tool-use agent that searches PubMed natively, using the Anthropic SDK with tool_use.

**Architecture:** A new `LLMCitationAgent` class sends each claim sentence to Claude with PubMed search/fetch defined as tools. Claude iteratively searches, reads abstracts, and returns structured JSON with selected citations, confidence, and verification. The existing `PubMedClient` and `BioRxivClient` serve as tool backends. The orchestrator collapses from 7 stages to 4.

**Tech Stack:** Python 3.11+, PySide6, Anthropic Python SDK, NCBI E-utilities (existing), Pydantic

---

### Task 1: Add anthropic dependency

**Files:**
- Modify: `requirements.txt`

**Step 1: Add the anthropic package**

Add `anthropic>=0.42.0` to `requirements.txt` after the `requests` line:

```
requests>=2.31.0
anthropic>=0.42.0
```

**Step 2: Install it**

Run: `pip install anthropic>=0.42.0`

**Step 3: Verify import works**

Run: `python -c "import anthropic; print(anthropic.__version__)"`
Expected: prints version number without error

---

### Task 2: Add new settings fields to ProjectSettings

**Files:**
- Modify: `src/models/project.py:35-62`

**Step 1: Add anthropic_api_key and claude_model fields**

Add these two fields to the `ProjectSettings` class, after the `ncbi_email` / PubMed section (after line 54):

```python
    # Anthropic / Claude
    anthropic_api_key: Optional[str] = Field(default=None, description="Anthropic API key for Claude-powered citation search")
    claude_model: str = Field(default="claude-haiku-4-5-20251001", description="Claude model for citation agent")
```

**Step 2: Add AI_CITATION_SEARCH to PipelineStage enum**

Add a new stage value to the `PipelineStage` enum (after `MARKER_LOCATION`, replacing the old granular stages):

```python
    AI_CITATION_SEARCH = "ai_citation_search"
```

Keep the old enum values in place for backwards compatibility with saved projects.

**Step 3: Verify the model loads**

Run: `python -c "from src.models.project import ProjectSettings; s = ProjectSettings(); print(s.claude_model)"`
Expected: `claude-haiku-4-5-20251001`

---

### Task 3: Create LLMCitationAgent — tool definitions and system prompt

**Files:**
- Create: `src/pipeline/llm_citation_agent.py`

**Step 1: Create the file with imports, constants, and tool schemas**

```python
"""LLM-powered citation agent using Claude tool-use.

Replaces the deterministic claim extraction, candidate retrieval,
ranking, and verification stages with a single Claude agent loop
that searches PubMed iteratively and selects the best citations.
"""

import json
import logging
from typing import Optional, Callable

import anthropic

from ..models.sentence import SentenceRecord, MarkerType
from ..models.citation import CitationCandidate
from ..models.evidence import (
    EvidenceRecord, ConfidenceLevel, VerificationStatus,
)
from ..services.pubmed_client import PubMedClient
from ..services.biorxiv_client import BioRxivClient

logger = logging.getLogger(__name__)

MAX_AGENT_ROUNDS = 10

SYSTEM_PROMPT = """\
You are a biomedical citation-finding assistant. Your job is to find the best \
PubMed references that directly support a given claim sentence from a research \
document.

Instructions:
1. Read the claim sentence carefully. Identify the key scientific assertion.
2. Use the search_pubmed tool to find relevant articles. Start with a specific \
query. If you get fewer than 3 results, broaden your search terms.
3. Use the fetch_articles tool to retrieve full metadata (title, authors, year, \
journal, abstract) for promising PMIDs.
4. Read the abstracts carefully. Select ONLY articles whose abstracts contain \
evidence that directly supports the claim.
5. For a (REF) marker, select exactly 1 best reference. For a (REFS) marker, \
select {max_refs} references.
6. Prefer peer-reviewed primary research over reviews unless the claim is a \
broad summary statement. Prefer recent publications when relevance is equal.

When you have made your selections, respond with ONLY a JSON object (no markdown \
fencing, no extra text) in this exact format:
{{
  "selected": [
    {{
      "pmid": "12345678",
      "why": "Brief explanation of why this paper supports the claim"
    }}
  ],
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "confidence_score": 0-100,
  "confidence_rationale": "Why you are this confident",
  "verification_status": "verified" | "partial" | "indirect" | "weak",
  "supporting_snippets": ["Relevant quote from abstract..."],
  "search_queries_used": ["query1", "query2"],
  "all_pmids_considered": ["12345678", "23456789"]
}}

If you cannot find any supporting references after thorough searching, return:
{{
  "selected": [],
  "confidence": "LOW",
  "confidence_score": 0,
  "confidence_rationale": "No supporting references found after exhaustive search",
  "verification_status": "weak",
  "supporting_snippets": [],
  "search_queries_used": ["query1"],
  "all_pmids_considered": []
}}
"""

TOOLS = [
    {
        "name": "search_pubmed",
        "description": (
            "Search PubMed for articles matching a query. Returns a list of "
            "PubMed IDs (PMIDs) and the total number of results. Use standard "
            "PubMed query syntax: combine terms with AND/OR, use [MeSH] tags, "
            "field tags like [ti] (title), [tiab] (title/abstract), [au] (author). "
            "Example: 'Rac1 AND dendritic spine AND hippocampus'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "PubMed search query"
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of PMIDs to return (default 20)",
                    "default": 20
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "fetch_articles",
        "description": (
            "Fetch full metadata for one or more PubMed articles by their PMIDs. "
            "Returns title, authors, year, journal, abstract, DOI, MeSH terms "
            "for each article. Use this after search_pubmed to read abstracts "
            "and evaluate relevance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pmids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of PubMed IDs to fetch"
                }
            },
            "required": ["pmids"]
        }
    },
]

BIORXIV_TOOL = {
    "name": "search_biorxiv",
    "description": (
        "Search bioRxiv preprints by keywords. Returns matching preprints with "
        "title, authors, year, abstract, and DOI. Use this to supplement PubMed "
        "results with recent preprints that may not yet be indexed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Search keywords"
            },
            "category": {
                "type": "string",
                "description": "Optional bioRxiv category (e.g. 'neuroscience', 'cell_biology')",
                "default": ""
            }
        },
        "required": ["keywords"]
    }
}
```

**Step 2: Verify syntax**

Run: `python -c "from src.pipeline.llm_citation_agent import SYSTEM_PROMPT, TOOLS; print('OK')"`
Expected: `OK`

---

### Task 4: Create LLMCitationAgent — tool execution methods

**Files:**
- Modify: `src/pipeline/llm_citation_agent.py` (append to file)

**Step 1: Add the LLMCitationAgent class with __init__ and tool execution**

Append to the file:

```python


class LLMCitationAgent:
    """Claude-powered citation finder using tool-use."""

    def __init__(
        self,
        anthropic_api_key: str,
        model: str,
        pubmed_client: PubMedClient,
        biorxiv_client: Optional[BioRxivClient] = None,
        search_biorxiv: bool = True,
        max_refs: int = 3,
        log_callback: Optional[Callable[[str, str], None]] = None,
    ):
        self.client = anthropic.Anthropic(api_key=anthropic_api_key)
        self.model = model
        self.pubmed = pubmed_client
        self.biorxiv = biorxiv_client
        self.search_biorxiv = search_biorxiv and biorxiv_client is not None
        self.max_refs = max_refs
        self._log = log_callback or (lambda *a: None)

        # Build tool list
        self.tools = list(TOOLS)
        if self.search_biorxiv:
            self.tools.append(BIORXIV_TOOL)

    def _emit(self, level: str, msg: str):
        getattr(logger, level, logger.info)(msg)
        self._log(level, msg)

    def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        """Execute a tool call and return the result as a string."""
        try:
            if tool_name == "search_pubmed":
                query = tool_input["query"]
                max_results = tool_input.get("max_results", 20)
                self._emit("info", f"    PubMed search: {query}")
                pmids, total = self.pubmed.search(query, max_results=max_results)
                return json.dumps({
                    "pmids": pmids,
                    "total_results": total,
                    "returned": len(pmids),
                })

            elif tool_name == "fetch_articles":
                pmids = tool_input["pmids"]
                self._emit("info", f"    Fetching {len(pmids)} article(s)...")
                articles = self.pubmed.fetch_articles(pmids)
                result = []
                for a in articles:
                    result.append({
                        "pmid": a.pmid,
                        "title": a.title,
                        "authors": ", ".join(
                            au.display for au in a.authors[:6]
                        ) + (" et al." if len(a.authors) > 6 else ""),
                        "year": a.year,
                        "journal": a.journal_abbrev or a.journal,
                        "doi": a.doi,
                        "abstract": a.abstract[:2000],  # Truncate long abstracts
                        "mesh_terms": a.mesh_terms[:10],
                        "is_review": a.is_review,
                        "is_retracted": a.is_retracted,
                    })
                return json.dumps(result)

            elif tool_name == "search_biorxiv":
                keywords = tool_input["keywords"]
                category = tool_input.get("category", "")
                self._emit("info", f"    bioRxiv search: {keywords}")
                categories = [category] if category else None
                candidates = self.biorxiv.search_by_keywords(
                    keywords=keywords,
                    categories=categories,
                    days=365 * 3,
                    max_results=10,
                )
                result = []
                for c in candidates:
                    result.append({
                        "doi": c.doi,
                        "title": c.title,
                        "authors": ", ".join(
                            au.display for au in c.authors[:6]
                        ) + (" et al." if len(c.authors) > 6 else ""),
                        "year": c.year,
                        "journal": c.journal,
                        "abstract": c.abstract[:2000],
                    })
                return json.dumps(result)

            else:
                return json.dumps({"error": f"Unknown tool: {tool_name}"})

        except Exception as e:
            logger.error(f"Tool execution error ({tool_name}): {e}")
            return json.dumps({"error": str(e)})
```

**Step 2: Verify syntax**

Run: `python -c "from src.pipeline.llm_citation_agent import LLMCitationAgent; print('OK')"`
Expected: `OK`

---

### Task 5: Create LLMCitationAgent — agent loop and response parsing

**Files:**
- Modify: `src/pipeline/llm_citation_agent.py` (append to LLMCitationAgent class)

**Step 1: Add the find_citations method (agent loop)**

Append these methods to the `LLMCitationAgent` class:

```python
    def find_citations(
        self,
        sentence: SentenceRecord,
        domain_context: list[str] = None,
    ) -> EvidenceRecord:
        """Run the agent loop for a single sentence. Returns an EvidenceRecord."""
        marker_type = sentence.marker_type or MarkerType.REF
        num_refs = 1 if marker_type == MarkerType.REF else self.max_refs

        system = SYSTEM_PROMPT.format(max_refs=num_refs)

        domain_info = ""
        if domain_context:
            domain_info = f"\nDocument research domains: {', '.join(domain_context)}"

        user_message = (
            f"Find {'1 reference' if num_refs == 1 else f'{num_refs} references'} "
            f"that support this claim:\n\n"
            f"\"{sentence.clean_text}\"{domain_info}"
        )

        messages = [{"role": "user", "content": user_message}]

        # Track all candidates seen across rounds
        all_pmids_seen: dict[str, CitationCandidate] = {}
        queries_used = []

        evidence = EvidenceRecord(sentence_id=sentence.id)

        try:
            for round_num in range(MAX_AGENT_ROUNDS):
                self._emit("info", f"    Agent round {round_num + 1}...")

                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=system,
                    tools=self.tools,
                    messages=messages,
                )

                # Check if Claude wants to use tools
                if response.stop_reason == "tool_use":
                    # Process all tool calls in this response
                    tool_results = []
                    for block in response.content:
                        if block.type == "tool_use":
                            tool_result = self._execute_tool(
                                block.name, block.input
                            )
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": tool_result,
                            })

                            # Track search queries
                            if block.name == "search_pubmed":
                                queries_used.append(block.input.get("query", ""))

                            # Track fetched articles
                            if block.name == "fetch_articles":
                                pmids = block.input.get("pmids", [])
                                articles = self.pubmed.fetch_articles(pmids)
                                for a in articles:
                                    if a.pmid not in all_pmids_seen:
                                        all_pmids_seen[a.pmid] = a

                    # Add assistant response and tool results to conversation
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": tool_results})

                elif response.stop_reason == "end_turn":
                    # Claude is done — parse the final response
                    text = ""
                    for block in response.content:
                        if hasattr(block, "text"):
                            text += block.text

                    evidence = self._parse_response(
                        text, sentence, all_pmids_seen, queries_used
                    )
                    self._emit(
                        "info",
                        f"    Result: {len(evidence.selected)} ref(s), "
                        f"confidence={evidence.confidence_level.value}"
                    )
                    return evidence

                else:
                    # Unexpected stop reason
                    self._emit(
                        "warning",
                        f"    Unexpected stop_reason: {response.stop_reason}"
                    )
                    break

            # Hit max rounds — return what we have
            self._emit("warning", f"    Hit max rounds ({MAX_AGENT_ROUNDS})")
            evidence.candidates = list(all_pmids_seen.values())
            evidence.search_query = " | ".join(queries_used)
            evidence.confidence_level = ConfidenceLevel.LOW
            evidence.confidence_rationale = (
                f"Agent hit {MAX_AGENT_ROUNDS}-round limit without completing"
            )
            return evidence

        except anthropic.AuthenticationError as e:
            raise RuntimeError(
                f"Anthropic API authentication failed. Check your API key. ({e})"
            ) from e
        except anthropic.APIError as e:
            self._emit("error", f"    Anthropic API error: {e}")
            evidence.retrieval_error = f"API error: {e}"
            evidence.confidence_level = ConfidenceLevel.UNRESOLVED
            return evidence
        except Exception as e:
            self._emit("error", f"    Agent error: {e}")
            evidence.retrieval_error = str(e)
            evidence.confidence_level = ConfidenceLevel.UNRESOLVED
            return evidence

    def _parse_response(
        self,
        text: str,
        sentence: SentenceRecord,
        all_candidates: dict[str, CitationCandidate],
        queries_used: list[str],
    ) -> EvidenceRecord:
        """Parse Claude's final JSON response into an EvidenceRecord."""
        evidence = EvidenceRecord(sentence_id=sentence.id)

        try:
            # Strip markdown fencing if present
            cleaned = text.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                # Remove first and last lines (``` markers)
                lines = [l for l in lines if not l.strip().startswith("```")]
                cleaned = "\n".join(lines)

            data = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"Failed to parse agent response: {e}\nRaw: {text[:500]}")
            evidence.retrieval_error = f"Failed to parse agent response: {e}"
            evidence.confidence_level = ConfidenceLevel.UNRESOLVED
            evidence.candidates = list(all_candidates.values())
            evidence.search_query = " | ".join(queries_used)
            return evidence

        # Selected citations
        selected_pmids = []
        for sel in data.get("selected", []):
            pmid = str(sel.get("pmid", ""))
            if pmid:
                selected_pmids.append(pmid)

        # Fetch any selected PMIDs we haven't seen yet
        missing = [p for p in selected_pmids if p not in all_candidates]
        if missing:
            fetched = self.pubmed.fetch_articles(missing)
            for a in fetched:
                all_candidates[a.pmid] = a

        # Build selected list
        evidence.selected = [
            all_candidates[p] for p in selected_pmids if p in all_candidates
        ]

        # All candidates considered
        considered_pmids = data.get("all_pmids_considered", [])
        for p in considered_pmids:
            if p not in all_candidates:
                # Try to fetch
                fetched = self.pubmed.fetch_articles([p])
                for a in fetched:
                    all_candidates[a.pmid] = a
        evidence.candidates = list(all_candidates.values())

        # Confidence
        conf_str = data.get("confidence", "LOW").upper()
        conf_map = {
            "HIGH": ConfidenceLevel.HIGH,
            "MEDIUM": ConfidenceLevel.MEDIUM,
            "LOW": ConfidenceLevel.LOW,
        }
        evidence.confidence_level = conf_map.get(conf_str, ConfidenceLevel.LOW)
        evidence.confidence_score = float(data.get("confidence_score", 0))
        evidence.confidence_rationale = data.get("confidence_rationale", "")

        # Verification
        ver_str = data.get("verification_status", "not_checked").lower()
        ver_map = {
            "verified": VerificationStatus.VERIFIED,
            "partial": VerificationStatus.PARTIAL,
            "indirect": VerificationStatus.INDIRECT,
            "weak": VerificationStatus.WEAK,
        }
        evidence.verification_status = ver_map.get(
            ver_str, VerificationStatus.NOT_CHECKED
        )

        # Snippets and queries
        evidence.abstract_snippets = data.get("supporting_snippets", [])
        evidence.search_query = " | ".join(
            data.get("search_queries_used", queries_used)
        )
        evidence.search_result_count = len(all_candidates)

        return evidence
```

**Step 2: Verify full module loads**

Run: `python -c "from src.pipeline.llm_citation_agent import LLMCitationAgent; print('OK')"`
Expected: `OK`

---

### Task 6: Rewrite the orchestrator to use LLMCitationAgent

**Files:**
- Modify: `src/pipeline/orchestrator.py`

**Step 1: Replace the entire orchestrator file**

The new orchestrator has 4 stages instead of 7. Replace the full contents of `orchestrator.py`:

```python
"""Pipeline orchestrator: chains all stages in sequence.

Can run headlessly or emit Qt signals for GUI progress updates.
"""

import logging
from typing import Optional, Callable

from ..models.project import ProjectState, PipelineStage
from ..models.sentence import MarkerType
from ..models.evidence import EvidenceRecord, ConfidenceLevel
from ..services.docx_io import DocxHandler
from ..services.pubmed_client import PubMedClient
from ..services.biorxiv_client import BioRxivClient
from ..storage.cache_db import CacheDB
from .document_parser import DocumentParser
from .marker_locator import MarkerLocator
from .llm_citation_agent import LLMCitationAgent
from .claim_extractor import infer_domains
from .global_qa import GlobalQA

logger = logging.getLogger(__name__)

TOTAL_STAGES = 4


class PipelineOrchestrator:
    """Run the full reference-finding pipeline."""

    def __init__(self, project: ProjectState,
                 progress_callback: Optional[Callable[[str, int, int], None]] = None,
                 log_callback: Optional[Callable[[str, str], None]] = None):
        """
        Args:
            project: ProjectState to populate
            progress_callback: Called with (stage_name, current, total) for progress
            log_callback: Called with (level, message) for GUI log panel
        """
        self.project = project
        self._progress = progress_callback or (lambda *a: None)
        self._log = log_callback or (lambda *a: None)
        self._paused = False
        self._cancelled = False

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def cancel(self):
        self._cancelled = True

    def _check_pause(self):
        """Block until unpaused or cancelled."""
        while self._paused and not self._cancelled:
            import time
            time.sleep(0.1)

    def _emit(self, level: str, msg: str):
        """Emit a log message."""
        getattr(logger, level, logger.info)(msg)
        self._log(level, msg)

    def run(self) -> ProjectState:
        """Execute the full pipeline. Returns the updated ProjectState."""
        try:
            return self._run_stages()
        except Exception as e:
            self.project.current_stage = PipelineStage.ERROR
            self._emit("error", f"Pipeline error: {e}")
            raise

    def _run_stages(self) -> ProjectState:
        settings = self.project.settings

        # ── Stage 1: Parse document ───────────────────────────────────
        self.project.current_stage = PipelineStage.PARSING
        self._emit("info", "Stage 1: Parsing document...")
        self._progress("Parsing", 0, TOTAL_STAGES)

        handler = DocxHandler(self.project.input_docx_path)
        parser = DocumentParser(handler)
        sentences = parser.parse()
        self.project.sentences = sentences

        if self._cancelled:
            return self.project

        # ── Stage 2: Locate markers ───────────────────────────────────
        self.project.current_stage = PipelineStage.MARKER_LOCATION
        self._emit("info", "Stage 2: Locating markers...")
        self._progress("Locating markers", 1, TOTAL_STAGES)

        locator = MarkerLocator()
        sentences = locator.locate(sentences)
        marked = locator.get_marked_sentences(sentences)

        self._emit("info", f"Found {len(marked)} sentences with markers")

        if not marked:
            self._emit("info", "No markers found in document. Nothing to process.")
            self.project.current_stage = PipelineStage.COMPLETE
            return self.project

        # Domain inference (used as context for the agent)
        if settings.domain_inference:
            self.project.inferred_domains = infer_domains(sentences)
            self._emit("info", f"Inferred domains: {self.project.inferred_domains}")

        self._check_pause()
        if self._cancelled:
            return self.project

        # ── Stage 3: AI Citation Search ───────────────────────────────
        self.project.current_stage = PipelineStage.AI_CITATION_SEARCH
        self._emit("info", "Stage 3: AI-powered citation search...")
        self._progress("AI Citation Search", 2, TOTAL_STAGES)

        cache = CacheDB()
        pubmed = PubMedClient(
            email=settings.ncbi_email,
            api_key=settings.ncbi_api_key or "",
            cache_db=cache,
        )
        biorxiv = BioRxivClient(cache_db=cache, server="biorxiv")

        agent = LLMCitationAgent(
            anthropic_api_key=settings.anthropic_api_key,
            model=settings.claude_model,
            pubmed_client=pubmed,
            biorxiv_client=biorxiv,
            search_biorxiv=settings.search_biorxiv,
            max_refs=settings.max_refs_for_refs,
            log_callback=self._log,
        )

        for i, sent in enumerate(marked):
            self._check_pause()
            if self._cancelled:
                return self.project

            self._emit(
                "info",
                f"  [{i+1}/{len(marked)}] {sent.id}: {sent.clean_text[:60]}..."
            )
            evidence = agent.find_citations(
                sent, self.project.inferred_domains
            )
            self.project.evidence_map[sent.id] = evidence

            if not evidence.selected:
                self._emit("warning", f"  No citations found for {sent.id}")

        self._check_pause()
        if self._cancelled:
            return self.project

        # ── Stage 4: Global QA ────────────────────────────────────────
        self.project.current_stage = PipelineStage.GLOBAL_QA
        self._emit("info", "Stage 4: Running global QA...")
        self._progress("Global QA", 3, TOTAL_STAGES)

        qa = GlobalQA()
        self.project.evidence_map = qa.run(sentences, self.project.evidence_map)

        # Done
        self.project.current_stage = PipelineStage.COMPLETE
        self._progress("Complete", TOTAL_STAGES, TOTAL_STAGES)

        resolved = sum(1 for ev in self.project.evidence_map.values()
                      if ev.selected)
        self._emit("info",
                   f"Pipeline complete: {resolved}/{len(marked)} sentences have citations")

        return self.project
```

**Step 2: Verify import**

Run: `python -c "from src.pipeline.orchestrator import PipelineOrchestrator; print('OK')"`
Expected: `OK`

---

### Task 7: Update InputsTab — add AI Settings group

**Files:**
- Modify: `src/gui/inputs_tab.py`

**Step 1: Add AI Settings group after the NCBI group**

After the existing `api_group` section (after line 162 `layout.addWidget(api_group)`), add:

```python
        # ── AI Settings ─────────────────────────────────────────────
        ai_group = QGroupBox("AI Settings (Anthropic Claude)")
        ai_form = QFormLayout(ai_group)

        self.anthropic_key_edit = QLineEdit()
        self.anthropic_key_edit.setPlaceholderText("sk-ant-... (required for AI citation search)")
        self.anthropic_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        ai_form.addRow("Anthropic API Key:", self.anthropic_key_edit)

        self.model_combo = QComboBox()
        self.model_combo.addItems([
            "Haiku 4.5 (Fast, cheapest)",
            "Sonnet 4.5 (Balanced)",
            "Opus 4.6 (Highest quality)",
        ])
        ai_form.addRow("Claude Model:", self.model_combo)

        layout.addWidget(ai_group)
```

**Step 2: Update get_settings() to include new fields**

Add these two fields to the `ProjectSettings(...)` constructor in `get_settings()`:

```python
            anthropic_api_key=self.anthropic_key_edit.text().strip() or None,
            claude_model=self._get_model_id(),
```

**Step 3: Add the _get_model_id helper method**

Add this method to `InputsTab`:

```python
    def _get_model_id(self) -> str:
        """Map combo box index to Anthropic model ID."""
        model_map = {
            0: "claude-haiku-4-5-20251001",
            1: "claude-sonnet-4-5-20250929",
            2: "claude-opus-4-6",
        }
        return model_map.get(self.model_combo.currentIndex(), "claude-haiku-4-5-20251001")
```

**Step 4: Verify the tab loads**

Run: `python -c "from src.gui.inputs_tab import InputsTab; print('OK')"`
Expected: `OK`

---

### Task 8: Update RunTab — 4-stage indicators

**Files:**
- Modify: `src/gui/run_tab.py`

**Step 1: Update STAGE_NAMES constant**

Replace the existing `STAGE_NAMES` list (lines 99-107) with:

```python
    STAGE_NAMES = [
        "Parse Document",
        "Locate Markers",
        "AI Citation Search",
        "Global QA",
    ]
```

**Step 2: Update progress bar range**

Change line 142 from `self.progress_bar.setRange(0, 7)` to:

```python
        self.progress_bar.setRange(0, 4)
```

**Step 3: Update _on_finished to use 4**

Change line 214 from `self.progress_bar.setValue(7)` to:

```python
        self.progress_bar.setValue(4)
```

**Step 4: Verify the tab loads**

Run: `python -c "from src.gui.run_tab import RunTab; print('OK')"`
Expected: `OK`

---

### Task 9: Update MainWindow — API key validation

**Files:**
- Modify: `src/gui/main_window.py`

**Step 1: Add Anthropic API key validation**

In `_start_pipeline()`, after the NCBI email check (after line 136), add:

```python
        if not settings.anthropic_api_key:
            QMessageBox.warning(self, "API Key Required",
                              "Please enter your Anthropic API key in the AI Settings.\n"
                              "This is required for Claude-powered citation search.")
            return
```

**Step 2: Verify the window loads**

Run: `python -c "from src.gui.main_window import MainWindow; print('OK')"`
Expected: `OK` (may warn about no QApplication, that's fine)

---

### Task 10: Smoke test the full pipeline

**Step 1: Verify full import chain**

Run:
```bash
python -c "
from src.models.project import ProjectState, ProjectSettings, PipelineStage
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.llm_citation_agent import LLMCitationAgent
print('PipelineStage.AI_CITATION_SEARCH:', PipelineStage.AI_CITATION_SEARCH)
s = ProjectSettings()
print('Model:', s.claude_model)
print('All imports OK')
"
```
Expected: Prints stage value, model name, and "All imports OK"

**Step 2: Verify tool definitions are valid**

Run:
```bash
python -c "
from src.pipeline.llm_citation_agent import TOOLS, BIORXIV_TOOL
import json
for t in TOOLS:
    print(f'Tool: {t[\"name\"]} - schema valid: {bool(t[\"input_schema\"])}')
print(f'Tool: {BIORXIV_TOOL[\"name\"]} - schema valid: {bool(BIORXIV_TOOL[\"input_schema\"])}')
print('All tools valid')
"
```
Expected: Lists all 3 tools as valid

---

## Summary of Changes

| File | Action | Description |
|------|--------|-------------|
| `requirements.txt` | Modify | Add `anthropic>=0.42.0` |
| `src/models/project.py` | Modify | Add `anthropic_api_key`, `claude_model`, `AI_CITATION_SEARCH` stage |
| `src/pipeline/llm_citation_agent.py` | Create | Full LLMCitationAgent with tool definitions, agent loop, response parsing |
| `src/pipeline/orchestrator.py` | Rewrite | 4-stage pipeline using LLMCitationAgent |
| `src/gui/inputs_tab.py` | Modify | Add AI Settings group (API key, model dropdown) |
| `src/gui/run_tab.py` | Modify | 4-stage indicators instead of 7 |
| `src/gui/main_window.py` | Modify | API key validation |

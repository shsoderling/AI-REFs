# LLM Citation Agent Design

**Date:** 2026-02-15
**Status:** Approved

## Goal

Replace the deterministic keyword-based citation pipeline (stages 3-6) with a Claude tool-use agent that intelligently searches PubMed, evaluates abstracts, and selects the best supporting citations for each (REF)/(REFS) marker — mirroring how Claude Chat natively searches PubMed.

## Architecture

### Pipeline Changes

```
Current (7 stages):              New (4 stages):
1. DocumentParser                1. DocumentParser (unchanged)
2. MarkerLocator                 2. MarkerLocator (unchanged)
3. ClaimExtractor       ─┐
4. CandidateRetriever    ├──►   3. LLMCitationAgent (NEW)
5. Ranker                │         Claude tool-use loop per sentence
6. Verifier             ─┘         Uses PubMed/bioRxiv as tools
7. GlobalQA                      4. GlobalQA (unchanged)
```

Stages 1, 2, and GlobalQA are unchanged. The review tab, export, and project save/load see no changes — they consume `EvidenceRecord` objects regardless of how they were produced.

### LLMCitationAgent

A new class at `src/pipeline/llm_citation_agent.py` using the `anthropic` Python SDK with tool-use.

**Tools defined for Claude:**

1. `search_pubmed(query, max_results)` — wraps `PubMedClient.search()`, returns PMIDs + total count
2. `fetch_articles(pmids)` — wraps `PubMedClient.fetch_articles()`, returns full metadata (title, authors, year, journal, abstract, DOI, MeSH)
3. `search_biorxiv(keywords, category)` — wraps `BioRxivClient.search_by_keywords()`, returns matching preprints. Only registered if bioRxiv search is enabled.

**System prompt** instructs Claude to:
- Act as a citation-finding assistant for biomedical research
- For (REF) find 1 best citation, for (REFS) find N (configurable)
- Search iteratively — start specific, broaden if needed
- Verify each selection by checking the abstract supports the claim
- Return structured JSON with selections, confidence, rationale, and snippets

**Agent loop per sentence:**
1. Send claim sentence + marker type + domain context
2. Claude responds with tool calls or final JSON
3. Execute tool calls via existing clients, return results
4. Repeat until final JSON (max 10 rounds)
5. Parse JSON into `EvidenceRecord`

### Model Selection

User chooses from three models in settings:
- Haiku 4.5 (`claude-haiku-4-5-20251001`) — default, fast, cheapest
- Sonnet 4.5 (`claude-sonnet-4-5-20250929`) — better reasoning
- Opus 4.6 (`claude-opus-4-6`) — highest quality

### Data Flow

Claude's structured JSON maps directly to existing `EvidenceRecord` fields:

```
Claude returns:                    Maps to EvidenceRecord:
  selected[].pmid           →     .selected (fetched CitationCandidates)
  confidence                →     .confidence_level
  confidence_score          →     .confidence_score
  confidence_rationale      →     .confidence_rationale
  verification_status       →     .verification_status
  supporting_snippets       →     .abstract_snippets
  search_queries_used       →     .search_query (joined with " | ")
  all_candidates_considered →     .candidates
```

## GUI Changes

### InputsTab

New "AI Settings" group:
- Anthropic API Key — password-masked text field (required)
- Model — dropdown: Haiku 4.5 / Sonnet 4.5 / Opus 4.6

### RunTab

Stage indicators: 4 stages instead of 7:
1. Parse Document
2. Locate Markers
3. AI Citation Search
4. Global QA

Log panel shows per-sentence Claude activity (queries run, candidates evaluated, reasoning).

### MainWindow

Pipeline start validates both NCBI email AND Anthropic API key.

## ProjectSettings Changes

Two new fields:
- `anthropic_api_key: Optional[str]`
- `claude_model: str = "claude-haiku-4-5-20251001"`

## Error Handling

- **Auth/model errors:** Pipeline stops immediately with clear error message
- **PubMed tool failures:** Error string returned to Claude so it can retry
- **10-round limit:** Take whatever candidates found, mark confidence LOW
- **Malformed JSON:** Log raw response, mark sentence UNRESOLVED
- **No results:** `retrieval_error = "AI agent found no supporting citations"`
- **Timeouts:** 60s per Anthropic call, 30s per PubMed call
- **Rate limiting:** PubMed handled by existing `_rate_limit()`, Anthropic SDK handles its own

## Files

**New:**
- `src/pipeline/llm_citation_agent.py`

**Modified:**
- `src/models/project.py` — new settings fields
- `src/pipeline/orchestrator.py` — replace stages 3-6 with agent loop
- `src/gui/inputs_tab.py` — AI Settings group
- `src/gui/run_tab.py` — 4 stage indicators
- `src/gui/main_window.py` — API key validation
- `requirements.txt` — add `anthropic>=0.42.0`

**Unchanged:**
- `src/services/pubmed_client.py`, `biorxiv_client.py` — used as tool backends
- `src/models/citation.py`, `sentence.py`, `evidence.py`
- `src/gui/review_tab.py`
- `src/storage/`
- `src/pipeline/claim_extractor.py`, `ranker.py`, `verifier.py` — left in place, no longer called

## Dependencies

- `anthropic>=0.42.0` added to `requirements.txt`

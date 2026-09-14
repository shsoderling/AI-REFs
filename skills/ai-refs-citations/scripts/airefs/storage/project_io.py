"""Save and load AI REFs project files (.airefsproj)."""

import json
import logging
from pathlib import Path

from ..models.project import PROJECT_SCHEMA_VERSION, ProjectState

logger = logging.getLogger(__name__)


SENSITIVE_SETTINGS_FIELDS = {"anthropic_api_key", "ncbi_api_key"}


def save_project(project: ProjectState, path: str):
    """Save a ProjectState to a JSON-based .airefsproj file.

    API keys are excluded from the saved file to prevent accidental
    leakage when sharing project files.  Keys are re-entered via the
    Input tab each session (or loaded from environment variables).
    """
    project.project_path = path
    data = project.model_dump(
        mode="json",
        exclude={"settings": SENSITIVE_SETTINGS_FIELDS},
    )
    Path(path).write_text(json.dumps(data, indent=2))
    logger.info(f"Project saved to {path}")


_DROPPED_V1_KEYS = ("bibliography_pmids", "pmid_to_bib_number")


def upgrade_project_data(data: dict) -> dict:
    """Bring a project file's JSON up to the current schema.

    v1 files (no ``schema_version``) lose the two bibliography maps that
    were never read; the document's own citation fields replaced them. A
    file written by a newer AI REFs is refused rather than misread.
    """
    version = int(data.get("schema_version") or 1)
    if version > PROJECT_SCHEMA_VERSION:
        raise ValueError(
            f"This project was saved by a newer AI REFs (schema v{version}); "
            f"this version reads up to v{PROJECT_SCHEMA_VERSION}.")
    if version < 2:
        for key in _DROPPED_V1_KEYS:
            data.pop(key, None)
        data["schema_version"] = 2
    return data


def load_project(path: str) -> ProjectState:
    """Load a ProjectState from a .airefsproj file (upgrading older files)."""
    data = upgrade_project_data(json.loads(Path(path).read_text()))
    project = ProjectState.model_validate(data)
    project.project_path = path
    logger.info(f"Project loaded from {path}")
    return project

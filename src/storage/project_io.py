"""Save and load AI REFs project files (.airefsproj)."""

import json
import logging
from pathlib import Path

from ..models.project import ProjectState

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


def load_project(path: str) -> ProjectState:
    """Load a ProjectState from a .airefsproj file."""
    data = json.loads(Path(path).read_text())
    project = ProjectState.model_validate(data)
    project.project_path = path
    logger.info(f"Project loaded from {path}")
    return project

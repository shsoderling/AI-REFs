"""AI REFs — Entry point for the desktop application."""

import sys
import os
import logging

# When running as a frozen PyInstaller bundle, the 'src' package isn't on
# sys.path and relative imports fail.  Fix that before importing anything
# from the project.
if getattr(sys, 'frozen', False):
    # PyInstaller sets _MEIPASS to the temp extraction dir.
    # The spec file adds the project root to `pathex`, so 'src/' lives
    # alongside the executable's internal modules.
    _bundle_dir = sys._MEIPASS          # type: ignore[attr-defined]
    if _bundle_dir not in sys.path:
        sys.path.insert(0, _bundle_dir)

from PySide6.QtWidgets import QApplication

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("AI REFs")

    # Use absolute import so it works both as `python -m src.app`
    # and inside a PyInstaller frozen bundle.
    from src.gui.main_window import MainWindow
    window = MainWindow()
    window.show()

    sys.exit(app.exec())


# Allow `python -m src.app` and direct `python src/app.py` to work
if __name__ == "__main__":
    main()

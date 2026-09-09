"""Dialog showing the merged final citation numbering for insert mode."""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
    QHeaderView, QDialogButtonBox,
)
from PySide6.QtGui import QColor
from PySide6.QtCore import Qt

from ..pipeline.renumbering import RenumberingResult


class RenumberPreviewDialog(QDialog):
    """Read-only table of how citations will be numbered after merge."""

    def __init__(self, result: RenumberingResult, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Preview Final Numbering")
        self.resize(720, 480)
        self._build_ui(result)

    def _build_ui(self, result: RenumberingResult):
        layout = QVBoxLayout(self)

        n_new = sum(1 for a in result.assignments.values() if a.is_new)
        n_existing = len(result.assignments) - n_new
        n_changed = sum(1 for o, n in result.renumber_map.items() if o != n)
        layout.addWidget(QLabel(
            f"{len(result.assignments)} references after merge — "
            f"{n_new} new, {n_existing} existing ({n_changed} renumbered)."
        ))

        table = QTableWidget(len(result.assignments), 4)
        table.setHorizontalHeaderLabels(
            ["Final #", "Source", "Old #", "Reference"])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch)

        for row, num in enumerate(sorted(result.assignments.keys())):
            assn = result.assignments[num]
            if assn.is_new:
                source, old, ref = "NEW", "", _candidate_label(assn.candidate)
            else:
                source = "existing"
                old = str(assn.original_number)
                ref = assn.bib_key

            renumbered = (not assn.is_new
                          and result.renumber_map.get(assn.original_number) != assn.original_number)

            cells = [str(num), source, old, ref]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if assn.is_new:
                    item.setBackground(QColor("#e8f5e9"))      # green-ish: new
                elif renumbered:
                    item.setBackground(QColor("#fff3cd"))      # amber: moved
                if col == 0:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                table.setItem(row, col, item)

        layout.addWidget(table)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


def _candidate_label(candidate) -> str:
    if not candidate:
        return "(unknown)"
    bits = [candidate.title or "(untitled)"]
    if candidate.first_author_year:
        bits.append(f"— {candidate.first_author_year}")
    return " ".join(bits)

"""The dialog that lists the raster files of a band folder: the user ticks the files that are
bands and can correct the band of each. PPE then imports only the ticked files."""

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

BAND_CHOICES = (
    ("", "— not a band —"),
    ("blue", "Blue"),
    ("green", "Green"),
    ("red", "Red"),
    ("re", "Red edge"),
    ("nir", "NIR"),
)
BAND_NAMES = dict(BAND_CHOICES)

# The dialog styles every part itself. It inherits the panel's dark text colour, so leaving the
# table to the system palette made it dark text on a dark table when macOS is in dark mode.
DIALOG_STYLE = """
    QDialog#ppeBandFiles { background: #f3f6f4; }
    QDialog#ppeBandFiles QLabel { color: #1f3a30; background: transparent; }
    QTableWidget {
        color: #172d25; background: #ffffff; alternate-background-color: #f4f8f6;
        border: 1px solid #c9d6cf; border-radius: 6px; gridline-color: #e3eae6;
    }
    QTableWidget::item { color: #172d25; padding: 2px 8px; border: none; }
    QHeaderView::section {
        color: #17473a; background: #e7f0eb; font-weight: 700; padding: 5px 8px;
        border: none; border-bottom: 1px solid #c9d6cf; border-right: 1px solid #d9e3dd;
    }
    QTableCornerButton::section { background: #e7f0eb; border: none; }
    QScrollBar:horizontal { background: #eef3f0; height: 12px; margin: 0px; border: none; }
    QScrollBar::handle:horizontal { background: #a9bfb3; min-width: 40px; border-radius: 5px; margin: 2px; }
    QScrollBar:vertical { background: #eef3f0; width: 12px; margin: 0px; border: none; }
    QScrollBar::handle:vertical { background: #a9bfb3; min-height: 30px; border-radius: 5px; margin: 2px; }
    QScrollBar::add-line, QScrollBar::sub-line { width: 0px; height: 0px; border: none; background: none; }
    QScrollBar::add-page, QScrollBar::sub-page { background: none; }
    QToolTip { color: #172d25; background: #ffffff; border: 1px solid #b9c9c0; padding: 4px; }
    QPushButton {
        color: #17473a; background: #ffffff; border: 1px solid #b9c9c0; border-radius: 6px;
        min-height: 28px; padding: 0px 14px; font-weight: 600;
    }
    QPushButton:hover { border-color: #2b8560; }
    QPushButton:default, QPushButton[ppePrimary="true"] { color: #ffffff; background: #1d7550; border-color: #1d7550; }
    QPushButton:disabled { color: #8a9a92; background: #eef2f0; border-color: #d3ddd8; }
"""


class BandFilesDialog(QDialog):
    """scan: the result of native_engine.scan_band_folder(folder)."""

    def __init__(self, folder, scan, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Band files in this folder")
        self.setObjectName("ppeBandFiles")
        self.setStyleSheet(DIALOG_STYLE)
        self.setSizeGripEnabled(True)
        self.setMinimumWidth(640)
        self.rows = []
        layout = QVBoxLayout(self)
        intro = QLabel(
            "PPE found these raster files in <b>{}</b>. Tick the files that are bands of the image and check "
            "the band of each; PPE imports only the ticked files. Files PPE does not recognise, thermal "
            "files and stacked files start unticked.".format(os.path.basename(folder.rstrip("/\\")) or folder)
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        table = QTableWidget(len(scan["files"]), 4)
        table.setHorizontalHeaderLabels(["Use", "File", "Band", "Note"])
        table.verticalHeader().setVisible(False)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        # Full file names: columns take the width of their text and the table scrolls sideways
        # instead of cutting names short with "...".
        table.setWordWrap(False)
        table.setTextElideMode(Qt.TextElideMode.ElideNone)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        for i, entry in enumerate(scan["files"]):
            holder = QWidget()
            line = QHBoxLayout(holder)
            line.setContentsMargins(6, 0, 6, 0)
            line.setAlignment(Qt.AlignmentFlag.AlignCenter)
            check = QCheckBox()
            check.setChecked(bool(entry["use"]))
            line.addWidget(check)
            readable = entry["size"] is not None
            check.setEnabled(readable)
            name = QTableWidgetItem(entry["file"])
            size = "{} x {} pixels".format(*entry["size"]) if entry["size"] else "not readable"
            name.setToolTip("{}\n{}".format(entry["path"], size))
            combo = QComboBox()
            for key, label in BAND_CHOICES:
                combo.addItem(label, key)
            combo.setCurrentIndex(max(0, combo.findData(entry["band"] or "")))
            combo.setEnabled(readable)
            note = QTableWidgetItem(entry["note"])
            note.setToolTip(entry["note"])
            table.setCellWidget(i, 0, holder)
            table.setItem(i, 1, name)
            table.setCellWidget(i, 2, combo)
            table.setItem(i, 3, note)
            row = {"entry": entry, "check": check, "combo": combo}
            self.rows.append(row)
            check.toggled.connect(self._validate)
            combo.currentIndexChanged.connect(lambda _index, r=row: self._band_changed(r))
        # Rows as tall as the band box, so the boxes do not overlap the next row.
        row_height = max([combo.sizeHint().height() for combo in (r["combo"] for r in self.rows)] + [28]) + 8
        table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        table.verticalHeader().setDefaultSectionSize(row_height)
        for i in range(table.rowCount()):
            table.setRowHeight(i, row_height)
        layout.addWidget(table)
        self.table = table
        if scan.get("other_files"):
            other = QLabel(
                "{} other file(s) in the folder are not rasters (for example .aux.xml or plot files) and are "
                "not listed.".format(scan["other_files"])
            )
            other.setWordWrap(True)
            other.setStyleSheet("color: #5f6f68;")
            layout.addWidget(other)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Use the ticked files")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setProperty("ppePrimary", True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        # Wide enough for the longest file name when the screen allows; otherwise the table scrolls.
        wanted = table.verticalHeader().width() + sum(table.columnWidth(c) for c in range(4)) + 60
        screen = self.screen().availableGeometry().width() if hasattr(self, "screen") and self.screen() else 1400
        self.resize(max(760, min(wanted, int(screen * 0.85))), min(680, 250 + row_height * max(3, len(self.rows))))
        self._validate()

    def _band_changed(self, row):
        if row["combo"].currentData() and not row["check"].isChecked():
            row["check"].setChecked(True)  # choosing a band means "use this file"
        self._validate()

    def chosen(self):
        """{band: path} of the ticked files."""
        return {
            row["combo"].currentData(): row["entry"]["path"]
            for row in self.rows
            if row["check"].isChecked() and row["combo"].currentData()
        }

    def _problem(self):
        ticked = [row for row in self.rows if row["check"].isChecked()]
        if not ticked:
            return "Tick at least one band file."
        missing = [row["entry"]["file"] for row in ticked if not row["combo"].currentData()]
        if missing:
            return "Choose the band of {}, or untick it.".format(", ".join(missing))
        bands = [row["combo"].currentData() for row in ticked]
        double = sorted({b for b in bands if bands.count(b) > 1})
        if double:
            return "More than one ticked file is {}: keep one.".format(" and ".join(BAND_NAMES[b] for b in double))
        sizes = {row["entry"]["size"] for row in ticked}
        if len(sizes) > 1:
            return "The ticked files are not the same size ({}); band files must share one pixel grid.".format(
                ", ".join("{} x {}".format(*s) for s in sorted(sizes))
            )
        return ""

    def _validate(self, *_ignored):
        problem = self._problem()
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(not problem)
        if problem:
            self.status.setText("⚠ " + problem)
            self.status.setStyleSheet("color: #a33a1f;")
        else:
            order = ("blue", "green", "red", "re", "nir")
            chosen = self.chosen()
            self.status.setText(
                "PPE will import {} file(s): {}.".format(
                    len(chosen), ", ".join(BAND_NAMES[b] for b in order if b in chosen)
                )
            )
            self.status.setStyleSheet("color: #17473a;")

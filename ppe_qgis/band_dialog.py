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

BAND_CHOICES = (("", "— not a band —"), ("blue", "Blue"), ("green", "Green"), ("red", "Red"),
                ("re", "Red edge"), ("nir", "NIR"))
BAND_NAMES = dict(BAND_CHOICES)


class BandFilesDialog(QDialog):
    """scan: the result of native_engine.scan_band_folder(folder)."""

    def __init__(self, folder, scan, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Band files in this folder")
        self.setMinimumWidth(640)
        self.rows = []
        layout = QVBoxLayout(self)
        intro = QLabel("PPE found these raster files in <b>{}</b>. Tick the files that are bands of the image and check "
                       "the band of each; PPE imports only the ticked files. Files PPE does not recognise, thermal "
                       "files and stacked files start unticked.".format(os.path.basename(folder.rstrip("/\\")) or folder))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        table = QTableWidget(len(scan["files"]), 4)
        table.setHorizontalHeaderLabels(["Use", "File", "Band", "Note"])
        table.verticalHeader().setVisible(False)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        for i, entry in enumerate(scan["files"]):
            holder = QWidget(); line = QHBoxLayout(holder); line.setContentsMargins(6, 0, 6, 0); line.setAlignment(Qt.AlignCenter)
            check = QCheckBox(); check.setChecked(bool(entry["use"])); line.addWidget(check)
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
            note = QTableWidgetItem(entry["note"]); note.setToolTip(entry["note"])
            table.setCellWidget(i, 0, holder); table.setItem(i, 1, name); table.setCellWidget(i, 2, combo); table.setItem(i, 3, note)
            row = {"entry": entry, "check": check, "combo": combo}
            self.rows.append(row)
            check.toggled.connect(self._validate)
            combo.currentIndexChanged.connect(lambda _index, r=row: self._band_changed(r))
        table.resizeRowsToContents()
        layout.addWidget(table)
        self.table = table
        if scan.get("other_files"):
            other = QLabel("{} other file(s) in the folder are not rasters (for example .aux.xml or plot files) and are "
                           "not listed.".format(scan["other_files"]))
            other.setWordWrap(True); other.setStyleSheet("color: #5f6f68;")
            layout.addWidget(other)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Use the ticked files")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.resize(760, min(620, 230 + 34 * max(3, len(self.rows))))
        self._validate()

    def _band_changed(self, row):
        if row["combo"].currentData() and not row["check"].isChecked():
            row["check"].setChecked(True)          # choosing a band means "use this file"
        self._validate()

    def chosen(self):
        """{band: path} of the ticked files."""
        return {row["combo"].currentData(): row["entry"]["path"] for row in self.rows
                if row["check"].isChecked() and row["combo"].currentData()}

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
                ", ".join("{} x {}".format(*s) for s in sorted(sizes)))
        return ""

    def _validate(self, *_ignored):
        problem = self._problem()
        ok = self.buttons.button(QDialogButtonBox.Ok)
        ok.setEnabled(not problem)
        if problem:
            self.status.setText("⚠ " + problem)
            self.status.setStyleSheet("color: #a33a1f;")
        else:
            order = ("blue", "green", "red", "re", "nir")
            chosen = self.chosen()
            self.status.setText("PPE will import {} file(s): {}.".format(
                len(chosen), ", ".join(BAND_NAMES[b] for b in order if b in chosen)))
            self.status.setStyleSheet("color: #17473a;")

"""Plant Pixel Extractor dock interface."""

import logging
import json
import os
from statistics import median

from qgis.PyQt.QtCore import QEvent, QObject, QSettings, QStandardPaths, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QBrush, QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qgis.core import Qgis, QgsProject
from osgeo import gdal

from .curve_widget import ThresholdCurveDialog
from .band_dialog import BandFilesDialog
from .plant_parts import likely_parts
from .native_engine import finding_shares
from .crop_tools import PolygonDrawTool, show_crop_areas, to_crs
from .layer_utils import FINDING_COLOURS, canopy_at, load_plot_layer, plot_layer, remove_role, set_plot_labels
from .native_engine import (BAND_LABELS, PLOT_FILE_FILTER, dataset_wavelengths, describe_image, display_channels,
                            guess_plot_id_field, list_batch_images, open_plot_file, plots_over_image, scan_band_folder)

_LOG = logging.getLogger(__name__)    # errors PPE deliberately ignores are kept here (debug level)


class _TextHeightKeeper(QObject):
    """Event filter: on resize (and when asked) set a word-wrapped label's minimum height to the
    height its text needs at its width."""
    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Resize, QEvent.Show):
            self.fit(obj)
        return False

    @staticmethod
    def fit(label):
        width = label.width() if label.width() > 20 else max(120, label.sizeHint().width())
        # measured from the text alone (heightForWidth would otherwise include the minimum set before)
        margins = label.contentsMargins()
        inner = max(1, width - margins.left() - margins.right())
        rect = label.fontMetrics().boundingRect(0, 0, inner, 100000, int(Qt.TextWordWrap), label.text())
        needed = rect.height() + margins.top() + margins.bottom() + 2
        if label.minimumHeight() != needed:
            label.setMinimumHeight(needed)


class HSIWavelengthDialog(QDialog):
    """Collect and validate the wavelength grid for an unnamed HSI cube."""

    def __init__(self, band_count, minimum=0.0, maximum=0.0, interval=0.0, parent=None):
        super().__init__(parent)
        self.band_count = int(band_count)
        self.setWindowTitle("Hyperspectral wavelength grid")
        self.setMinimumWidth(410)
        layout = QVBoxLayout(self)
        note = QLabel(
            "This cube has {} bands. Enter the wavelength of the first and last band and "
            "the constant band interval. PPE will build blue, green, red, red-edge, and "
            "NIR proxy bands before running the usual ensemble.".format(self.band_count)
        )
        note.setWordWrap(True); layout.addWidget(note)
        form = QFormLayout(); layout.addLayout(form)
        self.minimum = QDoubleSpinBox(); self.minimum.setRange(300, 2500); self.minimum.setDecimals(3); self.minimum.setSuffix(" nm")
        self.maximum = QDoubleSpinBox(); self.maximum.setRange(300, 2500); self.maximum.setDecimals(3); self.maximum.setSuffix(" nm")
        self.interval = QDoubleSpinBox(); self.interval.setRange(.001, 100); self.interval.setDecimals(4); self.interval.setSuffix(" nm")
        self.minimum.setValue(minimum if minimum >= 300 else 400)
        self.interval.setValue(interval if interval > 0 else 5)
        inferred_max = self.minimum.value() + max(0, self.band_count - 1) * self.interval.value()
        self.maximum.setValue(maximum if maximum > minimum else min(2500, inferred_max))
        form.addRow("Minimum wavelength", self.minimum)
        form.addRow("Maximum wavelength", self.maximum)
        form.addRow("Band interval", self.interval)
        self.check_label = QLabel(); self.check_label.setWordWrap(True); layout.addWidget(self.check_label)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._validate_and_accept); self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        for field in (self.minimum, self.maximum, self.interval): field.valueChanged.connect(self._update_check)
        self._update_check()

    def _update_check(self):
        expected = self.minimum.value() + max(0, self.band_count - 1) * self.interval.value()
        delta = abs(expected - self.maximum.value())
        self.check_label.setText("Grid check: calculated last band {:.3f} nm · difference {:.3f} nm".format(expected, delta))

    def _validate_and_accept(self):
        expected = self.minimum.value() + max(0, self.band_count - 1) * self.interval.value()
        tolerance = max(.1, self.interval.value() * .5)
        if self.maximum.value() <= self.minimum.value() or abs(expected - self.maximum.value()) > tolerance:
            QMessageBox.warning(self, "Invalid wavelength grid", "Minimum + (band count − 1) × interval must match the maximum wavelength.\n\nCalculated last band: {:.3f} nm".format(expected))
            return
        self.accept()


class PPEDockWidget(QDockWidget):
    generate_requested = pyqtSignal(dict)
    apply_requested = pyqtSignal(dict)
    export_requested = pyqtSignal(dict)
    cancel_requested = pyqtSignal()
    preview_requested = pyqtSignal(dict)
    reset_requested = pyqtSignal()
    zones_requested = pyqtSignal(dict)      # prepare the crop areas (outlines -> crop raster + preview)
    zones_cleared = pyqtSignal()            # back to one threshold for the whole image
    crop_preview_changed = pyqtSignal()     # a crop slider moved
    pale_layer_toggled = pyqtSignal(bool)   # show / hide the pale-plant layer on the map
    batch_run_requested = pyqtSignal(dict)       # run every image of a folder
    batch_finalize_requested = pyqtSignal(dict)  # write every mask of a batch at its own threshold
    batch_open_requested = pyqtSignal(dict)      # open one image of a batch for review
    count_requested = pyqtSignal(dict)           # count plants in the finalized mask(s)
    training_requested = pyqtSignal(dict)        # cut training tiles + PPE labels
    labels_requested = pyqtSignal(dict)          # create label points / write buffered labels (4.6)
    closing = pyqtSignal()

    def __init__(self, iface, parent=None):
        super().__init__("Plant Pixel Extractor", parent or iface.mainWindow())
        self.iface = iface
        self.settings = QSettings()
        self.curve_points = []
        self.result = {}
        self.hsi_grid_confirmed = False
        self.curve_dialog = None
        self._preview_key = None
        self._busy = False
        self.crop_rows = []
        self.zone_info = None
        self._draw_tool = None
        self._drawing_row = None
        self._previous_tool = None
        self.folder_bands = None
        self._folder_bands_for = None
        self._band_dialog = None
        self.batch = None              # {"manifest": ..., "choices": {name: {...}}, "open": name or None}
        self.setObjectName("PPEDockWidget")
        self.setMinimumWidth(390)
        self._build_ui()
        self._restore_settings()
        self._connect_ui()

    def _build_ui(self):
        host = QWidget()
        root = QVBoxLayout(host)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        header = QFrame()
        header.setObjectName("ppeHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(14, 10, 14, 10)
        header_layout.setSpacing(2)
        title = QLabel("Plant Pixel Extractor")
        title.setObjectName("ppeTitle")
        subtitle = QLabel("Separate plant pixels from soil in drone imagery")
        subtitle.setObjectName("ppeSubtitle")
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)
        goal_row = QHBoxLayout(); goal_row.setContentsMargins(0, 6, 0, 0); goal_row.setSpacing(8)
        goal_label = QLabel("Goal"); goal_label.setObjectName("ppeSubtitle")
        self.goal = QComboBox()
        for text, key in (("Plant mask", "mask"), ("Plant counter", "count"), ("Training data for a model", "training"),
                          ("Create labels", "labels")):
            self.goal.addItem(text, key)
        self.goal.setToolTip("Plant mask: plant pixels, plant-only bands and plot statistics.\n"
                             "Plant counter: count plants (early season) per image and per plot.\n"
                             "Training data: image tiles with PPE labels to train a fast model, no hand labelling.\n"
                             "Create labels: plant and soil label points (and buffers) for your own machine-learning model.\n"
                             "Every goal starts with the same steps: image, run, review.")
        goal_row.addWidget(goal_label); goal_row.addWidget(self.goal, 1)
        header_layout.addLayout(goal_row)
        root.addWidget(header)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setUsesScrollButtons(False)       # five short labels always fit; no hidden tabs
        self.tabs.tabBar().setExpanding(True)
        # Every page is built once; the goal decides which are shown (and their numbers).
        self._pages = {
            "image": (self._input_tab(), "Image", "Choose the image and check its bands"),
            "run": (self._generate_tab(), "Run", "Build the plant vote"),
            "review": (self._review_tab(), "Review", "Set the threshold and finalize the mask"),
            "export": (self._export_tab(), "Export", "Export plant-only bands and indices"),
            "plots": (self._aoi_export_tab(), "Plots", "Statistics for each plot polygon"),
            "count": (self._count_tab(), "Count", "Count the plants in the finalized mask, per image and per plot"),
            "training": (self._training_tab(), "Train data", "Image tiles with PPE labels for training a fast model"),
            "labels": (self._labels_tab(), "Labels", "Plant and soil label points for a machine-learning model"),
            "batch": (self._batch_tab(), "Batch", "Many images at once: run a folder, review each, finalize all"),
        }
        root.addWidget(self.tabs, 1)
        self.setWidget(host)
        # The sheet goes on the content widget. Set on the QDockWidget itself it has no effect
        # inside QGIS (the main window carries its own sheet), and the panel shows unstyled.
        self._apply_goal("mask")
        host.setObjectName("ppeHost")
        host.setStyleSheet(self._stylesheet())
        self.setStyleSheet(self._stylesheet())

    def _card(self, title=None):
        """A white rounded box with an optional title. Returns (card, body); put the layout on body."""
        card = QFrame()
        card.setObjectName("card")
        outer = QVBoxLayout(card)
        outer.setContentsMargins(12, 10, 12, 12)
        outer.setSpacing(8)
        if title:
            heading = QLabel(title)
            heading.setObjectName("cardTitle")
            outer.addWidget(heading)
        body = QWidget()
        body.setObjectName("cardBody")
        outer.addWidget(body)
        return card, body

    def _form(self, parent):
        """Labels above their fields: nothing can misalign and every field gets the full width."""
        form = QFormLayout(parent)
        form.setContentsMargins(0, 0, 0, 0)
        form.setRowWrapPolicy(QFormLayout.WrapAllRows)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignLeft)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)
        return form

    def _keep_text_height(self, label):
        """A word-wrapped label in a form layout keeps the height Qt first gave it: when its text or the
        panel width changes it can end up shorter than one line (the clipped green box after an image
        was removed). This keeps its minimum height at what the text needs at its current width."""
        if not hasattr(self, "_height_keeper"):
            self._height_keeper = _TextHeightKeeper(self)
        label.installEventFilter(self._height_keeper)
        self._height_keeper.fit(label)

    def _note(self, label, kind="note"):
        """Put a message label in a box that grows with its text. (Padding set on a word-wrapped
        QLabel itself makes Qt miscount its height, which cut off the first and last lines.)"""
        frame = QFrame()
        frame.setObjectName("accentBox" if kind == "accent" else "noteBox")
        box = QVBoxLayout(frame)
        box.setContentsMargins(12, 10, 12, 10)
        label.setObjectName("accentText" if kind == "accent" else "noteText")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setMinimumWidth(1)          # a long word (a file path) must never make the panel wider
        box.addWidget(label)
        self._keep_text_height(label)
        return frame

    @staticmethod
    def _plain_spin(spin):
        spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        spin.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        return spin

    def _page(self):
        page = QWidget()
        page.setObjectName("ppePage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 12, 10, 12)
        layout.setSpacing(10)
        return page, layout

    def _scroll_tab(self, content):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content.setMinimumWidth(0)
        # Long combo entries and labels must never force the page wider than the dock.
        for combo in content.findChildren(QComboBox):
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(10)
        scroll.setWidget(content)
        scroll.viewport().setObjectName("ppeViewport")
        return scroll

    def _collapsible(self, title, body, expanded=False):
        """A section that opens and closes from its header."""
        holder = QWidget()
        outer = QVBoxLayout(holder)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        toggle = QToolButton()
        toggle.setObjectName("sectionToggle")
        toggle.setText(title)
        toggle.setCheckable(True)
        toggle.setChecked(expanded)
        toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        toggle.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        def _switch(opened):
            toggle.setArrowType(Qt.DownArrow if opened else Qt.RightArrow)

        toggle.toggled.connect(_switch)
        frame = QFrame()
        frame.setObjectName("card")
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(12, 10, 12, 12)
        inner.addWidget(body)
        frame.setVisible(expanded)
        body.setVisible(True)
        toggle.toggled.connect(frame.setVisible)
        outer.addWidget(toggle)
        outer.addWidget(frame)
        return holder

    def _fit_source_stack(self, *_args):
        """Make the source box as tall as the page shown, not as tall as the biggest page: only
        the current page asks for space; the others are ignored by the layout."""
        current = self.source_stack.currentWidget()
        for index in range(self.source_stack.count()):
            page = self.source_stack.widget(index)
            policy = QSizePolicy.Preferred if page is current else QSizePolicy.Ignored
            page.setSizePolicy(QSizePolicy.Preferred, policy)
        self.source_stack.setMinimumHeight(0)
        self.source_stack.setMaximumHeight(16777215)
        if current is not None:
            current.adjustSize()
        self.source_stack.adjustSize()

    def _path_row(self, line_edit, directory=False, executable=False, removable=False):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        button = QPushButton("Browse…")
        button.setProperty("secondary", True)
        button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        line_edit.textChanged.connect(line_edit.setToolTip)
        if directory:
            button.clicked.connect(lambda: self._browse_directory(line_edit))
        else:
            button.clicked.connect(lambda: self._browse_file(line_edit, executable))
        layout.addWidget(line_edit, 1)
        layout.addWidget(button)
        if removable:
            remove = QPushButton("✕")
            remove.setObjectName("removeButton")
            remove.setToolTip("Remove this image from PPE and from the map")
            remove.setFixedWidth(32)
            remove.setEnabled(bool(line_edit.text().strip()))
            line_edit.textChanged.connect(lambda text, b=remove: b.setEnabled(bool(text.strip())))
            remove.clicked.connect(lambda: self._remove_source(line_edit))
            layout.addWidget(remove)
        return row

    def _input_tab(self):
        page, layout = self._page()

        intro = QLabel("Choose the image. It appears on the map straight away; check the bands PPE found.")
        intro.setWordWrap(True)
        intro.setObjectName("ppeHint")
        layout.addWidget(intro)

        source_box, source_body = self._card("Imagery source")
        source_layout = QVBoxLayout(source_body)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.setSpacing(8)
        self.source_mode = QComboBox()
        self.source_mode.addItem("Stacked GeoTIFF", "stacked")
        self.source_mode.addItem("Folder containing bands", "folder")
        self.source_mode.addItem("Individual band files", "bands")
        source_layout.addWidget(self.source_mode)

        self.source_stack = QStackedWidget()
        stacked_page = QWidget()
        stacked_form = self._form(stacked_page)
        self.stacked_path = QLineEdit()
        self.stacked_path.setPlaceholderText("Select a multiband .tif/.tiff")
        stacked_form.addRow("Stacked raster", self._path_row(self.stacked_path, removable=True))

        folder_page = QWidget()
        folder_form = self._form(folder_page)
        self.folder_path = QLineEdit()
        self.folder_path.setPlaceholderText("Folder with blue/green/red/RE/NIR files")
        folder_form.addRow("Band folder", self._path_row(self.folder_path, directory=True, removable=True))
        self.folder_files_note = QLabel("When you choose a folder, PPE lists its raster files so you can tick the band files.")
        self.folder_files_note.setWordWrap(True); self.folder_files_note.setObjectName("ppeHint")
        self.folder_files_button = QPushButton("Choose band files…"); self.folder_files_button.setProperty("secondary", True)
        self.folder_files_button.setEnabled(False)
        self.folder_files_button.clicked.connect(self._reopen_folder_files)
        folder_form.addRow(self.folder_files_note)
        folder_form.addRow(self.folder_files_button)

        bands_page = QWidget()
        bands_form = self._form(bands_page)
        self.band_edits = {}
        for key, label in (
            ("blue", "Blue"), ("green", "Green"), ("red", "Red"),
            ("re", "Red edge"), ("nir", "NIR"),
        ):
            edit = QLineEdit()
            edit.setPlaceholderText("Optional" if key in ("re", "nir") else "Select raster")
            self.band_edits[key] = edit
            bands_form.addRow(label, self._path_row(edit, removable=True))
        self.source_stack.addWidget(stacked_page)
        self.source_stack.addWidget(folder_page)
        self.source_stack.addWidget(bands_page)
        source_layout.addWidget(self.source_stack)
        self.source_stack.currentChanged.connect(self._fit_source_stack)
        self._fit_source_stack()
        layout.addWidget(source_box)

        options_box, options_body = self._card("Image type and bands")
        options_form = self._form(options_body)
        self.options_form = options_form
        self.imagery_detection = QLabel("Choose an image to detect RGB, multispectral or hyperspectral input.")
        options_form.addRow(self._note(self.imagery_detection, "accent"))
        self.preview_status = QLabel("The image is shown on the map as soon as you choose it.")
        self.preview_status.setWordWrap(True); self.preview_status.setObjectName("ppeHint")
        options_form.addRow(self.preview_status)
        self.ortho_type = QComboBox()
        self.ortho_type.addItem("Auto-detect", "auto")
        self.ortho_type.addItem("RGB", "rgb")
        self.ortho_type.addItem("Multispectral", "ms")
        self.ortho_type.addItem("Hyperspectral", "hsi")
        self.rgb_order = QComboBox()
        self.rgb_order.addItem("Red, Green, Blue (camera RGB)", "rgb")
        self.rgb_order.addItem("Blue, Green, Red", "bgr")
        self.rgb_order.addItem("NIR, Red edge, Red", "nir_re_red")
        self.rgb_order.addItem("NIR, Red, Green (colour-infrared camera)", "nir_red_green")
        self.rgb_order.setToolTip("Only used when the file's three bands carry no names, colour tags or wavelengths.")
        self._detected = {"kind": "none", "bands": [], "count": 0, "named": False}
        logical_cpus = max(1, os.cpu_count() or 1)
        self.thread_count = max(1, min(16, logical_cpus - 1 if logical_cpus > 2 else logical_cpus))
        self.hsi_min_wavelength = QDoubleSpinBox(); self.hsi_min_wavelength.setRange(0, 2500); self.hsi_min_wavelength.setValue(0); self.hsi_min_wavelength.setSuffix(" nm")
        self.hsi_min_wavelength.setSpecialValueText("Read metadata")
        self.hsi_max_wavelength = QDoubleSpinBox(); self.hsi_max_wavelength.setRange(0, 2500); self.hsi_max_wavelength.setValue(0); self.hsi_max_wavelength.setSuffix(" nm")
        self.hsi_max_wavelength.setSpecialValueText("Read metadata")
        self.hsi_interval = QDoubleSpinBox(); self.hsi_interval.setRange(0, 100); self.hsi_interval.setDecimals(3); self.hsi_interval.setValue(0); self.hsi_interval.setSuffix(" nm")
        self.hsi_interval.setSpecialValueText("Read metadata")
        for spin in (self.hsi_min_wavelength, self.hsi_max_wavelength, self.hsi_interval):
            self._plain_spin(spin)
        options_form.addRow("Image type", self.ortho_type)
        # Fields that only apply to some images sit in their own boxes, hidden whole, so they
        # leave no empty gap when they do not apply.
        self.rgb_box = QWidget(); self._form(self.rgb_box).addRow("Band order (the file does not name its bands)", self.rgb_order)
        self.hsi_box = QWidget(); hsi_form = self._form(self.hsi_box)
        hsi_form.addRow("First wavelength", self.hsi_min_wavelength)
        hsi_form.addRow("Last wavelength", self.hsi_max_wavelength)
        hsi_form.addRow("Wavelength step", self.hsi_interval)
        options_form.addRow(self.rgb_box)
        options_form.addRow(self.hsi_box)
        self.rgb_order_label = None
        self.hsi_field_labels = [None, None, None]
        self._update_imagery_options()
        layout.addWidget(options_box)

        layout.addWidget(self._collapsible("About this field (optional)", self._field_info_box()))

        output_box = QWidget()
        output_form = self._form(output_box)
        self.output_path = QLineEdit()
        self.output_path.setPlaceholderText("Internal mask and log workspace")
        output_form.addRow("Output folder", self._path_row(self.output_path, directory=True))
        self.work_path = QLineEdit()
        self.work_path.setPlaceholderText("Fast local folder for temporary files")
        output_form.addRow("Working cache", self._path_row(self.work_path, directory=True))
        cache_help = QLabel(
            "Keep the working cache on a local disk, not OneDrive, Dropbox or a network drive."
        )
        cache_help.setWordWrap(True)
        cache_help.setObjectName("ppeHint")
        output_form.addRow(cache_help)
        layout.addWidget(self._collapsible("Folders", output_box))

        self.continue_button = QPushButton("Continue  →")
        self.continue_button.setProperty("primary", True)
        layout.addWidget(self.continue_button)
        layout.addStretch()
        return self._scroll_tab(page)

    def _generate_tab(self):
        page, layout = self._page()

        title = QLabel("Build the plant vote")
        title.setObjectName("sectionTitle")
        detail = QLabel("PPE ranks vegetation indices by how widely their values spread over the field and keeps the top three, one per spectral group. Each is split into soil and plant by Otsu, CDF, k-means and multi-Otsu, and the results are combined into a vote from 0 (soil) to 1 (plant).")
        detail.setWordWrap(True)
        detail.setObjectName("ppeHint")
        layout.addWidget(title)
        layout.addWidget(detail)

        flow = QLabel("Top 3 layers  →  4 methods  →  vote  →  guards")
        flow.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._note(flow, "accent"))

        self.generate_button = QPushButton("Run")
        self.generate_button.setProperty("primary", True)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setProperty("danger", True)
        self.cancel_button.setVisible(False)
        actions = QHBoxLayout()
        actions.setSpacing(6)
        actions.addWidget(self.generate_button, 1)
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("Ready")
        layout.addWidget(self.progress)

        self.run_summary = QLabel("No mask generated yet.")
        layout.addWidget(self._note(self.run_summary))

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setPlaceholderText("Processing details will appear here.")
        self.log_view.setMinimumHeight(200)
        layout.addWidget(self._collapsible("Processing log", self.log_view))
        self.review_button = QPushButton("Review the mask  →")
        self.review_button.setProperty("secondary", True)
        self.review_button.setEnabled(False)
        layout.addWidget(self.review_button)
        layout.addStretch(1)
        return self._scroll_tab(page)

    def _review_tab(self):
        page, layout = self._page()

        title = QLabel("Review plant mask")
        title.setObjectName("sectionTitle")
        help_text = QLabel("Move the slider to preview the mask on the map. Nothing is written until you finalize the mask.")
        help_text.setWordWrap(True)
        help_text.setObjectName("ppeHint")
        layout.addWidget(title)
        layout.addWidget(help_text)
        # Shown while an image of a batch is open (tab 6): which one, and a quick way to the next.
        self.batch_banner = QFrame(); self.batch_banner.setObjectName("accentBox")
        banner = QVBoxLayout(self.batch_banner); banner.setContentsMargins(12, 8, 12, 8); banner.setSpacing(6)
        self.batch_banner_text = QLabel(""); self.batch_banner_text.setObjectName("accentText"); self.batch_banner_text.setWordWrap(True)
        self.batch_banner_text.setMinimumWidth(1); banner.addWidget(self.batch_banner_text)
        banner_row = QHBoxLayout(); banner_row.setSpacing(6)
        self.batch_back_button = QPushButton("←  Batch list"); self.batch_back_button.setProperty("secondary", True)
        self.batch_next_button = QPushButton("Next image  →"); self.batch_next_button.setProperty("secondary", True)
        banner_row.addWidget(self.batch_back_button); banner_row.addWidget(self.batch_next_button, 1); banner.addLayout(banner_row)
        self.batch_banner.setVisible(False)
        layout.addWidget(self.batch_banner)

        stats = QFrame()
        stats.setObjectName("card")
        grid = QGridLayout(stats)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setVerticalSpacing(2)
        for column, caption in enumerate(("Vote threshold", "Canopy cover")):
            label = QLabel(caption); label.setObjectName("statCaption"); grid.addWidget(label, 0, column)
        self.threshold_value = QLabel("0.50")
        self.threshold_value.setObjectName("statValue")
        self.canopy_value = QLabel("—")
        self.canopy_value.setObjectName("statValue")
        grid.addWidget(self.threshold_value, 1, 0)
        grid.addWidget(self.canopy_value, 1, 1)
        layout.addWidget(stats)

        self.threshold_slider = QSlider(Qt.Horizontal)
        self.threshold_slider.setRange(15, 95)
        self.threshold_slider.setSingleStep(1)
        self.threshold_slider.setValue(50)
        self.threshold_slider.setEnabled(False)
        layout.addWidget(self.threshold_slider)
        range_row = QHBoxLayout()
        for text in ("0.15 · more plant", "less plant · 0.95"):
            label = QLabel(text); label.setObjectName("ppeHint"); range_row.addWidget(label)
            if text.startswith("0.15"): range_row.addStretch()
        layout.addLayout(range_row)

        # Suggested starting threshold (from the image itself; see suggest_threshold in native_engine).
        self.suggestion_label = QLabel("Run PPE to get a suggested threshold for this image.")
        self.suggestion_label.setWordWrap(True)
        self.suggestion_label.setObjectName("ppeHint")
        layout.addWidget(self.suggestion_label)
        self.suggestion_button = QPushButton("Back to suggested threshold")
        self.suggestion_button.setProperty("secondary", True)
        self.suggestion_button.setEnabled(False)
        self.suggestion_button.setVisible(False)
        self.suggestion_button.clicked.connect(self._use_suggestion)
        layout.addWidget(self.suggestion_button)

        self.curve_button = QPushButton("View threshold curve")
        self.curve_button.setProperty("secondary", True)
        self.curve_button.setEnabled(False)
        layout.addWidget(self.curve_button)

        layout.addWidget(self._pale_card())
        layout.addWidget(self._crops_box())

        self.apply_button = QPushButton("Finalize mask")
        self.apply_button.setProperty("primary", True)
        self.apply_button.setEnabled(False)
        layout.addWidget(self.apply_button)

        self.applied_status = QLabel("Run PPE first; the mask can then be reviewed here.")
        layout.addWidget(self._note(self.applied_status))
        layout.addStretch()
        return self._scroll_tab(page)

    def _export_tab(self):
        page, layout = self._page()
        title = QLabel("Export plant-only rasters"); title.setObjectName("sectionTitle")
        detail = QLabel("Each band and vegetation index is written as its own GeoTIFF on the original pixel grid, with soil set to NoData.")
        detail.setWordWrap(True); detail.setObjectName("ppeHint")
        layout.addWidget(title); layout.addWidget(detail)
        destination, destination_body = self._card("Destination"); form = self._form(destination_body)
        self.export_path = QLineEdit(); self.export_path.setPlaceholderText("Beside the image, unless you choose")
        form.addRow("Export folder", self._path_row(self.export_path, directory=True)); layout.addWidget(destination)
        self.export_bands_button = QPushButton("Export plant-only bands")
        self.export_bands_button.setToolTip("For HSI cubes, this exports every original hyperspectral band—not only the five processing proxies.")
        self.export_bands_button.setProperty("secondary", True); self.export_bands_button.setEnabled(False)
        self.export_vis_button = QPushButton("Export plant-only vegetation indices")
        self.export_vis_button.setProperty("secondary", True); self.export_vis_button.setEnabled(False)
        self.export_all_button = QPushButton("Export bands and all indices")
        self.export_all_button.setProperty("primary", True); self.export_all_button.setEnabled(False)
        layout.addWidget(self.export_bands_button); layout.addWidget(self.export_vis_button); layout.addWidget(self.export_all_button)
        self.export_progress = QProgressBar(); self.export_progress.setRange(0, 100); self.export_progress.setValue(0); self.export_progress.setFormat("Ready")
        layout.addWidget(self.export_progress)
        self.export_status = QLabel("Finalize the mask on tab 3 before exporting.")
        layout.addWidget(self._note(self.export_status))
        layout.addStretch(); return self._scroll_tab(page)

    def _aoi_export_tab(self):
        page, layout = self._page()
        title = QLabel("Plot statistics"); title.setObjectName("sectionTitle")
        detail = QLabel("Canopy cover of each plot, and statistics of its plant pixels for every band and vegetation index "
                        "(for hyperspectral images, every original band).")
        detail.setWordWrap(True); detail.setObjectName("ppeHint")
        layout.addWidget(title); layout.addWidget(detail)
        box, box_body = self._card("Plot boundaries"); form = self._form(box_body)
        self.aoi_path = QLineEdit(); self.aoi_path.setPlaceholderText("Shapefile (.shp or .zip), GeoPackage, GeoJSON, KML/KMZ …")
        form.addRow("Polygon file", self._aoi_path_row())
        self.aoi_layer = QComboBox(); self.aoi_layer.setToolTip("The file holds several polygon layers: choose the one with the plots.")
        self.aoi_layer_label = QLabel("Layer"); form.addRow(self.aoi_layer_label, self.aoi_layer)
        self.aoi_layer.setVisible(False); self.aoi_layer_label.setVisible(False)
        self.aoi_id_field = QComboBox(); self.aoi_id_field.addItem("Automatic · feature ID", "")
        self.aoi_id_field.setToolTip("Choose the attribute that identifies each plot. Automatic uses the feature ID.")
        form.addRow("Plot identifier", self.aoi_id_field)
        self.aoi_labels = QCheckBox("Show plot IDs on the map"); self.aoi_labels.setChecked(True)
        self.aoi_labels.setToolTip("The plots are shown as the layer 'PPE · Plots'. Switch the layer off in the Layers panel, "
                                   "or change its style there like any QGIS layer.")
        form.addRow("", self.aoi_labels)
        layout.addWidget(box)
        self.aoi_info = QLabel("Choose the plot file: the plots appear on the map with their IDs so you can check them.")
        layout.addWidget(self._note(self.aoi_info))
        output_note = QLabel("Output, in the export folder of tab 4: ppe_plot_canopy.csv (one row per plot: crop and threshold used, "
                             "plot and plant pixels, canopy %, plot and plant area in m², and the field information from tab 1) and "
                             "ppe_plot_statistics.csv (one row per plot and variable: crop, threshold and canopy columns, then pixel "
                             "count, mean, median, SD, CV %, min, P10, Q25, Q75, P90 and max of the plant pixels), with "
                             "ppe_run_record.json.")
        output_note.setWordWrap(True); output_note.setObjectName("ppeHint"); layout.addWidget(output_note)
        self.aoi_export_button = QPushButton("Export plot statistics")
        self.aoi_export_button.setProperty("primary", True); self.aoi_export_button.setEnabled(False)
        layout.addWidget(self.aoi_export_button)
        self.aoi_progress = QProgressBar(); self.aoi_progress.setRange(0, 100); self.aoi_progress.setValue(0); self.aoi_progress.setFormat("Ready")
        layout.addWidget(self.aoi_progress)
        self.aoi_status = QLabel("Finalize the mask on tab 3 before exporting plot statistics.")
        layout.addWidget(self._note(self.aoi_status))
        self.aoi_id_field.currentIndexChanged.connect(self._plot_labels_changed)
        self.aoi_labels.toggled.connect(self._plot_labels_changed)
        self.aoi_layer.currentIndexChanged.connect(self._plot_layer_changed)
        self.aoi_path.editingFinished.connect(self._plot_path_typed)
        layout.addStretch(); return self._scroll_tab(page)

    def _aoi_path_row(self):
        row = QWidget(); line = QHBoxLayout(row); line.setContentsMargins(0, 0, 0, 0); line.setSpacing(6)
        button = QPushButton("Browse…"); button.setProperty("secondary", True); button.clicked.connect(self._browse_aoi)
        button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed); self.aoi_path.textChanged.connect(self.aoi_path.setToolTip)
        line.addWidget(self.aoi_path, 1); line.addWidget(button); return row

    def _browse_aoi(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select plot boundaries", self.aoi_path.text(), PLOT_FILE_FILTER)
        if not path: return
        self.aoi_path.setText(path)
        self._load_plots(path)

    def _plot_path_typed(self):
        path = self.aoi_path.text().strip()
        if path and path != getattr(self, "_plots_loaded_from", None):
            self._load_plots(path)

    def _plot_layer_changed(self):
        if getattr(self, "_filling_plots", False): return
        path = self.aoi_path.text().strip()
        if path: self._load_plots(path, self.aoi_layer.currentData())

    def _plot_labels_changed(self):
        if getattr(self, "_filling_plots", False): return
        try:
            set_plot_labels(plot_layer(), self.aoi_id_field.currentData() or "", self.aoi_labels.isChecked())
        except Exception as err:
            self.append_log("Could not label the plots: {}".format(err))

    def _clear_plots(self, message):
        self._filling_plots = True
        self.aoi_layer.clear(); self.aoi_layer.setVisible(False); self.aoi_layer_label.setVisible(False)
        self.aoi_id_field.clear(); self.aoi_id_field.addItem("Automatic · feature ID", "")
        self._filling_plots = False
        self._plots_loaded_from = None
        try:
            remove_role("plots")
        except Exception:
            _LOG.debug("PPE: ignored error", exc_info=True)
        self.aoi_info.setText(message)

    def _load_plots(self, path, layer_name=None):
        """Read the plot file, fill the layer and ID lists, check it against the image and show it on the map."""
        try:
            _, _, info = open_plot_file(path, layer_name)
        except Exception as err:
            self._clear_plots("This plot file cannot be used: {}".format(err))
            return
        self._filling_plots = True
        previous_id = self.aoi_id_field.currentData() if layer_name else None
        self.aoi_layer.clear()
        for name in info["layers"]: self.aoi_layer.addItem(name, name)
        self.aoi_layer.setCurrentIndex(max(0, self.aoi_layer.findData(info["layer"])))
        several = len(info["layers"]) > 1
        self.aoi_layer.setVisible(several); self.aoi_layer_label.setVisible(several)
        self.aoi_id_field.clear(); self.aoi_id_field.addItem("Automatic · feature ID", "")
        for name in info["fields"]: self.aoi_id_field.addItem(name, name)
        wanted = previous_id if previous_id in info["fields"] else guess_plot_id_field(info["fields"])
        self.aoi_id_field.setCurrentIndex(max(0, self.aoi_id_field.findData(wanted)))
        self._filling_plots = False
        self._plots_loaded_from = path
        lines = ["{} plots in layer '{}'. Coordinate system: {}.".format(info["features"], info["layer"], info["crs_name"] or "none")]
        if info["note"]: lines.append("Note: {}.".format(info["note"][0].upper() + info["note"][1:]))
        image = getattr(self, "_image_grid_path", None)
        if image:
            try:
                over, total, note = plots_over_image(path, image, info["layer"])
                if over == 0:
                    lines.append("None of the plots lie over the image. Check that the file belongs to this field and that its "
                                 "coordinate system is right (for a Shapefile, the .prj file).")
                elif over < total:
                    lines.append("{} of {} plots lie over the image; the others will be listed with 0 pixels.".format(over, total))
                else:
                    lines.append("All {} plots lie over the image.".format(total))
                if note: lines.append("Note: {}.".format(note[0].upper() + note[1:]))
            except Exception as err:
                self.append_log("Could not check the plots against the image: {}".format(err))
        layer = None
        try:
            layer = load_plot_layer(info["uri"], info["layer"], self.aoi_id_field.currentData() or "", self.aoi_labels.isChecked())
        except Exception as err:
            self.append_log("Could not add the plots to the map: {}".format(err))
        lines.append("Shown on the map as 'PPE · Plots' with their IDs: check them before exporting." if layer is not None
                     else "QGIS could not draw this file, but the statistics can still be exported.")
        self.aoi_info.setText("\n".join(lines))

    # ---- Tab 6 Batch (4.2): many images, each reviewed with the normal slider ------------------
    GOAL_TABS = {"mask": ("image", "run", "review", "export", "plots", "batch"),
                 "count": ("image", "run", "review", "count", "batch"),
                 "training": ("image", "run", "review", "training", "batch"),
                 "labels": ("image", "labels")}

    def _apply_goal(self, goal):
        """Show the tabs of one goal, numbered in order. The first three steps are the same for every
        goal, and the goal's own step is always tab 4 (where Finalize leads)."""
        goal = goal if goal in self.GOAL_TABS else "mask"
        current = self.tabs.currentWidget()
        self.tabs.blockSignals(True)
        while self.tabs.count(): self.tabs.removeTab(0)
        for number, key in enumerate(self.GOAL_TABS[goal], 1):
            widget, name, tip = self._pages[key]
            index = self.tabs.addTab(widget, "{} {}".format(number, name)); self.tabs.setTabToolTip(index, tip)
        self.tabs.blockSignals(False)
        if current is not None and self.tabs.indexOf(current) >= 0: self.tabs.setCurrentWidget(current)
        elif current is not None: self.tabs.setCurrentIndex(3)          # was on the old goal's own step: go to this goal's
        self._goal = goal
        index = self.goal.findData(goal)
        if index >= 0 and self.goal.currentIndex() != index:
            self.goal.blockSignals(True); self.goal.setCurrentIndex(index); self.goal.blockSignals(False)

    def show_tab(self, key):
        """Go to a page by name (when the goal shows it)."""
        widget = self._pages.get(key, (None,))[0]
        if widget is not None and self.tabs.indexOf(widget) >= 0: self.tabs.setCurrentWidget(widget)

    def tab_number(self, key):
        widget = self._pages.get(key, (None,))[0]; index = self.tabs.indexOf(widget) if widget is not None else -1
        return index + 1 if index >= 0 else None

    def _goal_changed(self):
        old = getattr(self, "_goal", None)
        self._apply_goal(self.goal.currentData()); self.settings.setValue("ppe_qgis/goal", self._goal); self._refresh_goal_buttons()
        if old != self._goal:
            if old == "labels": self._label_edit_done()
            if old in getattr(self, "_areas", {}): self._area_end(old)
            if old == "count" and getattr(self, "_plant_tool", None) is not None: self._plant_edit_done()
            try:
                from .layer_utils import switch_goal_layers
                switch_goal_layers(old, self._goal)
            except Exception as err:
                self.append_log("Could not switch the layers to the new goal: {}".format(err))

    # ---- goal: plant counter -------------------------------------------------------------------
    def _count_tab(self):
        page, layout = self._page()
        title = QLabel("Count plants"); title.setObjectName("sectionTitle")
        hint = QLabel("Each plant in the finalized mask gets one point. Leaves of one plant that show as separate pieces are joined. "
                      "This works best early in the season, before plants touch: plants that touch become one, flagged 'check' "
                      "when much bigger than a typical plant.")
        hint.setWordWrap(True); hint.setObjectName("ppeHint"); layout.addWidget(title); layout.addWidget(hint)
        card, body = self._card("Settings"); form = self._form(body)
        self.count_min = self._plain_spin(QDoubleSpinBox()); self.count_min.setRange(0.01, 10000); self.count_min.setDecimals(2); self.count_min.setValue(1.0); self.count_min.setSuffix(" cm²")
        self.count_min.setToolTip("Groups smaller than this are specks, not plants. Set it a little below your smallest seedling.")
        form.addRow("Smallest plant", self.count_min)
        self.count_join = self._plain_spin(QDoubleSpinBox()); self.count_join.setRange(0, 20); self.count_join.setDecimals(1); self.count_join.setValue(1.0); self.count_join.setSuffix(" cm")
        self.count_join.setSpecialValueText("Off · every piece is a plant")
        self.count_join.setToolTip("Leaves of one plant often show as separate pieces (the stem or petiole between them is thin). "
                                   "Pieces closer than this are one plant. Keep it smaller than the gap between neighbouring plants.")
        form.addRow("Join leaves closer than", self.count_join)
        self.count_find_rows = QCheckBox("Find crop rows: plants between rows are possible weeds")
        self.count_find_rows.setChecked(True)
        self.count_find_rows.setToolTip("PPE finds the direction and spacing of the crop rows itself (15 to 150 cm apart, any crop), "
                                        "from the plant pixels, separately for each 4 m patch so shifted plots fit. Plants between the rows "
                                        "are marked as possible weeds (grey) and not counted; switch any back with Plant ↔ weed. "
                                        "Where rows are not clear (closed canopy, no rows), nothing is marked.")
        form.addRow("", self.count_find_rows)
        form.addRow("Area", self._area_widget("count", "Plants counted only inside ", "Whole image. Draw an area (or choose a field boundary) to count only there."))
        self.count_plots = QLineEdit(); self.count_plots.setPlaceholderText("Optional: plot polygons for counts per plot")
        form.addRow("Plot file", self._path_row(self.count_plots))
        self.count_plot_id = QLineEdit(); self.count_plot_id.setPlaceholderText("Attribute with the plot ID (empty: feature ID)")
        form.addRow("Plot identifier", self.count_plot_id)
        layout.addWidget(card)
        row = QHBoxLayout(); row.setSpacing(6)
        self.count_button = QPushButton("Count plants"); self.count_button.setProperty("primary", True); self.count_button.setEnabled(False)
        self.count_batch_button = QPushButton("Count all batch masks"); self.count_batch_button.setProperty("secondary", True); self.count_batch_button.setEnabled(False)
        row.addWidget(self.count_button, 1); row.addWidget(self.count_batch_button, 1); layout.addLayout(row)
        self.count_progress = QProgressBar(); self.count_progress.setRange(0, 100); self.count_progress.setValue(0); self.count_progress.setFormat("Ready")
        layout.addWidget(self.count_progress)
        self.count_status = QLabel("Finalize the mask on tab 3, then count.")
        layout.addWidget(self._note(self.count_status))
        fix, fix_body = self._card("Correct the count on the map")
        fb = QVBoxLayout(fix_body); fb.setContentsMargins(0, 0, 0, 0); fb.setSpacing(6)
        fix_hint = QLabel("PPE's points are a fast first count. Check them on the map and fix what is wrong: each click is saved "
                          "and the count updates. Grey points with a red ring are possible weeds (between rows) and are not counted.")
        fix_hint.setWordWrap(True); fix_hint.setObjectName("ppeHint"); fb.addWidget(fix_hint)
        row = QHBoxLayout(); row.setSpacing(6)
        self.plant_add_button = QPushButton("＋ Add plant"); self.plant_remove_button = QPushButton("－ Remove point")
        self.plant_switch_button = QPushButton("Plant ↔ weed")
        self.plant_add_button.setToolTip("Click on the map where a plant was missed.")
        self.plant_remove_button.setToolTip("Click on a point that is not a plant (soil, a leaf counted twice).")
        self.plant_switch_button.setToolTip("Click on a point to switch it between plant (counted) and weed (not counted).")
        for b in (self.plant_add_button, self.plant_remove_button, self.plant_switch_button):
            b.setCheckable(True); b.setProperty("secondary", True); b.setEnabled(False); row.addWidget(b, 1)
        fb.addLayout(row)
        row = QHBoxLayout(); row.setSpacing(6)
        self.plant_undo_button = QPushButton("Undo last"); self.plant_undo_button.setProperty("secondary", True); self.plant_undo_button.setEnabled(False)
        self.plant_done_button = QPushButton("Done"); self.plant_done_button.setProperty("secondary", True); self.plant_done_button.setEnabled(False)
        row.addWidget(self.plant_undo_button, 1); row.addWidget(self.plant_done_button, 1); fb.addLayout(row)
        self.plant_edit_status = QLabel("Count the plants first.")
        self.plant_edit_status.setObjectName("findingTitle"); self.plant_edit_status.setWordWrap(True); fb.addWidget(self.plant_edit_status)
        layout.addWidget(fix)
        self.plant_add_button.clicked.connect(lambda: self._plant_edit("add"))
        self.plant_remove_button.clicked.connect(lambda: self._plant_edit("remove"))
        self.plant_switch_button.clicked.connect(lambda: self._plant_edit("switch"))
        self.plant_undo_button.clicked.connect(self._plant_undo)
        self.plant_done_button.clicked.connect(self._plant_edit_done)
        out = QLabel("Output, in the image's output folder: ppe_count/ppe_plants.gpkg (one point per plant: area, 'check' flag, "
                     "on a row), ppe_count_summary.csv and, with plots, ppe_plot_counts.csv (plants and plants per m² per plot). "
                     "A batch also writes ppe_batch_counts.csv.")
        out.setWordWrap(True); out.setObjectName("ppeHint"); layout.addWidget(out)
        self.count_button.clicked.connect(lambda: self._emit_count(False))
        self.count_batch_button.clicked.connect(lambda: self._emit_count(True))
        layout.addStretch(); return self._scroll_tab(page)

    # ---- correcting the count on the map ---------------------------------------------------------
    def _plant_edit(self, mode):
        from .plant_edit import PlantEditTool, plants_layer
        layer = plants_layer()
        if layer is None:
            self._plant_edit_buttons(None); self.plant_edit_status.setText("Count the plants first."); return
        canvas = self.iface.mapCanvas(); old = getattr(self, "_plant_tool", None)
        if old is None:
            self._plant_prev_tool = canvas.mapTool()
        undo = old.undo if old is not None else []
        tool = PlantEditTool(canvas, layer, mode); tool.undo = undo
        tool.changed.connect(self._plant_changed); tool.done.connect(self._plant_edit_done)
        self._plant_tool = tool; canvas.setMapTool(tool); canvas.setFocus()
        self._plant_edit_buttons(mode)
        self.plant_edit_status.setText({"add": "Click on the map where a plant was missed.", "remove": "Click on a point to remove it.",
                                        "switch": "Click on a point to switch it between plant and weed."}[mode] + " Esc or Done to stop.")

    def _plant_edit_buttons(self, mode):
        for b, m in ((self.plant_add_button, "add"), (self.plant_remove_button, "remove"), (self.plant_switch_button, "switch")):
            b.setChecked(mode == m)
        tool = getattr(self, "_plant_tool", None)
        self.plant_undo_button.setEnabled(tool is not None); self.plant_done_button.setEnabled(tool is not None)

    def _plant_undo(self):
        tool = getattr(self, "_plant_tool", None)
        if tool is None or not tool.undo_last(): self.plant_edit_status.setText("Nothing to undo.")

    def _plant_edit_done(self):
        tool = getattr(self, "_plant_tool", None); self._plant_tool = None
        canvas = self.iface.mapCanvas()
        if tool is not None and canvas.mapTool() is tool:
            prev = getattr(self, "_plant_prev_tool", None)
            if prev is not None: canvas.setMapTool(prev)
            else: self.iface.actionPan().trigger()
        self._plant_edit_buttons(None); self._plant_changed("Corrections saved.")

    def _plant_changed(self, message):
        from .plant_edit import plants_layer
        from .plant_count import summarize_points
        layer = plants_layer()
        if layer is None: return
        try:
            r = summarize_points(layer.source().split("|")[0])
        except Exception as exc:
            self.plant_edit_status.setText("Could not update the totals: {}".format(exc)); return
        self.plant_edit_status.setText("{}  Count now: {} plants{}{} · {} weeds not counted.".format(
            message, r["plants"], " ({} added by you)".format(r["added_by_user"]) if r["added_by_user"] else "",
            " · {:.2f} per m²".format(r["plants_per_m2"]) if r.get("plants_per_m2") else "", r["weeds_not_counted"]))

    def plants_shown(self):
        """The plant points are on the map: correcting is possible."""
        for b in (self.plant_add_button, self.plant_remove_button, self.plant_switch_button): b.setEnabled(True)
        self.plant_edit_status.setText("Check the points on the map; correct any with the buttons above.")

    def _count_settings(self):
        return dict({"action": "count", "min_plant_cm2": self.count_min.value(), "join_cm": self.count_join.value(), "find_rows": self.count_find_rows.isChecked(),
                     "aoi_path": self.count_plots.text().strip(), "aoi_id_field": self.count_plot_id.text().strip()}, **(self._area_config("count") or {}))

    def _emit_count(self, batch):
        cfg = self._count_settings()
        if self._area_config("count") is None:
            QMessageBox.warning(self, "PPE count", "The area file does not exist."); return
        if cfg["aoi_path"] and not os.path.isfile(cfg["aoi_path"]):
            QMessageBox.warning(self, "PPE count", "The plot file does not exist."); return
        if batch:
            items = []
            for n, p in (getattr(self, "_batch_masks", None) or []):
                vote = ""
                try:
                    entry = self._batch_entry(n) or {}
                    with open(entry.get("result_path", ""), encoding="utf-8") as f: vote = json.load(f).get("vote_path", "")
                except Exception:
                    _LOG.debug("PPE: ignored error", exc_info=True)
                items.append({"name": n, "mask_path": p, "vote_path": vote})
            if not items: QMessageBox.warning(self, "PPE count", "Write the batch masks first (Batch tab)."); return
            cfg.update(count_items=items, output_dir=(self.batch or {}).get("manifest", {}).get("output_dir") or os.path.dirname(os.path.dirname(items[0]["mask_path"])))
        else:
            mask = (self.result or {}).get("mask_path", "")
            if not os.path.isfile(mask): QMessageBox.warning(self, "PPE count", "Finalize the mask on tab 3 first."); return
            cfg.update(mask_path=mask, vote_path=(self.result or {}).get("vote_path", ""), output_dir=os.path.join(os.path.dirname(mask), "ppe_count"))
        self.count_requested.emit(cfg)

    def set_counted(self, result):
        self.count_progress.setValue(100); self.count_progress.setFormat("Complete")
        per_m2 = result.get("plants_per_m2")
        if result.get("batch_counts_csv"):
            text = "{} plants in {} images.\n{}".format(result["plants"], result["images"], result["batch_counts_csv"])
        else:
            rows = result.get("rows") or {}
            text = "{} plants{}{}{}.".format(result["plants"], " · {:.2f} per m²".format(per_m2) if per_m2 else "",
                                              " · {} flagged 'check' (may be touching plants)".format(result["flagged_big"]) if result.get("flagged_big") else "",
                                              " · {} possible weeds between rows, not counted (grey)".format(result["weeds_not_counted"]) if result.get("weeds_not_counted") else "")
            if rows and rows.get("rows"): text += "\nCrop rows found {:.0f} cm apart.".format(rows.get("spacing_cm", 0))
            elif rows: text += "\nNo clear crop rows: nothing was marked as weed."
            if result.get("no_core_left_out"): text += "\n{} spots not counted: no pixel there where every PPE vote says plant (soil let in by the threshold).".format(result["no_core_left_out"])
            if result.get("plot_counts_path"): text += "\nPer plot: {}".format(result["plot_counts_path"])
            text += "\nPoints: {}".format(result.get("points_path", ""))
        self.count_status.setText(text + "\n" + result.get("note", ""))

    # ---- goal: training data ---------------------------------------------------------------
    def _training_tab(self):
        page, layout = self._page()
        title = QLabel("Training data for a model"); title.setObjectName("sectionTitle")
        hint = QLabel("Cuts the image and PPE's sure labels into tiles to train a fast plant/soil model, with no hand labelling. "
                      "Plant = every PPE vote says plant; soil = no vote says plant, or too dark to tell; the rest is 'unsure' "
                      "and the model skips it. The labels do not depend on the slider.")
        hint.setWordWrap(True); hint.setObjectName("ppeHint"); layout.addWidget(title); layout.addWidget(hint)
        card, body = self._card("Settings"); form = self._form(body)
        self.train_tile = QComboBox()
        for t in (256, 512): self.train_tile.addItem("{} × {} pixels".format(t, t), t)
        self.train_tile.setToolTip("256 trains fastest and was as accurate as 512 on PhenoBench.")
        form.addRow("Tile size", self.train_tile)
        self.train_folder = QLineEdit(); self.train_folder.setPlaceholderText("Empty: ppe_training_data in the output folder")
        form.addRow("Folder", self._path_row(self.train_folder, directory=True))
        layout.addWidget(card)
        row = QHBoxLayout(); row.setSpacing(6)
        self.train_button = QPushButton("From this image"); self.train_button.setProperty("primary", True); self.train_button.setEnabled(False)
        self.train_batch_button = QPushButton("From all batch images"); self.train_batch_button.setProperty("secondary", True); self.train_batch_button.setEnabled(False)
        row.addWidget(self.train_button, 1); row.addWidget(self.train_batch_button, 1); layout.addLayout(row)
        self.train_progress = QProgressBar(); self.train_progress.setRange(0, 100); self.train_progress.setValue(0); self.train_progress.setFormat("Ready")
        layout.addWidget(self.train_progress)
        self.train_status = QLabel("Run PPE on tab 2 first (or a batch).")
        layout.addWidget(self._note(self.train_status))
        tools = os.path.join(os.path.dirname(__file__), "tools")
        how, how_body = self._card("Train and keep the model up to date")
        hb = QVBoxLayout(how_body); hb.setContentsMargins(0, 0, 0, 0); hb.setSpacing(6)
        steps = QLabel("Outside QGIS, with Python and PyTorch (pip install torch), using ppe_train.py from the plugin's tools folder:\n"
                       "• train:  python ppe_train.py train <folder>   (about 2 minutes on a laptop CPU, faster on a GPU or Apple chip)\n"
                       "• use:  python ppe_train.py predict model.pt <image.tif>\n"
                       "• each new flight: run PPE, then  python ppe_train.py check model.pt <image.tif> <PPE run folder>\n"
                       "• if check says it drifted: make training data from the new flight here, then\n"
                       "   python ppe_train.py update model.pt <new folder> <old folder>   (new + old: it keeps what it knew)")
        steps.setWordWrap(True); steps.setTextInteractionFlags(Qt.TextSelectableByMouse); steps.setObjectName("ppeHint"); hb.addWidget(steps)
        open_tools = QPushButton("Open the tools folder"); open_tools.setProperty("secondary", True)
        open_tools.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(tools))); hb.addWidget(open_tools)
        layout.addWidget(how)
        self.train_button.clicked.connect(lambda: self._emit_training(False))
        self.train_batch_button.clicked.connect(lambda: self._emit_training(True))
        layout.addStretch(); return self._scroll_tab(page)

    def _emit_training(self, batch):
        base = self.output_path.text().strip()
        out = self.train_folder.text().strip() or os.path.join(base, "ppe_training_data")
        cfg = {"action": "training", "output_dir": out, "tile": self.train_tile.currentData()}
        if batch:
            m = (self.batch or {}).get("manifest") or {}
            path = m.get("manifest_path") or os.path.join(m.get("output_dir", ""), "ppe_batch.json")
            if not os.path.isfile(path): QMessageBox.warning(self, "PPE training data", "Run a batch first (Batch tab)."); return
            cfg["batch_manifest"] = path
        else:
            r = self.result or {}
            if not os.path.isfile(r.get("vote_path", "")): QMessageBox.warning(self, "PPE training data", "Run PPE on tab 2 first."); return
            src = self.build_config("run"); source = {k: src[k] for k in ("source_mode", "stacked_path", "folder_path", "folder_bands", "bands", "ortho_type", "rgb_order",
                                                                          "hsi_min_wavelength", "hsi_max_wavelength", "hsi_interval", "hsi_grid_confirmed") if k in src}
            name = os.path.splitext(os.path.basename((src.get("stacked_path") or src.get("folder_path") or "image").rstrip("/\\")))[0]
            cfg["items"] = [{"name": name, "source": source, "vote_path": r["vote_path"], "findings_path": r.get("findings_path", "")}]
        if os.path.isfile(os.path.join(out, "manifest.json")):
            if QMessageBox.question(self, "PPE training data", "The folder already holds training data. Add to it (tiles with the same names are replaced)?",
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) != QMessageBox.Yes: return
        self.training_requested.emit(cfg)

    def set_training_done(self, result):
        self.train_progress.setValue(100); self.train_progress.setFormat("Complete")
        shares = result.get("pixel_shares") or {}
        self.train_status.setText("{} tiles ({} train, {} validation) · plant {:.0f} %, soil {:.0f} %, unsure {:.0f} %\n{}".format(
            len(result.get("tiles", [])), result.get("train", 0), result.get("val", 0), 100 * shares.get("plant", 0), 100 * shares.get("soil", 0),
            100 * shares.get("unsure", 0), result.get("output_dir", "")))

    # ---- goal: create labels (4.6) -----------------------------------------------------------
    def _labels_tab(self):
        page, layout = self._page()
        title = QLabel("Create labels"); title.setObjectName("sectionTitle")
        hint = QLabel("Plant and soil label points for your own machine-learning model. Choose the image on tab 1, then Create labels: "
                      "PPE runs, keeps its suggested threshold and places the points, all in one go. "
                      "Only clear pixels can become a point (sure plant or sure soil, nothing unusual found there, not on an edge), "
                      "spread over every band, NDVI and the whole field by conditioned Latin hypercube sampling.")
        hint.setWordWrap(True); hint.setObjectName("ppeHint"); layout.addWidget(title); layout.addWidget(hint)
        card, body = self._card("1  Points"); form = self._form(body)
        self.label_count = self._plain_spin(QSpinBox()); self.label_count.setRange(10, 100000); self.label_count.setSingleStep(100); self.label_count.setValue(500)
        self.label_count.setToolTip("How many label points in total (plant + soil).")
        form.addRow("Number of points", self.label_count)
        self.label_share = self._plain_spin(QSpinBox()); self.label_share.setRange(5, 95); self.label_share.setValue(50); self.label_share.setSuffix(" % plant")
        self.label_share.setToolTip("Share of plant points; the rest are soil. 50 % gives the model both classes equally.")
        form.addRow("Plant / soil", self.label_share)
        form.addRow("Area", self._area_widget("labels", "Points and buffers only inside ", "Whole image. Draw an area to leave out field edges, headlands or weedy corners."))
        buf = QWidget(); bl = QVBoxLayout(buf); bl.setContentsMargins(0, 0, 0, 0); bl.setSpacing(2)
        row = QHBoxLayout(); row.setSpacing(8)
        self.label_radius = QSlider(Qt.Horizontal); self.label_radius.setRange(0, 100); self.label_radius.setSingleStep(1); self.label_radius.setPageStep(2)
        self.label_radius.setValue(0); self.label_radius.setToolTip("Buffer radius, 0 to 50 cm in 0.5 cm steps (arrow keys for single steps). "
                                                                    "Each point grows into a disc cut to the clear pixels of its own class, so a buffer "
                                                                    "stops at the edge of a leaf (or of the soil) and never takes in the other class.")
        self.label_radius_text = QLabel("0.0 cm · points only"); self.label_radius_text.setMinimumWidth(120)
        row.addWidget(self.label_radius, 1); row.addWidget(self.label_radius_text); bl.addLayout(row)
        buf_hint = QLabel("Grows each point into its own class only: it stops at the edge of a leaf or of the soil.")
        buf_hint.setWordWrap(True); buf_hint.setObjectName("ppeHint"); bl.addWidget(buf_hint)
        form.addRow("Buffer", buf)
        self.label_cap = self._plain_spin(QSpinBox()); self.label_cap.setRange(0, 1000000); self.label_cap.setSingleStep(50); self.label_cap.setValue(0)
        self.label_cap.setSpecialValueText("No limit"); self.label_cap.setToolTip("At most this many pixels per point (the nearest ones), so one big leaf cannot dominate.")
        form.addRow("Pixels per point", self.label_cap)
        self.label_preview = QLabel(""); self.label_preview.setObjectName("findingTitle"); self.label_preview.setWordWrap(True)
        form.addRow("", self.label_preview)
        layout.addWidget(card)
        self.label_create_button = QPushButton("Create labels"); self.label_create_button.setProperty("primary", True)
        layout.addWidget(self.label_create_button)
        self.label_progress = QProgressBar(); self.label_progress.setRange(0, 100); self.label_progress.setValue(0); self.label_progress.setFormat("Ready")
        layout.addWidget(self.label_progress)
        self.label_status = QLabel("Choose the image on tab 1, then Create labels.")
        layout.addWidget(self._note(self.label_status))
        fix, fix_body = self._card("2  Check the points on the map")
        fb = QVBoxLayout(fix_body); fb.setContentsMargins(0, 0, 0, 0); fb.setSpacing(6)
        fix_hint = QLabel("Green = plant, orange = soil. Zoom in and fix any point that is wrong; each click is saved. "
                          "A point you switch keeps only its own pixel (no buffer), because PPE's mask disagrees with you there.")
        fix_hint.setWordWrap(True); fix_hint.setObjectName("ppeHint"); fb.addWidget(fix_hint)
        row = QHBoxLayout(); row.setSpacing(6)
        self.label_switch_button = QPushButton("Plant ↔ soil"); self.label_remove_button = QPushButton("－ Remove")
        self.label_add_plant_button = QPushButton("＋ Plant"); self.label_add_soil_button = QPushButton("＋ Soil")
        self.label_switch_button.setToolTip("Click on a point to switch it between plant and soil.")
        self.label_remove_button.setToolTip("Click on a point to remove it.")
        self.label_add_plant_button.setToolTip("Click on the map to add a plant point.")
        self.label_add_soil_button.setToolTip("Click on the map to add a soil point.")
        self._label_edit_buttons = ((self.label_switch_button, "switch"), (self.label_remove_button, "remove"),
                                    (self.label_add_plant_button, "add_plant"), (self.label_add_soil_button, "add_soil"))
        for b, _ in self._label_edit_buttons: b.setCheckable(True); b.setProperty("secondary", True); b.setEnabled(False); row.addWidget(b, 1)
        fb.addLayout(row)
        row = QHBoxLayout(); row.setSpacing(6)
        self.label_undo_button = QPushButton("Undo last"); self.label_undo_button.setProperty("secondary", True); self.label_undo_button.setEnabled(False)
        self.label_done_button = QPushButton("Done"); self.label_done_button.setProperty("secondary", True); self.label_done_button.setEnabled(False)
        row.addWidget(self.label_undo_button, 1); row.addWidget(self.label_done_button, 1); fb.addLayout(row)
        self.label_edit_status = QLabel("Create the points first.")
        self.label_edit_status.setObjectName("findingTitle"); self.label_edit_status.setWordWrap(True); fb.addWidget(self.label_edit_status)
        layout.addWidget(fix)
        exp, exp_body = self._card("3  Export")
        eb = QVBoxLayout(exp_body); eb.setContentsMargins(0, 0, 0, 0); eb.setSpacing(6)
        self.label_export_folder = QLineEdit(); self.label_export_folder.setPlaceholderText("Choose a folder (asked when you export)")
        eb.addWidget(self._path_row(self.label_export_folder, directory=True))
        self.label_write_button = QPushButton("Export labels"); self.label_write_button.setProperty("primary", True); self.label_write_button.setEnabled(False)
        self.label_write_button.setToolTip("Writes the checked points with the buffer of step 1 to the folder: points, buffers, label raster and pixel table.")
        eb.addWidget(self.label_write_button)
        self.label_write_status = QLabel("")
        self.label_write_status.setWordWrap(True); self.label_write_status.setObjectName("ppeHint"); eb.addWidget(self.label_write_status)
        out = QLabel("Exported: ppe_labels.gpkg (points: label, PPE's label, status, band values), ppe_label_buffers.gpkg (one polygon per point), "
                     "ppe_label_raster.tif (1 plant, 0 soil, 255 no label, aligned with the image), ppe_label_pixels.csv (every labelled pixel "
                     "with its band values and NDVI, ready for a model) and the area, when one was used. "
                     "Pixels of one buffer are alike: test a model on whole points (column point_id), not on single pixels.")
        out.setWordWrap(True); out.setObjectName("ppeHint"); eb.addWidget(out)
        layout.addWidget(exp)
        self.label_create_button.clicked.connect(self._emit_labels_create)
        self.label_write_button.clicked.connect(self._emit_labels_write)
        self.label_radius.valueChanged.connect(self._label_preview_update)
        self.label_cap.valueChanged.connect(self._label_preview_update)
        for b, mode in self._label_edit_buttons: b.clicked.connect(lambda _=False, m=mode: self._label_edit(m))
        self.label_undo_button.clicked.connect(self._label_undo)
        self.label_done_button.clicked.connect(self._label_edit_done)
        layout.addStretch(); return self._scroll_tab(page)

    def _label_dir(self):
        return os.path.join(self.output_path.text().strip(), "ppe_labels")

    def _emit_labels_create(self):
        config = self.build_config("run"); error = self.validate_config(config)
        if error: QMessageBox.warning(self, "PPE input", error); return
        self._save_settings()
        out = self._label_dir()
        if os.path.isfile(os.path.join(out, "ppe_labels.gpkg")):
            if QMessageBox.question(self, "PPE labels", "Label points were already created in this output folder (maybe with your checks). Replace them with new points?",
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes: return
        self._label_edit_done()
        area = self._area_config("labels")
        if area is None: QMessageBox.warning(self, "PPE labels", "The area file does not exist."); return
        self._label_info = None; self.label_write_status.setText(""); self.label_preview.setText("")
        self.labels_requested.emit({"action": "labels_create", "labels_dir": out,
                                    "n_points": self.label_count.value(), "plant_share": self.label_share.value() / 100.0,
                                    **area})

    # ---- an area drawn on the map or a polygon file (labels 4.6, plant counter 4.6.4) --------------
    AREA_ROLES = {"labels": ("label_area", "PPE · Label area"), "count": ("count_area", "PPE · Count area")}

    def _area_widget(self, key, inside_text, whole_text):
        area = QWidget(); ar = QVBoxLayout(area); ar.setContentsMargins(0, 0, 0, 0); ar.setSpacing(4)
        row = QHBoxLayout(); row.setSpacing(6)
        draw = QPushButton("✎ Draw area"); draw.setProperty("secondary", True)
        draw.setToolTip("Click the corners of an area on the map, right-click to finish. Draw several to add them together.")
        clear = QPushButton("Clear"); clear.setProperty("secondary", True)
        row.addWidget(draw, 1); row.addWidget(clear); ar.addLayout(row)
        f = QLineEdit(); f.setPlaceholderText("or a polygon file (field boundary, plots…)"); ar.addWidget(self._path_row(f))
        status = QLabel(whole_text); status.setObjectName("ppeHint"); status.setWordWrap(True); ar.addWidget(status)
        if not hasattr(self, "_areas"): self._areas = {}
        self._areas[key] = {"geoms": [], "crs": None, "tool": None, "prev": None, "draw": draw, "file": f, "status": status,
                            "inside": inside_text, "whole": whole_text}
        draw.clicked.connect(lambda: self._area_draw(key)); clear.clicked.connect(lambda: self._area_clear(key))
        f.textChanged.connect(lambda *_: self._area_text(key))
        return area

    def _area_draw(self, key):
        if self._busy: self._busy_warning(); return
        a = self._areas[key]
        if a["tool"] is not None: return
        if key == "labels": self._label_edit_done()
        elif getattr(self, "_plant_tool", None) is not None: self._plant_edit_done()
        canvas = self.iface.mapCanvas(); a["prev"] = canvas.mapTool()
        tool = PolygonDrawTool(canvas); tool.finished.connect(lambda g: self._area_drawn(key, g)); tool.cancelled.connect(lambda: self._area_end(key))
        a["tool"] = tool; canvas.setMapTool(tool); canvas.setFocus()
        a["draw"].setText("● drawing…")
        a["status"].setText("Click the corners on the map; right-click (or double-click) to finish, Esc to cancel, Backspace undoes a corner.")

    def _area_drawn(self, key, geometry):
        a = self._areas[key]
        copy, crs = to_crs(geometry, self.iface.mapCanvas().mapSettings().destinationCrs(), self._image_wkt())
        a["geoms"].append(copy); a["crs"] = crs
        self._area_end(key); self._area_show(key)

    def _area_end(self, key):
        a = self._areas.get(key)
        if a is None: return
        tool = a["tool"]; a["tool"] = None
        canvas = self.iface.mapCanvas() if self.iface is not None else None
        if tool is not None and canvas is not None and canvas.mapTool() is tool and not getattr(tool, "switched_away", False):
            if a["prev"] is not None: canvas.setMapTool(a["prev"])
            else: self.iface.actionPan().trigger()
        a["draw"].setText("✎ Draw area"); self._area_text(key)

    def _area_clear(self, key):
        a = self._areas[key]; a["geoms"] = []; a["file"].clear(); self._area_show(key); self._area_text(key)

    def _area_show(self, key):
        try:
            from .layer_utils import show_area_layer
            role, name = self.AREA_ROLES[key]; a = self._areas[key]
            show_area_layer(a["geoms"], a["crs"], role, name)
        except Exception as err:
            self.append_log("Could not show the area: {}".format(err))

    def _area_text(self, key):
        a = self._areas[key]; n = len(a["geoms"]); f = a["file"].text().strip()
        parts = (["{} drawn area{}".format(n, "s" if n > 1 else "")] if n else []) + (["the polygons in {}".format(os.path.basename(f))] if f else [])
        a["status"].setText(a["inside"] + " + ".join(parts) + "." if parts else a["whole"])

    def _area_config(self, key):
        """area_wkts / area_path for a job, or None when the chosen file is missing."""
        a = self._areas[key]; f = a["file"].text().strip()
        if f and not os.path.isfile(f): return None
        return {"area_wkts": [g.asWkt() for g in a["geoms"]], "area_path": f}

    def _emit_labels_write(self):
        info = getattr(self, "_label_info", None)
        if not info: QMessageBox.warning(self, "PPE labels", "Create the labels first."); return
        folder = self.label_export_folder.text().strip()
        if not folder:
            folder = QFileDialog.getExistingDirectory(self, "Export the labels to", self.output_path.text().strip())
            if not folder: return
            self.label_export_folder.setText(folder)
        self._label_edit_done()
        self.labels_requested.emit({"action": "labels_write", "points_path": info["points_path"], "radius_cm": self.label_radius.value() / 2.0,
                                    "max_px": self.label_cap.value(), "export_dir": folder})

    def set_labels_created(self, info):
        self._label_info = info
        self.label_progress.setValue(100); self.label_progress.setFormat("Complete")
        self.label_status.setText("{} plant and {} soil points, chosen from {:,} clear plant and {:,} clear soil pixels ({:.1f} cm pixels){}.\n{}".format(
            info.get("plant_points", 0), info.get("soil_points", 0), info.get("clear_plant_pixels", 0), info.get("clear_soil_pixels", 0),
            info.get("pixel_cm", 0), " · PPE threshold {:.2f}".format(info["threshold"]) if info.get("threshold") is not None else "", info.get("points_path", "")))
        self.label_write_status.setText(""); self._label_preview_update(); self._refresh_goal_buttons()

    def labels_shown(self):
        for b, _ in self._label_edit_buttons: b.setEnabled(True)
        self.label_edit_status.setText("Check the points on the map; fix any with the buttons above.")

    def set_labels_written(self, result):
        self.label_progress.setValue(100); self.label_progress.setFormat("Complete")
        self.label_write_status.setText("Exported {:,} plant and {:,} soil pixels from {} points (buffer {:.1f} cm){} to\n{}".format(
            result.get("plant_pixels", 0), result.get("soil_pixels", 0), result.get("points", 0), result.get("radius_cm", 0),
            "; {} points you changed keep only their own pixel".format(result["point_only"]) if result.get("point_only") else "",
            result.get("export_dir", "")))

    def _label_preview_update(self, *_):
        cm = self.label_radius.value() / 2.0
        self.label_radius_text.setText("{:.1f} cm{}".format(cm, " · points only" if cm == 0 else ""))
        info = getattr(self, "_label_info", None)
        if not info: self.label_preview.setText(""); return
        try:
            from .label_points import buffer_preview, read_points
            t = buffer_preview(info, read_points(info["points_path"]), cm, self.label_cap.value())
            self.label_preview.setText("About {:,} plant and {:,} soil pixels will be labelled.".format(t["plant"], t["soil"]))
        except Exception as exc:
            self.label_preview.setText("Could not count the pixels: {}".format(exc))

    def _label_edit(self, mode):
        from .plant_edit import LabelEditTool, labels_layer
        layer = labels_layer()
        if layer is None:
            for b, _ in self._label_edit_buttons: b.setChecked(False)
            self.label_edit_status.setText("Create the points first."); return
        canvas = self.iface.mapCanvas(); old = getattr(self, "_label_tool", None)
        if old is None: self._label_prev_tool = canvas.mapTool()
        tool = LabelEditTool(canvas, layer, mode); tool.undo = old.undo if old is not None else []
        tool.changed.connect(self._label_changed); tool.done.connect(self._label_edit_done)
        self._label_tool = tool; canvas.setMapTool(tool); canvas.setFocus()
        for b, m in self._label_edit_buttons: b.setChecked(m == mode)
        self.label_undo_button.setEnabled(True); self.label_done_button.setEnabled(True)
        self.label_edit_status.setText({"switch": "Click on a point to switch it between plant and soil.", "remove": "Click on a point to remove it.",
                                        "add_plant": "Click on the map to add a plant point.", "add_soil": "Click on the map to add a soil point."}[mode]
                                       + " Esc or Done to stop.")

    def _label_undo(self):
        tool = getattr(self, "_label_tool", None)
        if tool is None or not tool.undo_last(): self.label_edit_status.setText("Nothing to undo.")

    def _label_edit_done(self):
        tool = getattr(self, "_label_tool", None); self._label_tool = None
        canvas = self.iface.mapCanvas() if self.iface is not None else None
        if tool is not None and canvas is not None and canvas.mapTool() is tool:
            prev = getattr(self, "_label_prev_tool", None)
            if prev is not None: canvas.setMapTool(prev)
            else: self.iface.actionPan().trigger()
        if hasattr(self, "_label_edit_buttons"):
            for b, _ in self._label_edit_buttons: b.setChecked(False)
            self.label_undo_button.setEnabled(False); self.label_done_button.setEnabled(False)
        if tool is not None: self._label_changed("Checks saved.")

    def _label_changed(self, message):
        info = getattr(self, "_label_info", None)
        if not info: return
        try:
            from .label_points import read_points
            pts = read_points(info["points_path"])
        except Exception as exc:
            self.label_edit_status.setText("Could not read the points: {}".format(exc)); return
        plant = sum(1 for p in pts if p["label"] == 1); changed = sum(1 for p in pts if p["status"] in ("changed", "added"))
        self.label_edit_status.setText("{}  Now {} plant and {} soil points{}.".format(message, plant, len(pts) - plant,
                                                                                    " · {} changed or added by you".format(changed) if changed else ""))
        self._label_preview_update()

    def _refresh_goal_buttons(self):
        busy = getattr(self, "_busy", False); r = self.result or {}
        if hasattr(self, "count_button"):
            self.count_button.setEnabled(not busy and bool(r.get("mask_path")))
            self.count_batch_button.setEnabled(not busy and bool(getattr(self, "_batch_masks", None)))
            self.train_button.setEnabled(not busy and bool(r.get("vote_path")))
            self.train_batch_button.setEnabled(not busy and bool(self.batch))
        if hasattr(self, "label_create_button"):
            self.label_create_button.setEnabled(not busy)
            self.label_write_button.setEnabled(not busy and bool(getattr(self, "_label_info", None)))

    def _batch_tab(self):
        page, layout = self._page()
        title = QLabel("Many images at once"); title.setObjectName("sectionTitle")
        hint = QLabel("Choose a folder: every raster file in it is one image, and every sub-folder with band files is one image. "
                      "PPE runs them one after another, exactly as on tab 2. Then open each image here to check it with the slider "
                      "on tab 3, and write all masks at once. Results go to the output folder of tab 1, one sub-folder per image.")
        hint.setWordWrap(True); hint.setObjectName("ppeHint")
        layout.addWidget(title); layout.addWidget(hint)
        card, body_widget = self._card("Images")
        body = QVBoxLayout(body_widget); body.setContentsMargins(0, 0, 0, 0); body.setSpacing(6)
        self.batch_folder = QLineEdit(); self.batch_folder.setPlaceholderText("Folder with the images")
        body.addWidget(self._path_row(self.batch_folder, directory=True))
        self.batch_found = QLabel("No folder chosen."); self.batch_found.setObjectName("ppeHint"); self.batch_found.setWordWrap(True)
        body.addWidget(self.batch_found)
        layout.addWidget(card)
        row = QHBoxLayout(); row.setSpacing(6)
        self.batch_run_button = QPushButton("Run all"); self.batch_run_button.setProperty("primary", True); self.batch_run_button.setEnabled(False)
        self.batch_cancel_button = QPushButton("Cancel"); self.batch_cancel_button.setProperty("danger", True); self.batch_cancel_button.setVisible(False)
        row.addWidget(self.batch_run_button, 1); row.addWidget(self.batch_cancel_button); layout.addLayout(row)
        self.batch_progress = QProgressBar(); self.batch_progress.setRange(0, 100); self.batch_progress.setValue(0); self.batch_progress.setFormat("Ready")
        layout.addWidget(self.batch_progress)
        self.batch_load_button = QPushButton("Open a previous batch…"); self.batch_load_button.setProperty("secondary", True)
        layout.addWidget(self.batch_load_button)
        self.batch_table = QTableWidget(0, 4)
        self.batch_table.setHorizontalHeaderLabels(["Image", "Threshold", "Plant", "Check"])
        self.batch_table.setSelectionBehavior(QAbstractItemView.SelectRows); self.batch_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.batch_table.setEditTriggers(QAbstractItemView.NoEditTriggers); self.batch_table.verticalHeader().setVisible(False)
        head = self.batch_table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2, 3): head.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.batch_table.setMinimumHeight(230)
        layout.addWidget(self.batch_table)
        self.batch_status = QLabel("Run a folder to fill this list. Images to check first (⚠, red) are listed on top.")
        layout.addWidget(self._note(self.batch_status))
        row = QHBoxLayout(); row.setSpacing(6)
        self.batch_review_button = QPushButton("Review selected  ↓"); self.batch_review_button.setProperty("secondary", True)
        self.batch_same_button = QPushButton("Same settings for all"); self.batch_same_button.setProperty("secondary", True)
        self.batch_same_button.setToolTip("Give every image the threshold (and the kinds left out) of the image open on tab 3")
        row.addWidget(self.batch_review_button, 1); row.addWidget(self.batch_same_button, 1); layout.addLayout(row)
        # Review right here: the image opens on the map and this box holds its slider and Finalize.
        self.batch_review_box, review_body = self._card("Review")
        rb = QVBoxLayout(review_body); rb.setContentsMargins(0, 0, 0, 0); rb.setSpacing(6)
        self.batch_review_name = QLabel(""); self.batch_review_name.setObjectName("findingTitle"); self.batch_review_name.setWordWrap(True)
        self.batch_review_name.setTextFormat(Qt.RichText); self.batch_review_name.setMinimumWidth(1); rb.addWidget(self.batch_review_name)
        nums = QGridLayout(); nums.setVerticalSpacing(0)
        for column, caption in enumerate(("Vote threshold", "Canopy cover")):
            label = QLabel(caption); label.setObjectName("statCaption"); nums.addWidget(label, 0, column)
        self.batch_threshold_value = QLabel("—"); self.batch_threshold_value.setObjectName("statValue")
        self.batch_canopy_value = QLabel("—"); self.batch_canopy_value.setObjectName("statValue")
        nums.addWidget(self.batch_threshold_value, 1, 0); nums.addWidget(self.batch_canopy_value, 1, 1); rb.addLayout(nums)
        self.batch_slider = QSlider(Qt.Horizontal); self.batch_slider.setRange(15, 95); self.batch_slider.setSingleStep(1); rb.addWidget(self.batch_slider)
        ends = QHBoxLayout()
        for text in ("0.15 · more plant", "less plant · 0.95"):
            label = QLabel(text); label.setObjectName("ppeHint"); ends.addWidget(label)
            if text.startswith("0.15"): ends.addStretch()
        rb.addLayout(ends)
        row2 = QHBoxLayout(); row2.setSpacing(6)
        self.batch_suggest_button = QPushButton("Suggested"); self.batch_suggest_button.setProperty("secondary", True)
        self.batch_more_button = QPushButton("More on tab 3"); self.batch_more_button.setProperty("secondary", True)
        self.batch_more_button.setToolTip("What PPE found (leave kinds out) and crops: on tab 3, for this same image")
        row2.addWidget(self.batch_suggest_button, 1); row2.addWidget(self.batch_more_button, 1); rb.addLayout(row2)
        row3 = QHBoxLayout(); row3.setSpacing(6)
        self.batch_finalize_one = QPushButton("✓  Finalize this mask"); self.batch_finalize_one.setProperty("primary", True)
        self.batch_next_inline = QPushButton("Next image  →"); self.batch_next_inline.setProperty("secondary", True)
        row3.addWidget(self.batch_finalize_one, 1); row3.addWidget(self.batch_next_inline, 1); rb.addLayout(row3)
        self.batch_review_note = QLabel(""); self.batch_review_note.setObjectName("ppeHint"); self.batch_review_note.setWordWrap(True)
        rb.addWidget(self.batch_review_note)
        self.batch_review_box.setVisible(False)
        layout.addWidget(self.batch_review_box)
        self.batch_finalize_button = QPushButton("✓  Finalize all masks"); self.batch_finalize_button.setProperty("primary", True)
        layout.addWidget(self.batch_finalize_button)
        for b in (self.batch_review_button, self.batch_same_button, self.batch_finalize_button): b.setEnabled(False)
        layout.addStretch(1)
        return self._scroll_tab(page)

    def _batch_scan(self, *_args):
        path = self.batch_folder.text().strip()
        items = list_batch_images(path) if path else []
        if not path:
            self.batch_found.setText("No folder chosen.")
        elif not items:
            self.batch_found.setText("No images found here: PPE needs raster files (.tif) or sub-folders with band files.")
        else:
            files = sum(1 for i in items if i["source_mode"] == "stacked")
            self.batch_found.setText("{} image(s): {} single file(s), {} band folder(s).".format(len(items), files, len(items) - files))
        self.batch_run_button.setEnabled(bool(items) and not self._busy)
        self._batch_items_found = items

    def _emit_batch_run(self):
        items = getattr(self, "_batch_items_found", None) or []
        config = self.build_config("batch_run")
        for key in ("stacked_path", "folder_path", "folder_bands", "bands", "source_mode", "field_info"):
            config.pop(key, None)
        config["ortho_type"] = "auto"            # every image is detected on its own
        config["batch_folder"] = self.batch_folder.text().strip(); config["batch_items"] = items
        if not config["output_dir"] or not config["work_dir"]:
            QMessageBox.warning(self, "PPE batch", "Set the output and working folders on tab 1 first."); return
        if not items:
            QMessageBox.warning(self, "PPE batch", "No images found in this folder."); return
        if os.path.isfile(os.path.join(config["output_dir"], "ppe_batch.json")):
            answer = QMessageBox.question(self, "PPE batch", "The output folder already holds a batch. Run again and replace its list?",
                                          QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes: return
        self.batch_run_requested.emit(config)

    def _batch_choices_path(self):
        return os.path.join(os.path.dirname(self.batch["manifest"]["manifest_path"]), "ppe_batch_choices.json")

    def set_batch(self, manifest, choices=None):
        manifest = dict(manifest); manifest.setdefault("manifest_path", os.path.join(manifest.get("output_dir", ""), "ppe_batch.json"))
        self.batch = {"manifest": manifest, "choices": dict(choices or {}), "open": None}
        self._batch_masks = [(n, c["finalized_mask"]) for n, c in self.batch["choices"].items()
                             if isinstance(c, dict) and c.get("finalized_mask") and os.path.isfile(c["finalized_mask"])]
        QTimer.singleShot(0, self._refresh_goal_buttons)
        self.batch_progress.setValue(100); self.batch_progress.setFormat("Done")
        self._fill_batch_table()

    def _batch_entries(self):
        """Images in list order: to check first, then the rest, then failed ones."""
        items = list((self.batch or {}).get("manifest", {}).get("items") or [])
        rank = lambda e: (2 if e.get("status") != "done" else (0 if e.get("warnings") else 1))
        return sorted(items, key=lambda e: (rank(e), e["name"].lower()))

    def _fill_batch_table(self):
        entries = self._batch_entries(); choices = (self.batch or {}).get("choices", {})
        self.batch_table.setRowCount(len(entries))
        for r, e in enumerate(entries):
            c = choices.get(e["name"]) or {}
            done = e.get("status") == "done"
            t = c.get("threshold", e.get("threshold"))
            plant = c.get("canopy_text") or ("{:.1f} %".format(e["canopy_pct"]) if e.get("canopy_pct") is not None else "—")
            if not done:
                check = "failed"; tip = "Failed: " + str(e.get("error", ""))
            else:
                warns = "; ".join(e.get("warnings") or [])
                check = "✓ mask" if c.get("finalized_mask") else ("✓ reviewed" if c.get("reviewed") else ("⚠ check" if warns else "—"))
                tip = "; ".join(x for x in ("Check: " + warns if warns else "", "reviewed" if c.get("reviewed") else "",
                                            "kinds left out or added" if (c.get("clip_codes") or c.get("add_codes")) else "",
                                            "mask written: " + c["finalized_mask"] if c.get("finalized_mask") else "") if x) or "nothing to check"
            cells = [e["name"], "{:.2f}".format(t) if done and t is not None else "—", plant if done else "—", check]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text); item.setToolTip(e["name"] + " · " + tip)
                if col == 0: item.setData(Qt.UserRole, e["name"])
                item.setForeground(QBrush(QColor("#1f3a30")))
                if done and e.get("warnings") and not c.get("reviewed"): item.setForeground(QBrush(QColor("#b3261e")))
                if not done: item.setForeground(QBrush(QColor("#8a8f8c")))
                if (self.batch or {}).get("open") == e["name"]:
                    f = item.font(); f.setBold(True); item.setFont(f)
                self.batch_table.setItem(r, col, item)
        n = len(entries); ok = sum(1 for e in entries if e.get("status") == "done")
        warned = sum(1 for e in entries if e.get("status") == "done" and e.get("warnings"))
        seen = sum(1 for e in entries if (choices.get(e["name"]) or {}).get("reviewed"))
        self.batch_status.setText("{} of {} image(s) done; {} to check first (⚠, red, on top); {} reviewed. Select one and press Review "
                                  "selected (or double-click it); hover a row for its checks. Images you do not review are written "
                                  "at their suggested or starting threshold.".format(ok, n, warned, seen) if n else "No images in this batch.")
        if (self.batch or {}).get("final_note"):
            self.batch_status.setText(self.batch_status.text() + "\n\n" + self.batch["final_note"])
        for b in (self.batch_review_button, self.batch_finalize_button): b.setEnabled(bool(ok) and not self._busy)
        self.batch_same_button.setEnabled(bool(ok) and bool((self.batch or {}).get("open")) and not self._busy)

    def _batch_selected_name(self):
        row = self.batch_table.currentRow()
        item = self.batch_table.item(row, 0) if row >= 0 else None
        return item.data(Qt.UserRole) if item is not None else None

    def _batch_entry(self, name):
        return next((e for e in (self.batch or {}).get("manifest", {}).get("items", []) if e["name"] == name), None)

    def _batch_open(self, name=None):
        if self._busy or not self.batch: return
        name = name or self._batch_selected_name()
        entry = self._batch_entry(name) if name else None
        if entry is None: return
        if entry.get("status") != "done":
            QMessageBox.information(self, "PPE batch", "This image failed: {}".format(entry.get("error", ""))); return
        self._batch_save_open()
        self.batch_open_requested.emit({"entry": entry, "choice": self.batch["choices"].get(name)})

    def batch_opened(self, entry, choice):
        """The plugin has loaded a batch image as the current result: restore what was chosen for it."""
        self.batch["open"] = entry["name"]
        if choice:
            self.threshold_slider.setValue(round(float(choice["threshold"]) * 100))
            for code, row in self.finding_rows.items():
                on = code in (choice.get("clip_codes") or []) or code in (choice.get("add_codes") or [])
                row["clip"].blockSignals(True); row["clip"].setChecked(on); row["clip"].blockSignals(False)
            self._clip_toggled(0, False)
        names = [e["name"] for e in self._batch_entries() if e.get("status") == "done"]
        i = names.index(entry["name"]) + 1 if entry["name"] in names else 0
        warn = "; ".join(entry.get("warnings") or [])
        self.batch_banner_text.setText("Batch image {} of {}: <b>{}</b>{}. Your slider setting is kept for it when you open another "
                                       "image or finalize all.".format(i, len(names), entry["name"], " · check: " + warn if warn else ""))
        self.batch_banner_text.setTextFormat(Qt.RichText)
        self.batch_next_button.setEnabled(i < len(names))
        self.batch_banner.setVisible(True)
        self.batch_review_name.setText("Image {} of {}: <b>{}</b>{}".format(i, len(names), entry["name"], "<br>Check: " + warn if warn else ""))
        self.batch_next_inline.setEnabled(i < len(names))
        self.batch_suggest_button.setEnabled(self.suggestion_button.isEnabled())
        self.batch_review_note.setText("The map shows this image and its live mask. The setting is kept for this image; "
                                       "Finalize this mask writes it now, or Finalize all masks writes every image at the end.")
        self.batch_review_box.setVisible(True)
        self._batch_sync_from_main()
        self._fill_batch_table()
        for r in range(self.batch_table.rowCount()):
            item = self.batch_table.item(r, 0)
            if item is not None and item.data(Qt.UserRole) == entry["name"]:
                self.batch_table.selectRow(r); break

    def _batch_sync_from_main(self, *_args):
        """The Batch tab's slider and numbers follow the main slider (tab 3), and the other way round."""
        if not hasattr(self, "batch_slider"): return
        self.batch_slider.blockSignals(True); self.batch_slider.setValue(self.threshold_slider.value()); self.batch_slider.blockSignals(False)
        self.batch_slider.setEnabled(self.threshold_slider.isEnabled())
        self.batch_threshold_value.setText(self.threshold_value.text()); self.batch_canopy_value.setText(self.canopy_value.text())

    def _batch_finalize_open(self):
        self._batch_save_open(); self._emit_apply()

    def batch_mask_written(self, result):
        """Finalize this mask finished for the open batch image."""
        if not self.batch or not self.batch.get("open"): return
        c = self.batch["choices"].setdefault(self.batch["open"], {})
        c["finalized_mask"] = result.get("mask_path", ""); self._batch_write_choices(); self._fill_batch_table()
        masks = dict(getattr(self, "_batch_masks", None) or []); masks[self.batch["open"]] = c["finalized_mask"]
        self._batch_masks = sorted(masks.items()); self._refresh_goal_buttons()
        self.batch_review_note.setText("Mask written: {}".format(result.get("mask_path", "")))

    def _batch_save_open(self):
        """Keep the threshold and kinds of the batch image open on tab 3."""
        if not self.batch or not self.batch.get("open") or (self.result or {}).get("vote_path") is None: return
        self.batch["choices"][self.batch["open"]] = {"threshold": self.threshold(), "clip_codes": self.clip_codes(),
                                                     "add_codes": self.add_codes(), "reviewed": True,
                                                     "canopy_text": self.canopy_value.text().replace("%", " %")}
        self._batch_write_choices()

    def _batch_write_choices(self):
        try:
            with open(self._batch_choices_path(), "w", encoding="utf-8") as f:
                json.dump(self.batch["choices"], f, indent=1)
        except (OSError, KeyError):
            pass

    def _batch_next(self):
        names = [e["name"] for e in self._batch_entries() if e.get("status") == "done"]
        cur = (self.batch or {}).get("open")
        if cur in names and names.index(cur) + 1 < len(names):
            self._batch_open(names[names.index(cur) + 1])

    def _batch_back(self):
        self._batch_save_open(); self._fill_batch_table(); self.show_tab("batch")

    def _batch_same_for_all(self):
        if not self.batch or not self.batch.get("open"): return
        t = self.threshold(); clip = self.clip_codes(); add = self.add_codes()
        answer = QMessageBox.question(self, "PPE batch", "Give every image threshold {:.2f}{}? Images you reviewed get it too.".format(
            t, " and the same kinds left out or added" if (clip or add) else ""), QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes: return
        self._batch_save_open()
        for e in self.batch["manifest"]["items"]:
            if e.get("status") != "done" or e["name"] == self.batch["open"]: continue
            old = self.batch["choices"].get(e["name"]) or {}
            self.batch["choices"][e["name"]] = {"threshold": t, "clip_codes": clip, "add_codes": add, "reviewed": bool(old.get("reviewed"))}
        self._batch_write_choices(); self._fill_batch_table()

    def _emit_batch_finalize(self):
        if not self.batch: return
        self._batch_save_open()
        m = self.batch["manifest"]
        self.batch_finalize_requested.emit({"action": "batch_finalize", "manifest_path": m["manifest_path"], "choices": self.batch["choices"],
                                            "output_dir": m.get("output_dir") or os.path.dirname(m["manifest_path"]), "threads": self.thread_count})

    def set_batch_finalized(self, result):
        rows = result.get("rows") or []
        self._batch_masks = [(r["image"], r["mask_path"]) for r in rows if r.get("mask_path")]
        self._refresh_goal_buttons()
        self.batch_progress.setValue(100); self.batch_progress.setFormat("{} masks written".format(len(rows)))
        if self.batch is not None:
            self.batch["final_note"] = ("{} masks written, one per image folder. Summary table (threshold, plant cover, what was left "
                                        "out, checks): {}".format(len(rows), result.get("summary_csv", "")))
        self._fill_batch_table()

    def _batch_load_previous(self):
        start = self.output_path.text().strip() if hasattr(self, "output_path") else ""
        path, _f = QFileDialog.getOpenFileName(self, "Open a previous batch", start, "PPE batch (ppe_batch.json)")
        if not path: return
        try:
            with open(path, encoding="utf-8") as f:
                manifest = json.load(f)
            manifest["manifest_path"] = path
            choices = {}
            cp = os.path.join(os.path.dirname(path), "ppe_batch_choices.json")
            if os.path.isfile(cp):
                with open(cp, encoding="utf-8") as f:
                    choices = json.load(f)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "PPE batch", "Could not read this batch: {}".format(exc)); return
        self.set_batch(manifest, choices)
        self.batch_progress.setFormat("Opened")

    def _connect_ui(self):
        self._mode_index = self.source_mode.currentIndex()
        self.source_mode.currentIndexChanged.connect(self._source_mode_changed)
        self.stacked_path.editingFinished.connect(self._on_stacked_path_edited)
        self.folder_path.editingFinished.connect(self._on_folder_path_edited)
        for edit in self.band_edits.values():
            edit.editingFinished.connect(self._refresh_source_detection)
        self.ortho_type.currentIndexChanged.connect(lambda _index: self._update_imagery_options())
        self.ortho_type.currentIndexChanged.connect(lambda _index: self._request_preview())
        self.rgb_order.currentIndexChanged.connect(lambda _index: self._request_preview())
        self.tabs.currentChanged.connect(lambda index: self._request_preview() if index == 1 else None)
        self.continue_button.clicked.connect(lambda: self.show_tab(self.GOAL_TABS.get(getattr(self, "_goal", "mask"), ("image", "run"))[1]))
        self.review_button.clicked.connect(lambda: self.show_tab("review"))
        self.goal.currentIndexChanged.connect(self._goal_changed)
        self.generate_button.clicked.connect(self._emit_generate)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.threshold_slider.valueChanged.connect(self._threshold_changed)
        self.curve_button.clicked.connect(self._show_curve)
        self.apply_button.clicked.connect(self._emit_apply)
        self.export_bands_button.clicked.connect(lambda: self._emit_export("export_bands"))
        self.export_vis_button.clicked.connect(lambda: self._emit_export("export_vis"))
        self.export_all_button.clicked.connect(lambda: self._emit_export("export_all"))
        self.aoi_export_button.clicked.connect(lambda: self._emit_export("export_aoi"))
        self.add_crop_button.clicked.connect(lambda: self._add_crop_row())
        self.same_threshold.toggled.connect(self._same_threshold_toggled)
        self.field_stage.currentIndexChanged.connect(lambda _index: self._update_stage_tip())
        self.batch_folder.textChanged.connect(self._batch_scan)
        self.batch_run_button.clicked.connect(self._emit_batch_run)
        self.batch_cancel_button.clicked.connect(self.cancel_requested)
        self.batch_load_button.clicked.connect(self._batch_load_previous)
        self.batch_review_button.clicked.connect(lambda: self._batch_open())
        self.batch_table.cellDoubleClicked.connect(lambda _r, _c: self._batch_open())
        self.batch_same_button.clicked.connect(self._batch_same_for_all)
        self.batch_finalize_button.clicked.connect(self._emit_batch_finalize)
        self.batch_next_button.clicked.connect(self._batch_next)
        self.batch_next_inline.clicked.connect(self._batch_next)
        self.batch_slider.valueChanged.connect(self.threshold_slider.setValue)
        self.threshold_slider.valueChanged.connect(self._batch_sync_from_main)
        self.batch_suggest_button.clicked.connect(self._use_suggestion)
        self.batch_more_button.clicked.connect(lambda: self.show_tab("review"))
        self.batch_finalize_one.clicked.connect(self._batch_finalize_open)
        self.batch_back_button.clicked.connect(self._batch_back)

    def _on_stacked_path_edited(self):
        path = self.stacked_path.text().strip()
        if os.path.isfile(path):
            self._set_export_default(os.path.dirname(path))
            try:
                self._configure_hsi_if_needed(path)
            except Exception as err:
                self.append_log("Could not read the image's wavelengths: {}".format(err))
        self._refresh_source_detection()

    def _on_folder_path_edited(self):
        path = self.folder_path.text().strip()
        if os.path.isdir(path):
            self._set_export_default(path)
            if not self._folder_bands_for or os.path.normpath(path) != os.path.normpath(self._folder_bands_for):
                self._choose_folder_files(path)
        else:
            self._forget_folder_bands()
        self._refresh_source_detection()

    def _reopen_folder_files(self):
        if self._busy:
            self._busy_warning(); return
        before = self._current_folder_bands()
        self._choose_folder_files()
        if self._current_folder_bands() != before:
            self._refresh_source_detection()

    def _is_source_field(self, target):
        return target is self.stacked_path or target is self.folder_path or target in self.band_edits.values()

    def _browse_file(self, target, executable=False):
        if self._is_source_field(target) and not self._allow_source_change(target):
            return
        caption = "Select Rscript" if executable else "Select raster"
        filter_text = "All files (*)" if executable else "Raster files (*.tif *.tiff *.vrt);;All files (*)"
        path, _ = QFileDialog.getOpenFileName(self, caption, target.text(), filter_text)
        if path and self._is_source_field(target) and os.path.normpath(path) == os.path.normpath(target.text().strip() or "."):
            return      # the same file again: nothing changes
        if path:
            target.setText(path)
            if target is self.stacked_path:
                try:
                    self._configure_hsi_if_needed(path)
                except Exception as err:
                    self.append_log("Could not read the image's wavelengths: {}".format(err))
            if target is self.stacked_path or target in self.band_edits.values():
                self._set_export_default(os.path.dirname(path))
            if target in self.band_edits.values(): self._refresh_source_detection()
            elif target is self.stacked_path: self._request_preview()

    def _set_field_visible(self, field, label, visible):
        field.setVisible(visible)
        if label is not None: label.setVisible(visible)

    KIND_NAMES = {"rgb": "RGB", "ms": "Multispectral", "hsi": "Hyperspectral", "limited": "Few bands",
                  "unnamed3": "3 unnamed bands", "unnamed": "Unnamed bands"}

    def _band_list(self, bands):
        return ", ".join(BAND_LABELS.get(b, b) for b in reversed(bands))

    def _update_imagery_options(self, *_ignored):
        """Show what was found in the image and only the options that apply to it."""
        found = self._detected; kind = found.get("kind", "none"); chosen = self.ortho_type.currentData()
        # The Auto-detect entry names what was found, so the closed list already says it.
        auto_text = "Auto-detect" if kind == "none" else "Auto-detect · {}".format(self.KIND_NAMES.get(kind, kind))
        if found.get("bands") and kind in ("ms", "limited"):
            auto_text += " · {} bands".format(len(found["bands"]))
        self.ortho_type.setItemText(0, auto_text)
        self.ortho_type.setToolTip("" if not found.get("bands") else "Bands found: " + self._band_list(found["bands"]))
        unnamed3 = kind == "unnamed3" or (chosen == "rgb" and found.get("count") == 3 and not found.get("named"))
        self.rgb_box.setVisible(unnamed3)
        self.hsi_box.setVisible(chosen == "hsi" or kind == "hsi")
        for widget in (self.rgb_box.parentWidget(), self.rgb_box.parentWidget().parentWidget() if self.rgb_box.parentWidget() else None):
            if widget is not None and widget.layout() is not None:
                widget.layout().invalidate(); widget.layout().activate(); widget.updateGeometry()
        bands = self._band_list(found.get("bands", []))
        if kind == "none":
            text = "Choose an image to detect RGB, multispectral or hyperspectral input."
        elif kind == "rgb":
            text = "RGB image: red, green and blue found."
        elif kind == "ms":
            missing = [BAND_LABELS[b] for b in ("blue", "green", "red", "re", "nir") if b not in found["bands"]]
            text = "Multispectral image: {} found.".format(bands)
            if missing:
                text += " No {} band, so PPE uses the indices these bands allow.".format(
                    " or ".join(missing) if len(missing) <= 2 else ", ".join(missing[:-1]) + " or " + missing[-1])
                if not all(b in found["bands"] for b in ("red", "green", "blue")):
                    text += " Shown on the map as {}.".format(display_channels(found["bands"])[0])
            if not found.get("named"):
                text += " The file has no band names, so the standard order blue, green, red, red edge, NIR is assumed."
        elif kind == "hsi":
            text = "Hyperspectral image ({} bands). Check the wavelength range below.".format(found.get("count"))
        elif kind == "unnamed3":
            text = "3 bands without names, colour tags or wavelengths. Choose their order below (camera RGB is the usual case)."
        elif kind == "limited":
            text = "Only {} found. PPE uses the indices these bands allow.".format(bands)
        else:
            text = "{} bands without names or wavelengths: PPE cannot tell which is which. Use a folder of named band files or add band names.".format(found.get("count"))
        if chosen not in ("auto", None) and kind in ("rgb", "ms", "hsi") and chosen != kind:
            text += "\nYou chose {}; band names in the file still take precedence.".format(self.KIND_NAMES[chosen])
        self.imagery_detection.setText(text)
        self._height_keeper.fit(self.imagery_detection)

    def _detect_config(self):
        return {"source_mode": self.source_mode.currentData(), "stacked_path": self.stacked_path.text().strip(),
                "folder_path": self.folder_path.text().strip(), "folder_bands": self._current_folder_bands(),
                "bands": {k: e.text().strip() for k, e in self.band_edits.items() if e.text().strip()}}

    def _refresh_source_detection(self):
        cfg = self._detect_config(); mode = cfg["source_mode"]
        ready = (mode == "stacked" and os.path.isfile(cfg["stacked_path"])) or (mode == "folder" and os.path.isdir(cfg["folder_path"])) or (mode == "bands" and cfg["bands"])
        self._detected = describe_image(cfg) if ready else {"kind": "none", "bands": [], "count": 0, "named": False}
        kind = self._detected["kind"]
        if kind != "hsi":
            self.hsi_grid_confirmed = False
        # Auto-detect stays selected and names what it found; HSI switches once its grid is confirmed.
        target = "hsi" if kind == "hsi" and self.hsi_grid_confirmed else "auto"
        self.ortho_type.blockSignals(True); self.ortho_type.setCurrentIndex(max(0, self.ortho_type.findData(target))); self.ortho_type.blockSignals(False)
        self._update_imagery_options()
        self._request_preview()

    # ---- changing, removing and resetting the image -------------------------------------
    def _confirm(self, title, text, action="Continue"):
        """Ask before something that discards work. Returns True to go ahead."""
        box = QMessageBox(QMessageBox.Question, title, text, QMessageBox.NoButton, self)
        go = box.addButton(action, QMessageBox.AcceptRole)
        go.setProperty("primary", True)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.setStyleSheet(self._stylesheet())
        go.setStyleSheet("QPushButton { background: #1d7550; color: #ffffff; border: 1px solid #1d7550; border-radius: 6px; min-height: 30px; padding: 0px 14px; font-weight: 600; }"
                         " QPushButton:hover { background: #17613f; }")
        box.setDefaultButton(go)
        box.exec_()
        return box.clickedButton() is go

    def _has_source(self):
        return bool(self.stacked_path.text().strip() or self.folder_path.text().strip()
                    or any(edit.text().strip() for edit in self.band_edits.values()))

    def _busy_warning(self):
        QMessageBox.information(self, "Plant Pixel Extractor", "PPE is still working. Wait for it to finish, or cancel it on the Run tab, before changing the image.")

    def _allow_source_change(self, target=None):
        """Ask before replacing an image that is already chosen. Choosing the first image, or
        filling an empty band slot before any results exist, needs no question."""
        if self._busy:
            self._busy_warning(); return False
        replacing = (target is not None and bool(target.text().strip())) or bool(self.result)
        if not replacing:
            return True
        return self._confirm(
            "Replace the image?",
            "Choosing another image starts PPE afresh: the current results, the review settings and "
            "the PPE layers on the map are cleared.",
            "Replace image",
        )

    def _source_mode_changed(self, index):
        if index == self._mode_index:
            return
        if self._busy or (self._has_source() and not self._confirm(
                "Change the image source?",
                "Switching to another kind of source removes the current image and clears all PPE results "
                "and PPE layers on the map.", "Change source")):
            if self._busy:
                self._busy_warning()
            self.source_mode.blockSignals(True); self.source_mode.setCurrentIndex(self._mode_index); self.source_mode.blockSignals(False)
            return
        self._mode_index = index
        self.stacked_path.clear(); self.folder_path.clear(); self._forget_folder_bands()
        for edit in self.band_edits.values():
            edit.clear()
        self.source_stack.setCurrentIndex(index)
        self._image_gone()
        self._refresh_source_detection()

    # ---- band folder: the user ticks which files are bands ---------------------------------
    def _current_folder_bands(self):
        folder = self.folder_path.text().strip()
        if self.folder_bands and self._folder_bands_for and os.path.normpath(folder) == os.path.normpath(self._folder_bands_for):
            return dict(self.folder_bands)
        return None

    def _forget_folder_bands(self):
        self.folder_bands = None; self._folder_bands_for = None
        if hasattr(self, "folder_files_note"):
            self.folder_files_note.setText("When you choose a folder, PPE lists its raster files so you can tick the band files.")
            self.folder_files_button.setEnabled(False)

    def _choose_folder_files(self, folder=None):
        """Show the band files of the folder; the ticked files are what PPE imports."""
        folder = folder or self.folder_path.text().strip()
        if not os.path.isdir(folder):
            return
        try:
            scan = scan_band_folder(folder)
        except Exception as err:
            self.folder_files_note.setText("The folder could not be read: {}".format(err)); return
        self.folder_files_button.setEnabled(True)
        if not scan["files"]:
            self._forget_folder_bands(); self.folder_files_button.setEnabled(True)
            self.folder_files_note.setText("⚠ No GeoTIFF (.tif) files in this folder.")
            return
        dialog = BandFilesDialog(folder, scan, self)
        self._band_dialog = dialog
        accepted = dialog.exec_() == QDialog.Accepted
        self._band_dialog = None
        if accepted:
            self.folder_bands = dialog.chosen(); self._folder_bands_for = folder
        elif self._folder_bands_for != folder:
            self.folder_bands = None; self._folder_bands_for = None
        chosen = self._current_folder_bands()
        if chosen:
            names = {"blue": "blue", "green": "green", "red": "red", "re": "red edge", "nir": "NIR"}
            self.folder_files_note.setText("Using {} ticked file(s): {}.".format(
                len(chosen), ", ".join(names[b] for b in ("blue", "green", "red", "re", "nir") if b in chosen)))
        else:
            self.folder_files_note.setText("No files ticked: PPE reads the bands from the file names.")

    def _remove_source(self, target):
        """The ✕ next to an image path."""
        if self._busy:
            self._busy_warning(); return
        if not target.text().strip():
            return
        if target in self.band_edits.values() and sum(bool(e.text().strip()) for e in self.band_edits.values()) > 1:
            question = "Remove this band? The image changes, so the current results and PPE layers on the map are cleared."
        else:
            question = "Remove the image from PPE? Its results and the PPE layers on the map are cleared."
        if not self._confirm("Remove the image?", question, "Remove"):
            return
        target.clear()
        if target is self.folder_path:
            self._forget_folder_bands()
        if self._has_source():
            self._refresh_source_detection()          # the remaining bands form a new image
        else:
            self._image_gone()

    def image_layer_removed(self):
        """The PPE image layer was deleted in the QGIS Layers panel: forget the image too, so the
        panel never refers to something that is no longer on the map."""
        self.stacked_path.clear(); self.folder_path.clear(); self._forget_folder_bands()
        for edit in self.band_edits.values():
            edit.clear()
        self._image_gone(emit=False)
        self.preview_status.setText("The image was removed from the map. Choose an image to start again.")

    def _image_gone(self, emit=True):
        self._preview_key = None
        self.reset_results()
        self._detected = {"kind": "none", "bands": [], "count": 0, "named": False}
        self.ortho_type.blockSignals(True); self.ortho_type.setCurrentIndex(0); self.ortho_type.blockSignals(False)
        self._update_imagery_options()
        self.preview_status.setText("The image is shown on the map as soon as you choose it.")
        if emit:
            self.reset_requested.emit()

    def reset_results(self):
        """Back to a clean start: nothing from the previous image is left in the panel."""
        if self.curve_dialog is not None:
            self.curve_dialog.close(); self.curve_dialog = None
        self.result = {}
        self.curve_points = []
        self.pale_frame.setVisible(False); self.pale_layer_removed(); self._close_pale_card()
        self.threshold_slider.blockSignals(True); self.threshold_slider.setValue(50); self.threshold_slider.blockSignals(False)
        self.threshold_slider.setEnabled(False)
        self.threshold_value.setText("0.50"); self.canopy_value.setText("—")
        self.suggestion_label.setText("Run PPE to get a suggested threshold for this image.")
        self.suggestion_button.setVisible(False); self.suggestion_button.setEnabled(False)
        for button in (self.curve_button, self.apply_button, self.review_button, self.export_bands_button,
                       self.export_vis_button, self.export_all_button, self.aoi_export_button):
            button.setEnabled(False)
        self.progress.setValue(0); self.progress.setFormat("Ready")
        self.export_progress.setValue(0); self.export_progress.setFormat("Ready")
        self.aoi_progress.setValue(0); self.aoi_progress.setFormat("Ready")
        self._clear_crops(remove_layer=True)
        self._reset_field_info()
        self.run_summary.setText("No mask generated yet.")
        self.applied_status.setText("Run PPE first; the mask can then be reviewed here.")
        self.export_status.setText("Finalize the mask on tab 3 before exporting.")
        self.aoi_status.setText("Finalize the mask on tab 3 before exporting plot statistics.")
        self.aoi_path.clear(); self._clear_plots("Choose the plot file: the plots appear on the map with their IDs so you can check them.")
        self.export_path.clear()
        self.log_view.clear()
        self.hsi_grid_confirmed = False
        self.tabs.setCurrentIndex(0)

    def mask_layers_removed(self, vote_removed):
        """PPE result layers were deleted in the Layers panel."""
        if vote_removed and self.result:
            self.threshold_slider.setEnabled(False)
            self.applied_status.setText("The live preview layer was removed from the map. Run PPE again to review the mask.")

    def _source_error(self, config):
        mode = config["source_mode"]
        if mode == "stacked" and not os.path.isfile(config["stacked_path"]): return "no file"
        if mode == "folder" and not os.path.isdir(config["folder_path"]): return "no folder"
        if mode == "bands" and (not config["bands"] or any(not os.path.isfile(path) for path in config["bands"].values())): return "no band files"
        return ""

    def _request_preview(self):
        """Ask for the chosen image to be shown on the map. Called whenever the source or the way
        its bands are read changes; does nothing if that image is already shown."""
        config = self.build_config("preview")
        if self._source_error(config):
            if self._preview_key is not None and not self._has_source():
                self._image_gone()
            return
        key = repr([config[k] for k in ("source_mode", "stacked_path", "folder_path", "ortho_type", "rgb_order", "hsi_min_wavelength", "hsi_max_wavelength", "hsi_interval")] + [sorted(config["bands"].items()), sorted((config.get("folder_bands") or {}).items())])
        if key == self._preview_key or self.receivers(self.preview_requested) == 0:
            return
        if self._preview_key is not None or self.result:
            # A different image (or the same file read another way): start afresh.
            self._preview_key = None
            self.reset_results()
            self.reset_requested.emit()
            self._set_export_default_from_source()
        self._preview_key = key
        config["preview_key"] = key
        self.preview_status.setText("Reading the image…")
        self.preview_requested.emit(config)

    def set_preview(self, result):
        sources = [p for p in (result.get("rgb_sources") or []) if p] or [result.get("rgb_path")]
        self._image_grid_path = sources[0] if sources and sources[0] else None
        if self.aoi_path.text().strip(): self._load_plots(self.aoi_path.text().strip(), self.aoi_layer.currentData())
        names = {"blue": "blue", "green": "green", "red": "red", "re": "red edge", "nir": "NIR"}
        found = ", ".join(names.get(b, b) for b in result.get("source_bands", []))
        mode = result.get("display_mode", "true colour")
        hint = {"true colour": "", "false colour (NIR, red, green)": " Plants look red, soil grey-blue."}.get(mode, " Plants look bright, soil darker.")
        self.preview_status.setText("On the map now: {}.{}\nBands found: {}.".format(mode, hint, found))

    def set_preview_failed(self, message):
        self._preview_key = None
        self.preview_status.setText("The image could not be shown yet: {}".format(message))

    def _configure_hsi_if_needed(self, path):
        if not path or not os.path.isfile(path):
            return True
        try:
            with open(path, "rb") as h:
                _ = h.read(16)
        except (TimeoutError, OSError) as err:
            if getattr(err, "errno", None) == 60 or "timed out" in str(err).lower():
                fname = os.path.basename(path)
                parent = os.path.dirname(os.path.abspath(path))
                QMessageBox.warning(
                    self,
                    "Cloud File Needs Local Download",
                    "'{fname}' is an online-only placeholder managed by OneDrive or iCloud Files On-Demand, "
                    "and macOS timed out waiting to download it.\n\n"
                    "To fix this in 10 seconds:\n"
                    "1. Open Finder at: {parent}\n"
                    "2. Right-click on '{fname}'\n"
                    "3. Click 'Always Keep on This Device' and wait for the download to complete.\n"
                    "4. Then select it again in PPE.".format(fname=fname, parent=parent)
                )
                return False
        try:
            ds = gdal.Open(path)
        except Exception:
            ds = None
        if ds is None:
            QMessageBox.warning(self, "Unreadable raster", "QGIS/GDAL could not open the selected raster.")
            return False
        is_hsi = ds.RasterCount > 15
        if not is_hsi:
            self.hsi_grid_confirmed = False
            return True
        values = dataset_wavelengths(ds, {})
        known = sorted(value for value in values if value is not None)
        minimum = known[0] if len(known) == ds.RasterCount else self.hsi_min_wavelength.value()
        maximum = known[-1] if len(known) == ds.RasterCount else self.hsi_max_wavelength.value()
        differences = [b-a for a, b in zip(known, known[1:]) if b > a]
        interval = median(differences) if len(known) == ds.RasterCount and differences else self.hsi_interval.value()
        dialog = HSIWavelengthDialog(ds.RasterCount, minimum, maximum, interval, self)
        if dialog.exec_() != QDialog.Accepted:
            return False
        self.hsi_min_wavelength.setValue(dialog.minimum.value())
        self.hsi_max_wavelength.setValue(dialog.maximum.value())
        self.hsi_interval.setValue(dialog.interval.value())
        self.hsi_grid_confirmed = True
        return True

    def _browse_directory(self, target):
        if target is self.folder_path and not self._allow_source_change(target):
            return
        path = QFileDialog.getExistingDirectory(self, "Select folder", target.text())
        if path and target is self.folder_path and os.path.normpath(path) == os.path.normpath(target.text().strip() or "."):
            return
        if path:
            target.setText(path)
            if target is self.folder_path:
                self._set_export_default(path)
                self._choose_folder_files(path)
                self._refresh_source_detection()

    def _set_export_default_from_source(self):
        mode = self.source_mode.currentData()
        if mode == "stacked": base = os.path.dirname(self.stacked_path.text().strip())
        elif mode == "folder": base = self.folder_path.text().strip()
        else:
            paths = [e.text().strip() for e in self.band_edits.values() if e.text().strip()]
            base = os.path.dirname(paths[0]) if paths else ""
        if base: self._set_export_default(base)

    def _set_export_default(self, source_directory):
        if source_directory and not self.export_path.text().strip():
            self.export_path.setText(os.path.join(source_directory, "PPE_exports"))

    def _ensure_export_default(self):
        if self.export_path.text().strip(): return
        mode = self.source_mode.currentData()
        if mode == "stacked": base = os.path.dirname(self.stacked_path.text().strip())
        elif mode == "folder": base = self.folder_path.text().strip()
        else:
            paths = [edit.text().strip() for edit in self.band_edits.values() if edit.text().strip()]
            base = os.path.dirname(paths[0]) if paths else ""
        self._set_export_default(base)

    def _default_output(self):
        project_home = QgsProject.instance().homePath()
        base = project_home if project_home else os.path.expanduser("~/Documents")
        return os.path.join(base, "PPE-QGIS")

    def _default_work(self):
        base = QStandardPaths.writableLocation(QStandardPaths.CacheLocation)
        if not base:
            base = os.path.expanduser("~/Library/Caches")
        return os.path.join(base, "PPE-QGIS")

    def _restore_settings(self):
        prefix = "ppe_qgis/"
        self.output_path.setText(self.settings.value(prefix + "output", self._default_output(), type=str))
        self.work_path.setText(self.settings.value(prefix + "work", self._default_work(), type=str))
        self.rgb_order.setCurrentIndex(max(0, self.rgb_order.findData(self.settings.value(prefix + "rgb_order", "rgb", type=str))))
        self.hsi_min_wavelength.setValue(self.settings.value(prefix + "hsi_min_wavelength", 0.0, type=float))
        self.hsi_max_wavelength.setValue(self.settings.value(prefix + "hsi_max_wavelength", 0.0, type=float))
        self.hsi_interval.setValue(self.settings.value(prefix + "hsi_interval", 0.0, type=float))
        self.export_path.setText("")
        self._apply_goal(self.settings.value(prefix + "goal", "mask", type=str))

    def _save_settings(self):
        prefix = "ppe_qgis/"
        self.settings.setValue(prefix + "output", self.output_path.text().strip())
        self.settings.setValue(prefix + "work", self.work_path.text().strip())
        self.settings.setValue(prefix + "rgb_order", self.rgb_order.currentData())
        self.settings.setValue(prefix + "hsi_min_wavelength", self.hsi_min_wavelength.value())
        self.settings.setValue(prefix + "hsi_max_wavelength", self.hsi_max_wavelength.value())
        self.settings.setValue(prefix + "hsi_interval", self.hsi_interval.value())
        self.settings.setValue(prefix + "goal", getattr(self, "_goal", "mask"))

    def build_config(self, action="run"):
        self._ensure_export_default()
        mode = self.source_mode.currentData()
        bands = {key: edit.text().strip() for key, edit in self.band_edits.items() if edit.text().strip()}
        ortho_type = self.ortho_type.currentData()
        is_hsi_selected = ortho_type == "hsi"
        config = {
            "action": action,
            "source_mode": mode,
            "stacked_path": self.stacked_path.text().strip(),
            "folder_path": self.folder_path.text().strip(),
            "folder_bands": self._current_folder_bands() if mode == "folder" else None,
            "bands": bands,
            "output_dir": self.output_path.text().strip(),
            "work_dir": self.work_path.text().strip(),
            "ortho_type": ortho_type,
            "rgb_order": self.rgb_order.currentData(),
            "hsi_min_wavelength": self.hsi_min_wavelength.value() if is_hsi_selected else 0.0,
            "hsi_max_wavelength": self.hsi_max_wavelength.value() if is_hsi_selected else 0.0,
            "hsi_interval": self.hsi_interval.value() if is_hsi_selected else 0.0,
            "hsi_grid_confirmed": self.hsi_grid_confirmed if is_hsi_selected else False,
            # Fixed settings (there is no Advanced settings card): threads are chosen by the
            # engine from the computer's cores and free memory, thresholds follow local
            # conditions, the four original methods vote, and the shadow and algae guards are
            # always on (the colour guard switches itself on for camera RGB images).
            "threads": self.thread_count,
            "adaptation_mode": "adaptive",
            "half_mixture": False,
            "sample_size": 100000,
            "initial_threshold": 0.5,
            "bright_guard": False,
            "shadow_guard": True,
            "biofilm_guard": True,
        }
        config["field_info"] = self.field_info()
        if action == "apply":
            config.update({
                "vote_path": self.result.get("vote_path", ""),
                "threshold": self.threshold(),
                "run_dir": self.result.get("run_dir", config["output_dir"]),
                "suggestion": self.result.get("suggestion") or {},
                "chosen_layers": self.result.get("chosen_layers") or [],
                "pale_plant_pct": self.result.get("pale_plant_pct"),
                "findings_path": self.result.get("findings_path", ""),
                "clip_codes": self.clip_codes(),
                "add_codes": self.add_codes(),
                "findings_shares": {self.FINDING_TEXT[k][1]: round(v, 3) for k, v in self.finding_shares_now().items()},
                "pale_path": self.result.get("pale_plant_path", ""),
            })
            if self.batch and self.batch.get("open"):
                entry = self._batch_entry(self.batch["open"])
                if entry: config["output_dir"] = entry["output_dir"]       # a batch image's mask goes to its own folder
            if self.zone_info:
                config.update({"zone_path": self.zone_info["zone_path"], "zone_thresholds": self.zone_thresholds(),
                               "zone_names": [row["name"].text().strip() or "crop {}".format(i + 1)
                                              for i, row in enumerate(self.zone_info["rows"])]})
        elif action.startswith("export"):
            config.update({"mask_path": self.result.get("mask_path", ""), "export_dir": self.export_path.text().strip(), "aoi_path": self.aoi_path.text().strip(), "aoi_id_field": self.aoi_id_field.currentData(), "aoi_layer": self.aoi_layer.currentData() or "",
                           "threshold": self.result.get("applied_threshold"), "run_record": self.result.get("run_record", ""),
                           "zone_path": self.result.get("zone_path", "") if self.result.get("crops") else "",
                           "zone_names": [c["crop"] for c in (self.result.get("crops") or [])],
                           "zone_thresholds": self.result.get("zone_thresholds") or [],
                           "pale_path": self.result.get("pale_plant_path", ""),
                           "left_out": list((self.result.get("left_out") or {}).keys()),
                           "added": list((self.result.get("added") or {}).keys()) if self.result.get("added_pixels") else []})
        return config

    def validate_config(self, config):
        if not config["output_dir"]:
            return "Select an output folder."
        if not config["work_dir"]:
            return "Select a fast local working-cache folder."
        if config["action"] == "apply":
            if not os.path.isfile(config.get("vote_path", "")):
                return "The vote-fraction raster is missing. Generate the mask again."
            return ""
        if config["action"].startswith("export"):
            if not os.path.isfile(config.get("mask_path", "")): return "Finalize the reviewed mask before exporting plant-only rasters."
            if not config.get("export_dir"): return "Select an export folder."
            if config["action"] == "export_aoi" and not os.path.isfile(config.get("aoi_path", "")): return "Select the plot boundary file on tab 5."
            return ""
        mode = config["source_mode"]
        if mode == "stacked" and not os.path.isfile(config["stacked_path"]):
            return "Select a valid stacked GeoTIFF."
        if mode == "folder" and not os.path.isdir(config["folder_path"]):
            return "Select a valid folder containing band rasters."
        if mode == "bands":
            missing_files = [key for key, path in config["bands"].items() if not os.path.isfile(path)]
            if missing_files:
                return "These band paths are invalid: {}.".format(", ".join(missing_files))
            if len(config["bands"]) < 1:
                return "Select at least one individual band raster."
        return ""

    # ---- optional information about the field (tab 1) --------------------------------------
    STAGES = ("Not given", "Early / vegetative", "Closed canopy", "Flowering / tasselling", "Drying down / senescence")

    def _field_info_box(self):
        box = QWidget(); form = self._form(box)
        hint = QLabel("Optional. Saved with the results (ppe_run_record.json and the plot CSV files) so runs can be "
                      "compared later. The growth stage also gives a tip on the Review tab. List the crops on the "
                      "Review tab, where each can get its own area and slider.")
        hint.setWordWrap(True); hint.setObjectName("ppeHint"); form.addRow(hint)
        self.field_stage = QComboBox()
        for text in self.STAGES: self.field_stage.addItem(text)
        form.addRow("Growth stage", self.field_stage)
        self.field_planting = QLineEdit(); self.field_planting.setPlaceholderText("YYYY-MM-DD")
        form.addRow("Planting date", self.field_planting)
        self.field_flight = QLineEdit(); self.field_flight.setPlaceholderText("YYYY-MM-DD")
        form.addRow("Flight date", self.field_flight)
        self.field_irrigation = QComboBox()
        for text in ("Not given", "Irrigated or wet soil", "Dry soil"): self.field_irrigation.addItem(text)
        form.addRow("Soil moisture", self.field_irrigation)
        self.field_weeds = QComboBox()
        for text in ("Not given", "Few weeds", "Some weeds", "Many weeds"): self.field_weeds.addItem(text)
        form.addRow("Weeds", self.field_weeds)
        self.field_notes = QLineEdit(); self.field_notes.setPlaceholderText("Variety, row spacing, anything else")
        form.addRow("Notes", self.field_notes)
        return box

    def field_info(self):
        pick = lambda combo: "" if combo.currentIndex() == 0 else combo.currentText()
        crops = [row["name"].text().strip() for row in self.crop_rows if row["name"].text().strip()]
        info = {"crops": crops, "growth_stage": pick(self.field_stage), "planting_date": self.field_planting.text().strip(),
                "flight_date": self.field_flight.text().strip(), "irrigation": pick(self.field_irrigation),
                "weeds": pick(self.field_weeds), "notes": self.field_notes.text().strip()}
        return {key: value for key, value in info.items() if value}

    def _reset_field_info(self):
        for combo in (self.field_stage, self.field_irrigation, self.field_weeds): combo.setCurrentIndex(0)
        for edit in (self.field_planting, self.field_flight, self.field_notes): edit.clear()

    def _update_stage_tip(self):
        stage = self.field_stage.currentText()
        if "Flowering" in stage:
            self.stage_tip.setText("Flowering / tasselling: tassels and flowers are plant material but not green, so many get "
                                   "few plant votes. On a corn field at tasselling a threshold near 0.30 kept them without "
                                   "letting soil in. Check on the map.")
            self.stage_tip.setVisible(True)
        elif "Drying" in stage:
            self.stage_tip.setText("Drying down: yellow and brown leaves get fewer plant votes; a lower threshold keeps more "
                                   "of them. Check on the map.")
            self.stage_tip.setVisible(True)
        else:
            self.stage_tip.setVisible(False)
        self._update_pale_card()
        if self.pale_frame.isVisible():
            self.stage_tip.setVisible(False)

    # ---- pale plant parts (tassels, flowers, heads...): information only (Review tab) ----------
    # ---- What PPE found (4.1): simple notes, each can be left out of the mask and put back -------
    FINDING_SHOW = 0.2         # a category is listed from this percent of the plant on
    FINDING_TEXT = {
        1: ("✿", "Plant parts that are not green", "Probably {parts}. They are plant; they drop out if you raise the slider."),
        2: ("◌", "Leaf edges over soil", "Edges of leaves where a little soil shows through, so they look paler. They are plant."),
        3: ("●", "Shaded leaves", "Darker leaves under the top of the canopy or in shadow. They are plant: their NDVI is a leaf's "
            "(and, with a good NIR band, they reflect more near-infrared light than bare soil can). Without NIR: dark pixels whose "
            "strongest colour is still green."),
        4: ("?", "Unclear", "Too dark to tell soil from leaf (without NIR: dark and not green, mostly soil in shadow). You can leave them out of the mask."),
        5: ("❧", "Dry or yellowing plant parts", "Leaves inside the canopy that are drying or turning yellow-brown. They are plant; "
            "leave them out if you want only green plant."),
        6: ("✚", "Faint plants in sparse spots", "Where the canopy is thin (under 25 % plant in 3 m squares at the suggested threshold), "
            "small and pale plants get fewer votes. These are the pixels a threshold 0.20 lower adds there. On a field with a "
            "reference mask, adding them matched it better in the thin parts and over the whole field."),
    }
    ADD_CODES = (6,)            # kinds the user can add to the mask (the others can be left out)

    def _pale_card(self):
        frame = QFrame(); frame.setObjectName("paleCard")
        box = QVBoxLayout(frame); box.setContentsMargins(12, 8, 12, 8); box.setSpacing(6)
        # The header opens and closes the card; it starts closed, so only the header shows.
        self.pale_title = QToolButton(); self.pale_title.setObjectName("paleTitle")
        self.pale_title.setCheckable(True); self.pale_title.setChecked(False)
        self.pale_title.setToolButtonStyle(Qt.ToolButtonTextBesideIcon); self.pale_title.setArrowType(Qt.RightArrow)
        self.pale_title.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.pale_title.setToolTip("Click to open or close the details")
        box.addWidget(self.pale_title)
        self.pale_body = QWidget(); body = QVBoxLayout(self.pale_body); body.setContentsMargins(0, 2, 0, 2); body.setSpacing(8)
        intro = QLabel("Nothing here changes the mask until you tick <b>Leave out of the mask</b> (or, for faint plants "
                       "in sparse spots, <b>Add to the mask</b>). The live preview drops (or adds) those pixels at once and "
                       "keeps them so whatever the slider; <b>Finalize mask</b> writes the mask that way. Untick to undo.")
        intro.setObjectName("paleText"); intro.setWordWrap(True); intro.setTextFormat(Qt.RichText); body.addWidget(intro)
        self.finding_rows = {}
        for code in sorted(self.FINDING_TEXT):
            holder = QWidget(); line = QVBoxLayout(holder); line.setContentsMargins(0, 0, 0, 0); line.setSpacing(2)
            title = QLabel(""); title.setObjectName("findingTitle"); title.setWordWrap(True); title.setTextFormat(Qt.RichText)
            note = QLabel(""); note.setObjectName("paleText"); note.setWordWrap(True)
            clip = QCheckBox("Add to the mask" if code in self.ADD_CODES else "Leave out of the mask")
            clip.toggled.connect(lambda on, code=code: self._clip_toggled(code, on))
            line.addWidget(title); line.addWidget(note); line.addWidget(clip)
            body.addWidget(holder); holder.setVisible(False)
            self.finding_rows[code] = {"holder": holder, "title": title, "note": note, "clip": clip}
        self.clip_status = QLabel(""); self.clip_status.setObjectName("paleText"); self.clip_status.setWordWrap(True); self.clip_status.setVisible(False)
        body.addWidget(self.clip_status)
        # which checks could not run on this image, and why (a weak NIR band, missing bands, ...)
        self.findings_scope = QLabel(""); self.findings_scope.setObjectName("paleText"); self.findings_scope.setWordWrap(True)
        self.findings_scope.setVisible(False); body.addWidget(self.findings_scope)
        buttons = QHBoxLayout()
        self.pale_button = QPushButton("Show them on the map"); self.pale_button.setCheckable(True)
        self.pale_button.setProperty("secondary", True)
        self.pale_button.toggled.connect(self._pale_button_toggled)
        self.put_back_button = QPushButton("Put all back"); self.put_back_button.setProperty("secondary", True); self.put_back_button.setVisible(False)
        self.put_back_button.clicked.connect(self._put_all_back)
        buttons.addWidget(self.pale_button); buttons.addWidget(self.put_back_button); body.addLayout(buttons)
        box.addWidget(self.pale_body); self.pale_body.setVisible(False)
        self.pale_title.toggled.connect(self._pale_opened)
        frame.setVisible(False); self.pale_frame = frame
        return frame

    def _pale_opened(self, opened):
        self.pale_title.setArrowType(Qt.DownArrow if opened else Qt.RightArrow)
        self.pale_body.setVisible(bool(opened))

    def _close_pale_card(self):
        self.pale_title.setChecked(False)

    def _set_pale_style(self, full):
        self.pale_frame.setProperty("compact", not full)
        for widget in (self.pale_frame, self.pale_title):
            widget.style().unpolish(widget); widget.style().polish(widget)

    def _pale_button_toggled(self, on):
        self.pale_button.setText("Hide them from the map" if on else "Show them on the map")
        self.pale_layer_toggled.emit(bool(on))

    def pale_layer_removed(self):
        """The findings layer was deleted in the Layers panel (or by a new run)."""
        self.pale_button.blockSignals(True); self.pale_button.setChecked(False); self.pale_button.blockSignals(False)
        self.pale_button.setText("Show them on the map")

    def _early_stage(self):
        return self.field_stage.currentIndex() > 0 and "Early" in self.field_stage.currentText()

    def finding_shares_now(self):
        """Percent of the plant in each finding at the current threshold ({} without findings). Early in
        the season nothing pale is a flower or tassel, so plant parts count as leaf edges."""
        hist = (self.result or {}).get("findings_hist")
        if not hist:
            return {}
        shares = finding_shares(hist, self.threshold())
        import numpy as np
        h = np.asarray(hist); levels = h.shape[1] - 1; start = int(np.ceil(max(0.0, self.threshold() - 0.05) * levels - 1e-9))
        plant = h[:, start:].sum()
        for code in self.ADD_CODES:       # what adding would bring in, as a share of the plant now
            shares[code] = 100.0 * h[code, :start].sum() / plant if code < h.shape[0] and plant else 0.0
        if self._early_stage():
            shares[2] += shares[1]; shares[1] = 0.0
        return shares

    def finding_kinds(self):
        """The kinds listed on the card, for the map: [(code, [codes drawn])]. Early in the season
        plant parts are counted as leaf edges, so the leaf-edge layer draws both."""
        early = self._early_stage(); kinds = []
        for code, row in self.finding_rows.items():
            if row["holder"].isHidden() or (early and code == 1):
                continue
            kinds.append((code, [1, 2] if (early and code == 2) else [code]))
        return kinds

    def clip_codes(self):
        """Finding codes left out of the mask (leaf edges include plant parts early in the season)."""
        codes = [code for code, row in self.finding_rows.items() if row["clip"].isChecked() and code not in self.ADD_CODES]
        if 2 in codes and self._early_stage(): codes.append(1)
        return sorted(set(codes))

    def add_codes(self):
        """Kinds added to the mask (faint plants in sparse spots). Not with crop areas, which set
        their own thresholds."""
        if not hasattr(self, "finding_rows") or self.zone_info:
            return []
        return [code for code in self.ADD_CODES if code in self.finding_rows and self.finding_rows[code]["clip"].isChecked()]

    def added_pixels_now(self):
        hist = (self.result or {}).get("findings_hist")
        if not hist or not self.add_codes():
            return 0
        import numpy as np
        hist = np.asarray(hist); levels = hist.shape[1] - 1
        start = int(np.ceil(max(0.0, self.threshold() - 0.05) * levels - 1e-9))
        return int(sum(hist[c, :start].sum() for c in self.add_codes() if c < hist.shape[0]))

    def _clip_toggled(self, code, on):
        """A kind was ticked or unticked: the live preview drops (or takes back) those pixels at once,
        at every threshold. Nothing is written until Finalize mask."""
        self._threshold_changed(self.threshold_slider.value())
        self._update_clip_status()
        if self.result.get("vote_path"):
            self.crop_preview_changed.emit()

    def _put_all_back(self):
        for row in self.finding_rows.values():
            row["clip"].blockSignals(True); row["clip"].setChecked(False); row["clip"].blockSignals(False)
        self._clip_toggled(0, False)

    def _show_clip_result(self, result):
        """After Finalize: what was left out, counted in the mask file itself."""
        if not hasattr(self, "clip_status"):
            return
        left = result.get("left_out") or {}; added = int(result.get("added_pixels") or 0)
        if left or added:
            parts = []
            if added:
                parts.append("{:,} pixels added ({})".format(added, ", ".join(result.get("added") or {})))
            if left:
                parts.append("{:,} pixels left out ({})".format(int(result.get("left_out_pixels") or 0),
                                                              ", ".join("{} {:,}".format(name, int(px)) for name, px in left.items())))
            self.clip_status.setText("✔ Finalized at {:.2f}: {}.\nCanopy {:.1f} % → {:.1f} %.".format(
                float(result.get("threshold") or self.threshold()), "; ".join(parts),
                float(result.get("canopy_before_clip_pct") or 0.0), float(result.get("canopy_pct") or 0.0)))
            self.clip_status.setVisible(True)

    def left_out_pixels_now(self):
        """Exact number of plant pixels the ticked kinds take out at the current threshold
        (None with crop areas, where each area has its own threshold)."""
        hist = (self.result or {}).get("findings_hist")
        if not hist or self.zone_info:
            return None
        import numpy as np
        hist = np.asarray(hist); levels = hist.shape[1] - 1
        start = int(np.ceil(max(0.0, self.threshold() - 0.05) * levels - 1e-9))
        return int(sum(hist[c, start:].sum() for c in self.clip_codes() if c < hist.shape[0]))

    def _update_clip_status(self):
        names = [self.FINDING_TEXT[c][1].lower() for c in self.clip_codes() if c in self.FINDING_TEXT]
        added = [self.FINDING_TEXT[c][1].lower() for c in self.add_codes()]
        lines = []
        if added:
            shares = self.finding_shares_now()
            lines.append("Added to the live preview: {} ({:,} pixels, {:.1f} % more plant at {:.2f}). They stay in whatever the slider; "
                         "Finalize mask writes them into the mask.".format(", ".join(added), self.added_pixels_now(),
                                                                           sum(shares.get(c, 0.0) for c in self.add_codes()), self.threshold()))
        if names:
            shares = self.finding_shares_now(); left = sum(shares.get(c, 0.0) for c in self.clip_codes())
            pixels = self.left_out_pixels_now()
            text = "Left out of the live preview: {} ({}{:.1f} % of the plant at {:.2f}). Move the slider: they stay out. " \
                   "Finalize mask writes them out of the mask.".format(
                       ", ".join(names), "{:,} pixels, ".format(pixels) if pixels is not None else "about ", left, self.threshold())
            lines.append(text)
        self.clip_status.setText("\n".join(lines))
        self.clip_status.setVisible(bool(lines)); self.put_back_button.setVisible(bool(lines))

    def _update_pale_card(self):
        """The 'What PPE found' card: one short note per kind of pixel worth pointing out."""
        shares = self.finding_shares_now()
        if not shares:
            self.pale_frame.setVisible(False); return
        stage = self.field_stage.currentText() if self.field_stage.currentIndex() > 0 else ""
        crops = [row["name"].text().strip() for row in self.crop_rows if row["name"].text().strip()]
        parts = likely_parts(crops[0] if len(crops) == 1 else None, stage)[0]
        shown = []
        for code, row in self.finding_rows.items():
            share = shares.get(code, 0.0)
            visible = share >= self.FINDING_SHOW or row["clip"].isChecked()
            row["holder"].setVisible(visible)
            if visible:
                mark, name, note = self.FINDING_TEXT[code]
                rgb = {c: col for c, col, _n in FINDING_COLOURS}[code]
                # the square has the colour of this kind on the map (Show them on the map)
                row["title"].setText('<span style="color:#{:02x}{:02x}{:02x}; font-size:15px;">■</span>&nbsp;{}&nbsp;&nbsp;{} — {}'.format(
                    rgb[0], rgb[1], rgb[2], mark, name,
                    "adds {:.1f} % more plant".format(share) if code in self.ADD_CODES else "{:.1f} % of the plant".format(share)))
                if code in self.ADD_CODES and self.zone_info:
                    note = note + " Not available with crop areas: lower that crop's slider instead."
                row["clip"].setEnabled(not (code in self.ADD_CODES and self.zone_info))
                row["note"].setText(note.format(parts=parts))
                shown.append(name)
        r = self.result or {}
        scope = []
        if r.get("findings_note"):
            scope.append(r["findings_note"][0].upper() + r["findings_note"][1:] + ".")
        for what, why in (r.get("findings_not_checked") or {}).items():
            scope.append("Not checked: {} ({}).".format(what, why))
        if r.get("weak_split_warning"):
            scope.insert(0, r["weak_split_warning"])
        if (r.get("sparse_spots") or {}).get("whole_field_thin"):
            scope.append("Faint plants in sparse spots: not marked, because {:.0f} % of the 3 m squares are thin, so the whole field is thin: "
                         "move the slider instead.".format(r["sparse_spots"]["squares_pct"]))
        self.findings_scope.setText("\n".join(scope)); self.findings_scope.setVisible(bool(scope))
        self.findings_scope.setToolTip("Each check runs when this image has the bands it needs; its references are measured on this image.")
        if not shown and not scope:
            self.pale_frame.setVisible(False)
            if self.pale_button.isChecked(): self.pale_button.setChecked(False)
            return
        self._set_pale_style(True)
        self.pale_button.setVisible(bool(shown))
        self.pale_title.setText("✿  What PPE found: {}".format(
            "{} thing{} to look at".format(len(shown), "" if len(shown) == 1 else "s") if shown else "nothing to look at"))
        self.pale_title.setToolTip("; ".join(shown) + ". Click to open or close.")
        self._update_clip_status()
        self.pale_frame.setVisible(True)

    # ---- crops in this image: one outline and one slider per crop (Review tab) ------------------
    def _crops_hint(self):
        if not self.result.get("vote_path"):
            return "Run PPE first. Then, for an image with more than one crop, add each crop and draw a rough outline around it."
        return ("More than one crop in the image? Add each crop and draw a rough outline around it on the map "
                "(click the corners, right-click to finish, Esc to cancel). Each crop then gets its own slider; "
                "the slider above sets the rest of the image.")

    def _crops_box(self):
        frame = QFrame(); frame.setObjectName("card")
        box = QVBoxLayout(frame); box.setContentsMargins(14, 12, 14, 12); box.setSpacing(6)
        title = QLabel("Crops in this image (optional)"); title.setObjectName("cardTitle"); box.addWidget(title)
        self.crops_note = QLabel(self._crops_hint()); self.crops_note.setWordWrap(True); self.crops_note.setObjectName("ppeHint")
        box.addWidget(self.crops_note)
        self.stage_tip = QLabel(""); self.stage_tip.setWordWrap(True); self.stage_tip.setObjectName("ppeHint"); self.stage_tip.setVisible(False)
        box.addWidget(self.stage_tip)
        self.crop_list = QVBoxLayout(); self.crop_list.setSpacing(8); box.addLayout(self.crop_list)
        buttons = QHBoxLayout()
        self.add_crop_button = QPushButton("+ Add crop"); self.add_crop_button.setProperty("secondary", True); self.add_crop_button.setEnabled(False)
        buttons.addWidget(self.add_crop_button); buttons.addStretch(); box.addLayout(buttons)
        self.same_threshold = QCheckBox("Use the slider above for every crop"); self.same_threshold.setVisible(False)
        box.addWidget(self.same_threshold)
        self.crop_progress = QProgressBar(); self.crop_progress.setRange(0, 100); self.crop_progress.setValue(0); self.crop_progress.setFormat("Ready")
        self.crop_progress.setVisible(False); box.addWidget(self.crop_progress)
        return frame

    def _add_crop_row(self, name=""):
        frame = QFrame(); grid = QGridLayout(frame); grid.setContentsMargins(0, 0, 0, 0); grid.setHorizontalSpacing(6); grid.setVerticalSpacing(2)
        edit = QLineEdit(name); edit.setPlaceholderText("Crop, e.g. corn")
        draw = QPushButton("Draw area"); draw.setProperty("secondary", True); draw.setEnabled(self._draw_allowed())
        status = QLabel("not drawn"); status.setObjectName("ppeHint")
        remove = QToolButton(); remove.setText("✕"); remove.setToolTip("Remove this crop and its outline")
        slider = QSlider(Qt.Horizontal); slider.setRange(15, 95); slider.setValue(self.threshold_slider.value()); slider.setEnabled(False)
        value = QLabel("{:.2f}".format(slider.value() / 100.0)); canopy = QLabel("—"); canopy.setObjectName("ppeHint")
        grid.addWidget(edit, 0, 0); grid.addWidget(draw, 0, 1); grid.addWidget(status, 0, 2); grid.addWidget(remove, 0, 3)
        grid.addWidget(slider, 1, 0, 1, 2); grid.addWidget(value, 1, 2); grid.addWidget(canopy, 1, 3)
        grid.setColumnStretch(0, 1)
        row = {"frame": frame, "name": edit, "draw": draw, "status": status, "remove": remove, "slider": slider,
               "value": value, "canopy": canopy, "geometry": None}
        self.crop_rows.append(row); self.crop_list.addWidget(frame)
        draw.clicked.connect(lambda: self._draw_crop(row))
        remove.clicked.connect(lambda: self._remove_crop(row))
        edit.editingFinished.connect(self._crop_names_changed)
        slider.valueChanged.connect(lambda v: self._crop_slider_moved(row, v))
        self.same_threshold.setVisible(len(self.crop_rows) > 1)
        edit.setFocus()
        return row

    def _crop_slider_moved(self, row, value):
        row["value"].setText("{:.2f}".format(value / 100.0))
        self._threshold_changed(self.threshold_slider.value())
        self.crop_preview_changed.emit()

    def _drawn_rows(self):
        return [row for row in self.crop_rows if row["geometry"] is not None]

    def _image_wkt(self):
        ds = gdal.Open(self.result.get("vote_path", "")) if self.result.get("vote_path") else None
        return ds.GetProjection() if ds is not None else ""

    def _draw_allowed(self):
        return bool(self.result.get("vote_path")) and not self._busy and self._drawing_row is None

    def _refresh_draw_buttons(self):
        """While an outline is being drawn its button is greyed out and says so, and the other
        crops cannot start drawing; afterwards every button returns to Draw area / Redraw."""
        allowed = self._draw_allowed()
        for row in self.crop_rows:
            button = row["draw"]; drawing = row is self._drawing_row
            button.setText("✎ Drawing…" if drawing else ("Redraw" if row["geometry"] is not None else "Draw area"))
            button.setEnabled(allowed and not drawing)
            button.setProperty("drawing", drawing)
            button.setToolTip("Click the corners of the area on the map. Right-click to finish, Esc to cancel." if drawing else "")
            row["remove"].setEnabled(not drawing and not self._busy)
            row["status"].setProperty("drawing", drawing)
            for widget in (button, row["status"]):
                widget.style().unpolish(widget); widget.style().polish(widget)
        self.crops_note.setProperty("drawing", self._drawing_row is not None)
        self.crops_note.style().unpolish(self.crops_note); self.crops_note.style().polish(self.crops_note)

    def _draw_crop(self, row):
        if self._busy:
            self._busy_warning(); return
        if self._drawing_row is not None:
            return
        canvas = self.iface.mapCanvas()
        self._previous_tool = canvas.mapTool()
        tool = PolygonDrawTool(canvas)
        tool.finished.connect(lambda geometry: self._crop_drawn(row, geometry))
        tool.cancelled.connect(lambda: self._drawing_done(row, cancelled=True))
        self._draw_tool = tool; self._drawing_row = row
        canvas.setMapTool(tool)
        name = row["name"].text().strip() or "this crop"
        row["status"].setText("● drawing")
        self.crops_note.setText("✎ Drawing the area of {}: click its corners on the map, right-click (or double-click) to finish. "
                                "Esc cancels, Backspace undoes the last corner.".format(name))
        self._refresh_draw_buttons()
        bar = self.iface.messageBar()
        self._draw_message = bar.createMessage("Plant Pixel Extractor", "Drawing the area of {}: click its corners on the map, "
                                               "right-click to finish, Esc to cancel.".format(name))
        bar.pushWidget(self._draw_message, Qgis.Info)
        canvas.setFocus()

    def _drawing_done(self, row, cancelled=False):
        canvas = self.iface.mapCanvas()
        tool = self._draw_tool
        self._draw_tool = None; self._drawing_row = None
        message, self._draw_message = getattr(self, "_draw_message", None), None
        if message is not None:
            try: self.iface.messageBar().popWidget(message)
            except RuntimeError: pass           # already closed by the user
        if tool is not None and canvas.mapTool() is tool and not getattr(tool, "switched_away", False):
            if self._previous_tool is not None: canvas.setMapTool(self._previous_tool)
            else: self.iface.actionPan().trigger()      # no tool before drawing: leave the map on Pan
        if cancelled and row is not None and row in self.crop_rows:
            row["status"].setText("✓ drawn" if row["geometry"] is not None else "not drawn")
        self.crops_note.setText(self._crops_hint())
        self._refresh_draw_buttons()

    def _crop_drawn(self, row, geometry):
        if row not in self.crop_rows:
            return
        copy, crs = to_crs(geometry, self.iface.mapCanvas().mapSettings().destinationCrs(), self._image_wkt())
        row["geometry"] = copy; self._crop_crs = crs
        row["status"].setText("✓ drawn")
        self._drawing_done(row)
        self._show_crop_layer()
        self._request_zones()

    def _remove_crop(self, row):
        if self._busy:
            self._busy_warning(); return
        had_area = row["geometry"] is not None
        self.crop_rows.remove(row); row["frame"].setParent(None); row["frame"].deleteLater()
        self.same_threshold.setVisible(len(self.crop_rows) > 1)
        self._show_crop_layer()
        if had_area:
            if self._drawn_rows(): self._request_zones()
            else: self._zones_gone()

    def _crop_names_changed(self):
        self._update_stage_tip()
        self._show_crop_layer()
        self.crop_preview_changed.emit()          # legend names follow the crop names

    def _show_crop_layer(self):
        crs = getattr(self, "_crop_crs", None)
        if crs is None:
            return
        try:
            show_crop_areas([(row["name"].text().strip() or "crop {}".format(i + 1), row["geometry"])
                             for i, row in enumerate(self.crop_rows)], crs)
        except Exception as err:
            self.append_log("Could not show the crop areas: {}".format(err))

    def _request_zones(self):
        rows = self._drawn_rows()
        if not rows or not self.result.get("vote_path"):
            return
        for row in self.crop_rows: row["slider"].setEnabled(False)
        self.crop_progress.setVisible(True)
        self.zone_info = None
        self._pending_zone_rows = rows
        self.zones_requested.emit({"action": "zones", "vote_path": self.result["vote_path"],
                                   "pale_path": self.result.get("pale_plant_path", ""),
                                   "output_dir": self.result.get("run_dir") or os.path.dirname(self.result["vote_path"]),
                                   "crop_areas": [{"name": row["name"].text().strip() or "crop {}".format(i + 1),
                                                   "wkt": row["geometry"].asWkt()} for i, row in enumerate(rows)]})

    def set_zones(self, result):
        # The crop areas are ready: switch on one slider per drawn crop.
        rows = [row for row in getattr(self, "_pending_zone_rows", []) if row in self.crop_rows and row["geometry"] is not None]
        if len(rows) != len(result.get("zone_curves", [])) - 1:
            self._request_zones(); return                    # the outlines changed while preparing
        self.zone_info = dict(result); self.zone_info["rows"] = rows
        for row in rows: row["slider"].setEnabled(not self.same_threshold.isChecked())
        self.crop_progress.setValue(100); self.crop_progress.setFormat("Crop areas ready")
        self._threshold_changed(self.threshold_slider.value())
        self._update_stage_tip()

    def _zones_gone(self):
        self.zone_info = None
        self.crop_progress.setVisible(False)
        self.zones_cleared.emit()
        self._threshold_changed(self.threshold_slider.value())
        self._update_stage_tip()

    def _clear_crops(self, remove_layer=False):
        if self._draw_tool is not None:
            self._drawing_done(None)
        for row in list(self.crop_rows):
            row["frame"].setParent(None); row["frame"].deleteLater()
        self.crop_rows = []; self.zone_info = None
        self.same_threshold.setChecked(False); self.same_threshold.setVisible(False)
        self.crop_progress.setVisible(False); self.add_crop_button.setEnabled(False)
        self.crops_note.setText(self._crops_hint())
        if remove_layer:
            try: remove_role("crops")
            except Exception: _LOG.debug("PPE: ignored error", exc_info=True)

    def _same_threshold_toggled(self, on):
        for row in self.crop_rows:
            if on:
                row["slider"].blockSignals(True); row["slider"].setValue(self.threshold_slider.value()); row["slider"].blockSignals(False)
                row["value"].setText("{:.2f}".format(self.threshold()))
            row["slider"].setEnabled(not on and self.zone_info is not None and row in self.zone_info["rows"])
        self._threshold_changed(self.threshold_slider.value()); self.crop_preview_changed.emit()

    def zone_thresholds(self):
        # Thresholds for the preview and the final mask: [rest of image, crop 1, crop 2, ...].
        if not self.zone_info:
            return [self.threshold()]
        main = self.threshold()
        return [main] + [main if self.same_threshold.isChecked() else row["slider"].value() / 100.0 for row in self.zone_info["rows"]]

    def zone_names(self):
        if not self.zone_info:
            return []
        return [row["name"].text().strip() or "crop {}".format(i + 1) for i, row in enumerate(self.zone_info["rows"])]

    def _update_crop_labels(self):
        if not self.zone_info:
            for row in self.crop_rows: row["canopy"].setText("—")
            return
        curves = self.zone_info["zone_curves"]; thresholds = self.zone_thresholds()
        for k, row in enumerate(self.zone_info["rows"], start=1):
            value = canopy_at([(t, c) for t, c in curves[k] if c is not None], thresholds[k])
            row["canopy"].setText("—" if value is None else "{:.1f} %".format(value))

    def _whole_canopy(self, main_threshold):
        curves = self.zone_info["zone_curves"]; pixels = self.zone_info["zone_pixels"]; thresholds = self.zone_thresholds()
        thresholds[0] = main_threshold
        total = sum(pixels)
        if not total:
            return None
        plant = 0.0
        for k, (curve, count) in enumerate(zip(curves, pixels)):
            value = canopy_at([(t, c) for t, c in curve if c is not None], thresholds[k]) if count else 0.0
            plant += (value or 0.0) * count
        return plant / total

    def _emit_generate(self):
        config = self.build_config("run")
        error = self.validate_config(config)
        if error:
            QMessageBox.warning(self, "PPE input", error)
            return
        self._save_settings()
        self.generate_requested.emit(config)

    def _emit_apply(self):
        config = self.build_config("apply")
        error = self.validate_config(config)
        if error:
            QMessageBox.warning(self, "PPE export", error)
            return
        self.apply_requested.emit(config)

    def _emit_export(self, action):
        if action in ("export_vis", "export_all"):
            answer = QMessageBox.question(self, "Export all vegetation indices", "This can write many full-resolution GeoTIFFs and may require substantial time and disk space. Continue?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes: return
        config = self.build_config(action); error = self.validate_config(config)
        if error: QMessageBox.warning(self, "PPE export", error); return
        self._save_settings(); self.export_requested.emit(config)

    def threshold(self):
        return self.threshold_slider.value() / 100.0

    def _threshold_changed(self, value):
        threshold = value / 100.0
        self.threshold_value.setText("{:.2f}".format(threshold))
        if self.same_threshold.isChecked():
            for row in self.crop_rows:
                row["slider"].blockSignals(True); row["slider"].setValue(value); row["slider"].blockSignals(False)
                row["value"].setText("{:.2f}".format(threshold))
        self._update_crop_labels()
        canopy = self._whole_canopy(threshold) if self.zone_info else canopy_at(self.curve_points, threshold)
        shares_now = self.finding_shares_now() if hasattr(self, "finding_rows") else {}
        left = sum(shares_now.get(c, 0.0) for c in self.clip_codes()) if hasattr(self, "finding_rows") else 0.0
        more = sum(shares_now.get(c, 0.0) for c in self.add_codes()) if hasattr(self, "finding_rows") else 0.0
        if canopy is not None and (left or more):
            canopy = canopy * (1.0 - left / 100.0 + more / 100.0)   # both shares are shares of the plant
        self.canopy_value.setText("—" if canopy is None else "{:.1f}%".format(canopy))
        if hasattr(self, "finding_rows") and self.pale_frame.isVisible():
            self._update_pale_card()
        if self.curve_dialog is not None:
            self.curve_dialog.set_threshold(threshold)
        self._update_stale_note()

    def _update_stale_note(self):
        """The Export tab exports the finalized mask: say so when the Review tab has moved on."""
        r = self.result or {}
        if not r.get("mask_path") or not hasattr(self, "export_status") or not hasattr(self, "finding_rows"):
            return
        same = r.get("applied_threshold") is not None and abs(float(r["applied_threshold"]) - self.threshold()) < 1e-6
        same = same and sorted(r.get("clip_codes") or []) == self.clip_codes() and sorted(r.get("add_codes") or []) == self.add_codes()
        if self.zone_info:
            same = same and [round(t, 2) for t in (r.get("zone_thresholds") or [])[1:]] == [round(t, 2) for t in self.zone_thresholds()[1:]]
        if same:
            self.export_status.setText("Mask finalized. Choose what to export.")
        else:
            left = r.get("left_out") or {}; added = r.get("added") or {} if r.get("added_pixels") else {}
            self.export_status.setText("These exports use the mask finalized at {:.2f}{}{}. The Review tab shows other settings now: "
                                       "press Finalize mask there to export those.".format(
                                           float(r.get("applied_threshold") or 0), " without " + ", ".join(left) if left else "",
                                           " with " + ", ".join(added) if added else ""))

    def _show_curve(self):
        if not self.curve_points:
            return
        dialog = ThresholdCurveDialog(self.curve_points, self.threshold(), self)
        dialog.set_suggestion((self.result or {}).get("suggestion") or {})
        dialog.threshold_selected.connect(lambda threshold: self.threshold_slider.setValue(round(threshold * 100)))
        dialog.finished.connect(lambda _result: setattr(self, "curve_dialog", None))
        self.curve_dialog = dialog
        dialog.show()

    def set_running(self, running, action="run"):
        self._busy = bool(running)
        if running:
            self._action = action
        self.generate_button.setEnabled(not running)
        self.apply_button.setEnabled(not running and bool(self.result))
        self.cancel_button.setVisible(running and not str(action).startswith("batch"))
        self.continue_button.setEnabled(not running)
        for button in (self.export_bands_button, self.export_vis_button, self.export_all_button, self.aoi_export_button):
            button.setEnabled(not running and bool(self.result.get("mask_path")))
        self._refresh_goal_buttons()
        self._refresh_draw_buttons()
        self.add_crop_button.setEnabled(not running and bool(self.result.get("vote_path")))
        batch_action = str(getattr(self, "_action", "")).startswith("batch")
        self.batch_cancel_button.setVisible(running and batch_action)
        self.batch_run_button.setEnabled(not running and bool(getattr(self, "_batch_items_found", None)))
        self.batch_load_button.setEnabled(not running)
        for b in (self.batch_finalize_one, self.batch_next_inline, self.batch_slider): b.setEnabled(not running)
        if self.batch: self._fill_batch_table()
        if running and (action in ("zones", "count", "training", "labels_create", "labels_write") or str(action).startswith("export") or str(action).startswith("batch")):
            bar = self._bar_for(action)
            bar.setValue(0)
            bar.setFormat("Starting…")
        elif running:
            self.progress.setValue(0)
            self.progress.setFormat("Starting…")
            if action == "run":
                self.run_summary.setText("PPE is processing the imagery in the background.")

    def _bar_for(self, action):
        action = str(action)
        if action.startswith("batch"): return self.batch_progress
        if action == "count": return self.count_progress
        if action == "training": return self.train_progress
        if action.startswith("labels"): return self.label_progress
        if action == "export_aoi": return self.aoi_progress
        if action == "zones": return self.crop_progress
        if action.startswith("export"): return self.export_progress
        return self.progress

    def set_progress(self, percent, message):
        bar = self._bar_for(getattr(self, "_action", "run"))
        bar.setValue(percent)
        bar.setFormat("{}% · {}".format(percent, message))

    def append_log(self, message):
        if message:
            self.log_view.appendPlainText(message)

    def set_results(self, result, curve_points):
        if self.batch and self.batch.get("open"):
            self._batch_save_open(); self.batch["open"] = None
        if hasattr(self, "batch_banner"): self.batch_banner.setVisible(False)
        if hasattr(self, "batch_review_box"): self.batch_review_box.setVisible(False)
        self.pale_layer_removed()                 # a new run removes the old findings layer
        self._close_pale_card()
        for row in self.finding_rows.values():
            row["clip"].blockSignals(True); row["clip"].setChecked(False); row["clip"].blockSignals(False)
        self.result = dict(result)
        self.curve_points = list(curve_points)
        threshold = float(result.get("threshold", 0.5))
        self.threshold_slider.blockSignals(True)
        self.threshold_slider.setValue(round(threshold * 100))
        self.threshold_slider.blockSignals(False)
        self.threshold_slider.setEnabled(True)
        self.curve_button.setEnabled(bool(curve_points))
        suggestion = result.get("suggestion") or {}
        if suggestion:
            self.suggestion_label.setText(
                "Suggested threshold {:.2f} (good range {:.2f}–{:.2f}). The slider starts here: it is the value whose "
                "mask best matches the pixels that are at least half plant in this image. Check it on the map and "
                "adjust if needed.".format(suggestion["threshold"], suggestion["low"], suggestion["high"]))
            self.suggestion_button.setText("Back to suggested threshold ({:.2f})".format(suggestion["threshold"]))
            self.suggestion_button.setVisible(True); self.suggestion_button.setEnabled(True)
        else:
            note = result.get("suggestion_note") or "not enough clearly plant and clearly soil pixels"
            self.suggestion_label.setText("No suggested threshold for this image ({}). The slider starts at 0.50.".format(note))
            self.suggestion_button.setVisible(False); self.suggestion_button.setEnabled(False)
        self.apply_button.setEnabled(True)
        self.review_button.setEnabled(True)
        self.progress.setValue(100)
        self.progress.setFormat("Complete")
        self.threshold_value.setText("{:.2f}".format(threshold))
        canopy = canopy_at(curve_points, threshold)
        self.canopy_value.setText("—" if canopy is None else "{:.1f}%".format(canopy))
        timing_values = result.get("timings", {}) or {}
        timing_note = ""
        if timing_values:
            timing_note = "\nTiming: " + " · ".join(
                "{} {:.1f}s".format(name, float(seconds))
                for name, seconds in timing_values.items()
            )
        timing_note += "\nProcessing threads: {} (automatically detected)".format(result.get("processing_threads", self.thread_count))
        guards = []
        if result.get("shadow_guard", False):
            guards.append("shadows {:.1f}%".format(float(result.get("shadow_rejected_pct", 0.0))))
        if result.get("biofilm_guard", False):
            guards.append("algae {:.1f}%".format(float(result.get("biofilm_rejected_pct", 0.0))))
        if result.get("colour_guard", False):
            guards.append("grey/neutral objects {:.1f}%".format(float(result.get("colour_rejected_pct", 0.0))))
        if result.get("bright_guard", False):
            guards.append("bright soil {:.1f}%".format(float(result.get("bright_rejected_pct", 0.0))))
        proxy_note = ""
        if result.get("hsi_proxies"):
            proxy_note = "\nBands used: " + ", ".join(
                "{} {:.0f} nm".format(item["name"], float(item["wavelength"])) for item in result["hsi_proxies"]
            )
        warning = ""
        if result.get("one_class_warning") == "soil":
            warning = "Check the mask: this image looks like bare soil only.\n"
        elif result.get("one_class_warning") == "plant":
            warning = "Check the mask: this image looks like closed canopy only.\n"
        if result.get("weak_green_warning"):
            warning += "Check the mask: plants are barely greener than soil in this image.\n"
        if result.get("weak_split_warning"):
            warning += "Check the mask: plants and ground do not separate clearly (separation {:.2f}).\n".format(float(result.get("split_separability", 0.0)))
        names = {"ndvi": "NDVI", "ndre": "NDRE", "gndvi": "GNDVI", "ndrer": "NDRER", "arvi": "ARVI", "gli": "GLI", "ngrdi": "NGRDI", "vari": "VARI"}
        layers = ", ".join(names.get(n, n.upper()) for n in result.get("selected_layers", [])) or "—"
        seconds = float((result.get("timings", {}) or {}).get("total", 0.0))
        self.run_summary.setText(
            "{}Canopy at {:.2f}{}: {}\nLayers: {} · {} votes\nMap: {}{}\nGuards removed: {}\nPPE {} · {}".format(
                warning, threshold, " (suggested)" if result.get("suggestion") else "", "—" if canopy is None else "{:.1f} %".format(canopy),
                layers, result.get("n_voters", "—"),
                result.get("display_mode") or ("true colour" if result.get("true_rgb", False) else "single band"),
                proxy_note.replace("\nBands used:", "\nBands:"),
                ", ".join(guards) if guards else "none",
                str(result.get("ppe_version", "—")).replace("native-", ""), ("{:.1f} s".format(seconds) if seconds < 10 else "{:.0f} s".format(seconds)),
            )
        )
        self.append_log("Run folder: {}".format(result.get("run_dir", "")))
        self.zone_info = None
        self.add_crop_button.setEnabled(True)
        self._refresh_draw_buttons()
        for row in self.crop_rows:
            row["slider"].blockSignals(True); row["slider"].setValue(round(threshold * 100)); row["slider"].blockSignals(False)
            row["value"].setText("{:.2f}".format(threshold))
        self.crops_note.setText(self._crops_hint())
        self._update_stage_tip()
        if self._drawn_rows():
            QTimer.singleShot(300, self._request_zones)      # the vote changed: rebuild the crop preview
        self.applied_status.setText(("⚠ " + warning.strip().replace("\n", "\n⚠ ") + "\n\n" if warning else "") +
                                    "Move the slider and watch the map. When the mask looks right, finalize it.")
        self._refresh_goal_buttons()

    def _use_suggestion(self):
        suggestion = (self.result or {}).get("suggestion") or {}
        if suggestion:
            self.threshold_slider.setValue(round(float(suggestion["threshold"]) * 100))

    def set_applied(self, result):
        for key in ("zone_path", "zone_thresholds", "crops", "rest_canopy_pct", "run_record", "added", "add_codes", "left_out", "clip_codes"):
            self.result.pop(key, None)
        self.result.update(result)
        self.result["applied_threshold"] = result.get("threshold")
        if result.get("crops"):
            lines = ["Mask finalized · canopy {:.1f} % of the image".format(float(result.get("canopy_pct", 0.0)))]
            for crop in result["crops"]:
                lines.append("  {} at {:.2f}: {}".format(crop["crop"], crop["threshold"],
                             "no pixels" if crop["canopy_pct"] is None else "{:.1f} % canopy".format(crop["canopy_pct"])))
            if result.get("rest_canopy_pct") is not None:
                lines.append("  rest of the image at {:.2f}: {:.1f} % canopy".format(float(result.get("threshold", 0.5)), result["rest_canopy_pct"]))
        else:
            lines = ["Mask finalized at {:.2f} · canopy {:.1f} %".format(float(result.get("threshold", self.threshold())), float(result.get("canopy_pct", 0.0)))]
        if result.get("left_out"):
            lines.append("  left out of the mask: {} ({:.2f} % of the image; untick and finalize again to put them back)".format(
                ", ".join(result["left_out"]), float(result.get("left_out_pct") or 0.0)))
        if result.get("added_pixels"):
            lines.append("  added to the mask: {} ({:,} pixels; untick and finalize again to take them out)".format(
                ", ".join(result.get("added") or {}), int(result["added_pixels"])))
        # a zero-width space after each / and _ lets a long path wrap instead of widening the panel
        lines.append(result.get("mask_path", "").replace("/", "/\u200b").replace("\\", "\\\u200b").replace("_", "_\u200b"))
        self.applied_status.setText("\n".join(lines))
        self._show_clip_result(result)
        for button in (self.export_bands_button, self.export_vis_button, self.export_all_button, self.aoi_export_button): button.setEnabled(True)
        self.export_status.setText("Mask finalized. Choose what to export.")
        self._refresh_goal_buttons()

    def set_exported(self, result):
        bar = self.aoi_progress if result.get("aoi_csv") else self.export_progress
        bar.setValue(100); bar.setFormat("Complete")
        if result.get("aoi_csv"):
            outside = result.get("plots_outside", 0)
            self.aoi_status.setText("Plot statistics written · {} plots{} · {} rows\n{}\n{}".format(
                result.get("plot_count", 0), " ({} outside the image)".format(outside) if outside else "",
                result.get("row_count", 0), result.get("canopy_csv", ""), result["aoi_csv"]))
        else:
            self.export_status.setText("Export complete · {} GeoTIFFs\n{}".format(result.get("export_count", 0), result.get("export_dir", "")))

    def show_error(self, message):
        QMessageBox.critical(self, "Plant Pixel Extractor", message)
        self._bar_for(getattr(self, "_action", "run")).setFormat("Failed")

    def closeEvent(self, event):
        self._save_settings()
        self.closing.emit()
        super().closeEvent(event)

    @staticmethod
    def _stylesheet():
        """Panel style. Every control that is styled at all is styled completely (frame, padding,
        arrow, indicator): a half-styled Qt control falls back to drawing parts of itself natively
        and comes out clipped or overlapping."""
        icons = os.path.join(os.path.dirname(__file__), "icons").replace("\\", "/")
        sheet = """
            QWidget { color: #1f3a30; font-size: 12px; }
            QWidget#ppeHost, QWidget#ppePage, QWidget#ppeViewport, QScrollArea { background: #f3f6f4; }
            QWidget#cardBody, QStackedWidget, QStackedWidget > QWidget { background: transparent; }

            QFrame#ppeHeader { background: #17473a; border-radius: 8px; }
            QLabel#ppeTitle { color: #ffffff; font-size: 15px; font-weight: 700; background: transparent; }
            QLabel#ppeSubtitle { color: #cfe2d8; font-size: 11px; background: transparent; }

            QLabel#sectionTitle { color: #143d30; font-size: 15px; font-weight: 700; }
            QLabel#ppeHint { color: #5d7268; }
            QLabel#cardTitle { color: #17473a; font-weight: 700; font-size: 12px; background: transparent; }

            QFrame#card { background: #ffffff; border: 1px solid #d9e3dd; border-radius: 8px; }
            QFrame#card QLabel { background: transparent; }
            QFrame#noteBox { background: #ffffff; border: 1px solid #d9e3dd; border-radius: 8px; }
            QFrame#accentBox { background: #e7f2ec; border: 1px solid #cfe4d8; border-radius: 8px; }
            QLabel#noteText { color: #23392f; background: transparent; }
            QLabel#accentText { color: #1d5a42; font-weight: 600; background: transparent; }
            QLabel#statCaption { color: #5d7268; font-size: 11px; font-weight: 600; }
            QLabel#statValue { color: #17734c; font-size: 24px; font-weight: 700; }
            QLabel#autoValue { color: #1d5a42; background: #eef5f1; border: 1px solid #d0e2d7; border-radius: 6px; padding: 6px 8px; }

            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
                color: #172d25; background: #ffffff; border: 1px solid #b9c9c0; border-radius: 6px;
                padding: 0px 8px; min-height: 28px; selection-background-color: #267b59; selection-color: #ffffff;
            }
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border: 1px solid #2b8560; }
            QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled { color: #8a9a92; background: #f1f4f2; }
            QComboBox { padding-right: 28px; }
            QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right; width: 26px; border: none; background: transparent; }
            QComboBox::down-arrow { image: url(@ICONS@/chevron_down.svg); width: 12px; height: 12px; }
            QComboBox QAbstractItemView { color: #172d25; background: #ffffff; border: 1px solid #b9c9c0; selection-background-color: #267b59; selection-color: #ffffff; outline: 0; }

            QCheckBox { spacing: 8px; padding: 3px 0px; background: transparent; }
            QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #8ea69a; border-radius: 4px; background: #ffffff; }
            QCheckBox::indicator:hover { border-color: #2b8560; }
            QCheckBox::indicator:checked { background: #1d7550; border-color: #1d7550; image: url(@ICONS@/check.svg); }
            QCheckBox::indicator:disabled { background: #eef2f0; border-color: #c9d4ce; }
            QCheckBox::indicator:checked:disabled { background: #a9c3b6; border-color: #a9c3b6; image: url(@ICONS@/check_disabled.svg); }

            QPushButton { min-height: 30px; padding: 0px 14px; border-radius: 6px; border: 1px solid #c3d3ca; background: #ffffff; color: #1f4d3b; font-weight: 600; }
            QPushButton:hover { border-color: #2b8560; background: #f4f9f6; }
            QPushButton[primary="true"] { background: #1d7550; color: #ffffff; border-color: #1d7550; }
            QPushButton[primary="true"]:hover { background: #17613f; }
            QPushButton[secondary="true"] { background: #eaf3ee; color: #1f5a42; border-color: #c9ddd1; }
            QPushButton[danger="true"] { color: #a13c3c; background: #fff5f5; border-color: #e8c3c3; }
            QPushButton#removeButton { padding: 0px; color: #8a4040; background: #ffffff; border-color: #e3caca; font-size: 13px; }
            QPushButton#removeButton:hover { background: #fff3f3; border-color: #c46a6a; }
            QPushButton#removeButton:disabled { color: #c3cbc7; background: #f4f6f5; border-color: #e1e7e4; }
            QPushButton:disabled, QPushButton[primary="true"]:disabled, QPushButton[secondary="true"]:disabled {
                color: #97a69e; background: #edf1ef; border-color: #dde5e0;
            }
            QPushButton[drawing="true"]:disabled {
                color: #3f4d47; background: #d5dcd8; border: 1px dashed #7d8f86; font-style: italic; font-weight: 600;
            }
            QLabel[drawing="true"], QLabel#ppeHint[drawing="true"] { color: #9a5b00; font-weight: 700; }
            QFrame#paleCard { background: #fff6dd; border: 1px solid #f0c35a; border-left: 5px solid #e6a100; border-radius: 8px; }
            QFrame#paleCard QLabel { background: transparent; }
            QToolButton#paleTitle { text-align: left; border: none; background: transparent; color: #7a4e00; font-weight: 700; font-size: 13px; padding: 3px 0px; }
            QToolButton#paleTitle:hover { color: #a86b00; }
            QFrame#paleCard[compact="true"] { background: #fffaf0; border: 1px solid #f1dcae; border-left: 3px solid #e9c46a; }
            QFrame#paleCard[compact="true"] QToolButton#paleTitle { font-size: 12px; }
            QLabel#paleText { color: #4a3a12; }
            QLabel#findingTitle { color: #6b4500; font-weight: 700; }

            QToolButton#sectionToggle { text-align: left; padding: 7px 10px; border: 1px solid #d9e3dd; border-radius: 8px; background: #ffffff; color: #17473a; font-weight: 700; }
            QToolButton#sectionToggle:hover { border-color: #2b8560; }
            QToolButton#sectionToggle:checked { background: #f7faf8; }

            QProgressBar { border: 1px solid #d3dfd8; border-radius: 6px; background: #e8eeeb; text-align: center; color: #1f3a30; min-height: 20px; max-height: 20px; font-size: 11px; }
            QProgressBar::chunk { background: #8fcfae; border-radius: 5px; }

            QTableWidget { color: #1f3a30; background: #ffffff; alternate-background-color: #f6f9f7; gridline-color: #e3ebe6; border: 1px solid #d9e3dd; border-radius: 6px; selection-background-color: #cfe5da; selection-color: #17473a; }
            QTableWidget::item { padding: 2px 4px; }
            QTableWidget::item:selected { background: #cfe5da; color: #17473a; }
            QHeaderView::section { color: #1f3a30; background: #e8eeeb; border: none; border-right: 1px solid #d9e3dd; border-bottom: 1px solid #d9e3dd; padding: 4px 6px; font-weight: 600; }
            QTableCornerButton::section { background: #e8eeeb; border: none; }
            QPlainTextEdit { color: #23392f; background: #ffffff; border: 1px solid #d9e3dd; border-radius: 6px; padding: 4px; font-family: Menlo, Consolas, monospace; font-size: 10px; }

            QTabWidget::pane { border: none; }
            QTabBar { background: transparent; }
            QTabBar::tab { padding: 7px 4px; margin: 0px; color: #5d7268; background: transparent; border: none; border-bottom: 2px solid #dbe4df; }
            QTabBar::tab:hover { color: #17473a; }
            QTabBar::tab:selected { color: #17613f; border-bottom: 2px solid #1d7550; font-weight: 700; }

            QSlider::groove:horizontal { height: 6px; border-radius: 3px; background: #dbe5df; }
            QSlider::sub-page:horizontal { background: #2c9367; border-radius: 3px; }
            QSlider::handle:horizontal { background: #ffffff; border: 2px solid #1d7550; width: 16px; height: 16px; margin: -7px 0px; border-radius: 9px; }
            QSlider::handle:horizontal:disabled { border-color: #b7c7bf; }

            QScrollBar:vertical { background: transparent; width: 8px; margin: 0px; }
            QScrollBar::handle:vertical { background: #c4d2ca; border-radius: 4px; min-height: 30px; }
            QScrollBar::handle:vertical:hover { background: #9fb5a9; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical, QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { height: 0px; background: transparent; }

            QToolTip { color: #ffffff; background-color: #17473a; border: 1px solid #4e806b; padding: 6px; }
            QDialog, QMessageBox { background-color: #f7faf8; }
            QDialog QLabel, QMessageBox QLabel { color: #172d25; background: transparent; }
        """
        return sheet.replace("@ICONS@", icons)

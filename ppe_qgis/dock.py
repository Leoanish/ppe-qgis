"""Plant Pixel Extractor dock interface."""

import os
from statistics import median

from qgis.PyQt.QtCore import QSettings, QStandardPaths, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import (
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
from .layer_utils import canopy_at, load_plot_layer, plot_layer, remove_role, set_plot_labels
from .native_engine import (BAND_LABELS, PLOT_FILE_FILTER, dataset_wavelengths, describe_image, display_channels,
                            guess_plot_id_field, open_plot_file, plots_over_image, scan_band_folder)


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
        root.addWidget(header)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setUsesScrollButtons(False)       # five short labels always fit; no hidden tabs
        self.tabs.tabBar().setExpanding(True)
        self.tabs.addTab(self._input_tab(), "1 Image")
        self.tabs.addTab(self._generate_tab(), "2 Run")
        self.tabs.addTab(self._review_tab(), "3 Review")
        self.tabs.addTab(self._export_tab(), "4 Export")
        self.tabs.addTab(self._aoi_export_tab(), "5 Plots")
        for index, tip in enumerate(("Choose the image and check its bands", "Build the plant vote", "Set the threshold and finalize the mask",
                                     "Export plant-only bands and indices", "Statistics for each plot polygon")):
            self.tabs.setTabToolTip(index, tip)
        root.addWidget(self.tabs, 1)
        self.setWidget(host)
        # The sheet goes on the content widget. Set on the QDockWidget itself it has no effect
        # inside QGIS (the main window carries its own sheet), and the panel shows unstyled.
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
        box.addWidget(label)
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

        layout.addWidget(self._crop_draw_box())
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
            pass
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
        self.continue_button.clicked.connect(lambda: self.tabs.setCurrentIndex(1))
        self.review_button.clicked.connect(lambda: self.tabs.setCurrentIndex(2))
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
        self.ortho_type.setToolTip(auto_text if not found.get("bands") else auto_text + ": " + self._band_list(found["bands"]))
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
        self._image_grid_path = None              # drawing waits until the new image is on the map
        self._clear_crops(remove_layer=True)
        self._update_review_crops_note()
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
        self.add_crop_button.setEnabled(not self._busy); self.crops_note.setText(self._crops_hint()); self._refresh_draw_buttons()

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

    def _save_settings(self):
        prefix = "ppe_qgis/"
        self.settings.setValue(prefix + "output", self.output_path.text().strip())
        self.settings.setValue(prefix + "work", self.work_path.text().strip())
        self.settings.setValue(prefix + "rgb_order", self.rgb_order.currentData())
        self.settings.setValue(prefix + "hsi_min_wavelength", self.hsi_min_wavelength.value())
        self.settings.setValue(prefix + "hsi_max_wavelength", self.hsi_max_wavelength.value())
        self.settings.setValue(prefix + "hsi_interval", self.hsi_interval.value())

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
        if action == "run":
            drawn = self._drawn_rows()
            config["crop_areas"] = [{"name": row["name"].text().strip() or "crop {}".format(i + 1), "wkt": row["geometry"].asWkt()}
                                    for i, row in enumerate(drawn)]
            config["separate_areas"] = bool(drawn) and self.separate_areas.isChecked()
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
                "findings_shares": {self.FINDING_TEXT[k][1]: round(v, 3) for k, v in self.finding_shares_now().items()},
                "separate_areas": bool(self.result.get("area_runs")),
                "area_runs": self.result.get("area_runs") or [],
                "pale_path": self.result.get("pale_plant_path", ""),
            })
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
                           "left_out": list((self.result.get("left_out") or {}).keys())})
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
        3: ("●", "Shaded leaves", "Darker leaves under the top of the canopy. They are plant: they reflect more near-infrared light than bare soil can."),
        4: ("?", "Unclear", "Too dark to tell soil from leaf. You can leave them out of the mask."),
    }

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
        intro = QLabel("Information only: nothing here changes the mask unless you tick <b>Leave out of the mask</b>. "
                       "Untick to put them back.")
        intro.setObjectName("paleText"); intro.setWordWrap(True); intro.setTextFormat(Qt.RichText); body.addWidget(intro)
        self.finding_rows = {}
        for code in (1, 2, 3, 4):
            holder = QWidget(); line = QVBoxLayout(holder); line.setContentsMargins(0, 0, 0, 0); line.setSpacing(2)
            title = QLabel(""); title.setObjectName("findingTitle"); title.setWordWrap(True)
            note = QLabel(""); note.setObjectName("paleText"); note.setWordWrap(True)
            clip = QCheckBox("Leave out of the mask")
            clip.toggled.connect(lambda on, code=code: self._clip_toggled(code, on))
            line.addWidget(title); line.addWidget(note); line.addWidget(clip)
            body.addWidget(holder); holder.setVisible(False)
            self.finding_rows[code] = {"holder": holder, "title": title, "note": note, "clip": clip}
        self.clip_status = QLabel(""); self.clip_status.setObjectName("paleText"); self.clip_status.setWordWrap(True); self.clip_status.setVisible(False)
        body.addWidget(self.clip_status)
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
        if self._early_stage():
            shares[2] += shares[1]; shares[1] = 0.0
        return shares

    def clip_codes(self):
        """Finding codes left out of the mask (leaf edges include plant parts early in the season)."""
        codes = [code for code, row in self.finding_rows.items() if row["clip"].isChecked()]
        if 2 in codes and self._early_stage(): codes.append(1)
        return sorted(set(codes))

    def _clip_toggled(self, code, on):
        self._update_clip_status()
        self._threshold_changed(self.threshold_slider.value())
        if self.result.get("mask_path") and not getattr(self, "_restoring_clips", False):
            # The mask is already finalized: write it again at once, so ticking leaves pixels out and
            # unticking puts them back. Nothing is lost either way: the mask is rebuilt from the vote.
            if self._busy:
                self.clip_status.setText(self.clip_status.text() + " (PPE is busy: press Finalize mask when it is done.)")
            else:
                self._auto_apply = True
                self._emit_apply()

    def _put_all_back(self):
        for row in self.finding_rows.values():
            row["clip"].blockSignals(True); row["clip"].setChecked(False); row["clip"].blockSignals(False)
        self._clip_toggled(0, False)

    def _update_clip_status(self):
        names = [self.FINDING_TEXT[c][1].lower() for c in (1, 2, 3, 4) if self.finding_rows[c]["clip"].isChecked()]
        if names:
            shares = self.finding_shares_now(); left = sum(shares.get(c, 0.0) for c in self.clip_codes())
            self.clip_status.setText("Left out of the mask: {} ({:.1f} % of the plant at {:.2f}). Untick to put them back.".format(
                ", ".join(names), left, self.threshold()))
        self.clip_status.setVisible(bool(names)); self.put_back_button.setVisible(bool(names))

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
                row["title"].setText("{}  {} — {:.1f} % of the plant".format(mark, name, share))
                row["note"].setText(note.format(parts=parts))
                shown.append(name)
        if not shown:
            self.pale_frame.setVisible(False)
            if self.pale_button.isChecked(): self.pale_button.setChecked(False)
            return
        self._set_pale_style(True)
        self.pale_title.setText("✿  What PPE found: {} thing{} to look at".format(len(shown), "" if len(shown) == 1 else "s"))
        self.pale_title.setToolTip("; ".join(shown) + ". Click to open or close.")
        self._update_clip_status()
        self.pale_frame.setVisible(True)

    # ---- crops in this image: one outline and one slider per crop (Review tab) ------------------
    WIDE_OUTLINE = ("Draw each outline wide: take in the crop and some bare soil around it (an alley, a headland, "
                    "a bare strip), so PPE finds both plant and soil inside it.")

    def _image_ready(self):
        return bool(self.result.get("vote_path") or getattr(self, "_image_grid_path", None))

    def _crops_hint(self):
        if not self._image_ready():
            return "Choose an image first. Then, for an image with more than one crop, add each crop and draw its outline on the map."
        return ("More than one crop in the image? Add each crop and draw its outline on the map before Run (click the corners, "
                "right-click to finish, Esc to cancel). " + self.WIDE_OUTLINE + " Each crop gets its own slider on tab 3 (Review).")

    def _crop_draw_box(self):
        """Tab 1: add the crops and draw their outlines before Run (PPE 4.0)."""
        frame = QFrame(); frame.setObjectName("card")
        box = QVBoxLayout(frame); box.setContentsMargins(14, 12, 14, 12); box.setSpacing(6)
        title = QLabel("Crops in this image (optional)"); title.setObjectName("cardTitle"); box.addWidget(title)
        self.crops_note = QLabel(self._crops_hint()); self.crops_note.setWordWrap(True); self.crops_note.setObjectName("ppeHint")
        box.addWidget(self.crops_note)
        self.crop_list = QVBoxLayout(); self.crop_list.setSpacing(8); box.addLayout(self.crop_list)
        buttons = QHBoxLayout()
        self.add_crop_button = QPushButton("+ Add crop"); self.add_crop_button.setProperty("secondary", True); self.add_crop_button.setEnabled(False)
        buttons.addWidget(self.add_crop_button); buttons.addStretch(); box.addLayout(buttons)
        self.separate_areas = QCheckBox("Fit the thresholds inside each crop area on its own")
        self.separate_areas.setChecked(True)
        self.separate_areas.setToolTip("On: Run fits the whole PPE process again inside each outline (each crop gets its own layers and "
                                       "thresholds). Off: one run for the whole field, as in PPE 3.9; the crop sliders still apply.")
        self.separate_areas.setVisible(False)
        box.addWidget(self.separate_areas)
        self.separate_hint = QLabel("On: each crop area gets its own layers and thresholds (takes a few seconds per area). "
                                    "Off: one run for the whole field, as in PPE 3.9. Run both ways to compare.")
        self.separate_hint.setWordWrap(True); self.separate_hint.setObjectName("ppeHint"); self.separate_hint.setVisible(False)
        box.addWidget(self.separate_hint)
        return frame

    def _crops_box(self):
        """Tab 3: one slider per drawn crop."""
        frame = QFrame(); frame.setObjectName("card")
        box = QVBoxLayout(frame); box.setContentsMargins(14, 12, 14, 12); box.setSpacing(6)
        title = QLabel("Crops in this image"); title.setObjectName("cardTitle"); box.addWidget(title)
        self.review_crops_note = QLabel("Crops are added and drawn on tab 1 (Image), before Run. Each drawn crop then gets its own slider here.")
        self.review_crops_note.setWordWrap(True); self.review_crops_note.setObjectName("ppeHint")
        self.review_crops_note.setTextFormat(Qt.RichText); box.addWidget(self.review_crops_note)
        self.stage_tip = QLabel(""); self.stage_tip.setWordWrap(True); self.stage_tip.setObjectName("ppeHint"); self.stage_tip.setVisible(False)
        box.addWidget(self.stage_tip)
        self.crop_slider_list = QVBoxLayout(); self.crop_slider_list.setSpacing(8); box.addLayout(self.crop_slider_list)
        self.same_threshold = QCheckBox("Use the slider above for every crop"); self.same_threshold.setVisible(False)
        box.addWidget(self.same_threshold)
        self.crop_progress = QProgressBar(); self.crop_progress.setRange(0, 100); self.crop_progress.setValue(0); self.crop_progress.setFormat("Ready")
        self.crop_progress.setVisible(False); box.addWidget(self.crop_progress)
        return frame

    def _update_crop_options(self):
        drawn = bool(self._drawn_rows())
        self.separate_areas.setVisible(drawn); self.separate_hint.setVisible(drawn)

    def _update_review_crops_note(self):
        """Tab 3: how the crop areas were processed in the last run, and whether the outlines changed since."""
        runs = self.result.get("area_runs") or []
        if not self.result.get("vote_path"):
            text = "Crops are added and drawn on tab 1 (Image), before Run. Each drawn crop then gets its own slider here."
        elif runs:
            lines = ["<b>Each crop area was fitted on its own</b> (PPE 4.0):"]
            for run in runs:
                soil = run.get("soil_pct")
                warn = (" ⚠ only {:.1f} % bare soil inside: draw it wider, or run again with the separate fit off and compare".format(soil)
                        if soil is not None and soil < 10 else (" · bare soil {:.1f} %".format(soil) if soil is not None else ""))
                lines.append("• <b>{}</b>: layers {}; slider starts at its own suggestion {}{}".format(
                    run["crop"], ", ".join(run.get("chosen_layers") or []),
                    "{:.2f}".format(run["suggestion"]) if run.get("suggestion") is not None else "(none: 0.50)", warn))
            lines.append("The rest of the image keeps the whole-field run (slider above).")
            if getattr(self, "_outlines_changed", False):
                lines.append("<b>⚠ The outlines changed after Run: press Run again so the thresholds are fitted inside the new outlines.</b>")
            text = "<br>".join(lines)
        elif self._drawn_rows():
            text = ("One run for the whole field; each crop slider sets the threshold inside its outline. "
                    "To fit each area on its own, tick the option on tab 1 and Run again.")
        else:
            text = "No crop areas. To give crops their own threshold, add and draw them on tab 1 (Image) and Run again."
        self.review_crops_note.setText(text)

    def _add_crop_row(self, name=""):
        frame = QFrame(); grid = QGridLayout(frame); grid.setContentsMargins(0, 0, 0, 0); grid.setHorizontalSpacing(6); grid.setVerticalSpacing(2)
        edit = QLineEdit(name); edit.setPlaceholderText("Crop, e.g. corn")
        draw = QPushButton("Draw area"); draw.setProperty("secondary", True); draw.setEnabled(self._draw_allowed())
        status = QLabel("not drawn"); status.setObjectName("ppeHint")
        remove = QToolButton(); remove.setText("✕"); remove.setToolTip("Remove this crop and its outline")
        grid.addWidget(edit, 0, 0); grid.addWidget(draw, 0, 1); grid.addWidget(status, 0, 2); grid.addWidget(remove, 0, 3)
        grid.setColumnStretch(0, 1)
        slider_frame = QFrame(); sgrid = QGridLayout(slider_frame); sgrid.setContentsMargins(0, 0, 0, 0); sgrid.setHorizontalSpacing(6); sgrid.setVerticalSpacing(2)
        name_label = QLabel(name or "crop"); name_label.setObjectName("cardTitle")
        slider = QSlider(Qt.Horizontal); slider.setRange(15, 95); slider.setValue(self.threshold_slider.value()); slider.setEnabled(False)
        value = QLabel("{:.2f}".format(slider.value() / 100.0)); canopy = QLabel("—"); canopy.setObjectName("ppeHint")
        sgrid.addWidget(name_label, 0, 0, 1, 3); sgrid.addWidget(slider, 1, 0); sgrid.addWidget(value, 1, 1); sgrid.addWidget(canopy, 1, 2)
        sgrid.setColumnStretch(0, 1)
        slider_frame.setVisible(False)
        row = {"frame": frame, "slider_frame": slider_frame, "name_label": name_label, "name": edit, "draw": draw, "status": status,
               "remove": remove, "slider": slider, "value": value, "canopy": canopy, "geometry": None}
        edit.textChanged.connect(lambda text, label=name_label: label.setText(text.strip() or "crop"))
        self.crop_rows.append(row); self.crop_list.addWidget(frame); self.crop_slider_list.addWidget(slider_frame)
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
        path = self.result.get("vote_path") or getattr(self, "_image_grid_path", None) or ""
        ds = gdal.Open(path) if path else None
        return ds.GetProjection() if ds is not None else ""

    def _draw_allowed(self):
        return self._image_ready() and not self._busy and self._drawing_row is None

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
                                "Esc cancels, Backspace undoes the last corner. Draw it wide: take in the crop and some bare soil "
                                "around it, so PPE finds both plant and soil inside.".format(name))
        self._refresh_draw_buttons()
        bar = self.iface.messageBar()
        self._draw_message = bar.createMessage("Plant Pixel Extractor", "Drawing the area of {}: click its corners on the map, "
                                               "right-click to finish, Esc to cancel. Draw it wide, with some bare soil around the crop.".format(name))
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
        self._update_crop_options(); self._mark_outlines_changed()
        self._request_zones()

    def _mark_outlines_changed(self):
        if self.result.get("area_runs"):
            self._outlines_changed = True
            self.crops_note.setText("⚠ The outlines changed after Run: press Run again so PPE fits the thresholds inside them. " + self.WIDE_OUTLINE)
        self._update_review_crops_note()

    def _remove_crop(self, row):
        if self._busy:
            self._busy_warning(); return
        had_area = row["geometry"] is not None
        self.crop_rows.remove(row)
        for key in ("frame", "slider_frame"):
            row[key].setParent(None); row[key].deleteLater()
        self.same_threshold.setVisible(len(self.crop_rows) > 1)
        self._update_crop_options(); self._mark_outlines_changed()
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
        for row in rows:
            row["slider"].setEnabled(not self.same_threshold.isChecked()); row["slider_frame"].setVisible(True)
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
            for key in ("frame", "slider_frame"):
                row[key].setParent(None); row[key].deleteLater()
        self.crop_rows = []; self.zone_info = None
        self._outlines_changed = False
        self._update_crop_options()
        self.same_threshold.setChecked(False); self.same_threshold.setVisible(False)
        self.crop_progress.setVisible(False); self.add_crop_button.setEnabled(False)
        self.crops_note.setText(self._crops_hint())
        if remove_layer:
            try: remove_role("crops")
            except Exception: pass

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
        left = sum(self.finding_shares_now().get(c, 0.0) for c in self.clip_codes()) if hasattr(self, "finding_rows") else 0.0
        if canopy is not None and left:
            canopy = canopy * (1.0 - left / 100.0)          # the left-out share is a share of the plant
        self.canopy_value.setText("—" if canopy is None else "{:.1f}%".format(canopy))
        if hasattr(self, "finding_rows") and self.pale_frame.isVisible():
            self._update_pale_card()
        if self.curve_dialog is not None:
            self.curve_dialog.set_threshold(threshold)

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
        self.cancel_button.setVisible(running)
        self.continue_button.setEnabled(not running)
        for button in (self.export_bands_button, self.export_vis_button, self.export_all_button, self.aoi_export_button):
            button.setEnabled(not running and bool(self.result.get("mask_path")))
        self._refresh_draw_buttons()
        self.add_crop_button.setEnabled(not running and self._image_ready())
        if running and (action == "zones" or str(action).startswith("export")):
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
        own = {run["crop"]: run.get("suggestion") for run in (result.get("area_runs") or [])}
        self._outlines_changed = False
        for row in self.crop_rows:
            start = own.get(row["name"].text().strip()) or threshold
            row["slider"].blockSignals(True); row["slider"].setValue(round(start * 100)); row["slider"].blockSignals(False)
            row["value"].setText("{:.2f}".format(start))
            row["slider_frame"].setVisible(row["geometry"] is not None)
        self.crops_note.setText(self._crops_hint())
        self._update_review_crops_note()
        self._update_stage_tip()
        if self._drawn_rows():
            QTimer.singleShot(300, self._request_zones)      # the vote changed: rebuild the crop preview
        self.applied_status.setText(("⚠ " + warning.strip().replace("\n", "\n⚠ ") + "\n\n" if warning else "") +
                                    "Move the slider and watch the map. When the mask looks right, finalize it.")

    def _use_suggestion(self):
        suggestion = (self.result or {}).get("suggestion") or {}
        if suggestion:
            self.threshold_slider.setValue(round(float(suggestion["threshold"]) * 100))

    def set_applied(self, result):
        for key in ("zone_path", "zone_thresholds", "crops", "rest_canopy_pct", "run_record"):
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
            lines.append("  left out of the mask: {} ({:.2f} % of the image; untick on this tab to put them back)".format(
                ", ".join(result["left_out"]), float(result.get("left_out_pct") or 0.0)))
        lines.append(result.get("mask_path", ""))
        self.applied_status.setText("\n".join(lines))
        for button in (self.export_bands_button, self.export_vis_button, self.export_all_button, self.aoi_export_button): button.setEnabled(True)
        self.export_status.setText("Mask finalized. Choose what to export.")

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

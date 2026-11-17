"""Plant Pixel Extractor dock interface."""

import os
from statistics import median

from qgis.PyQt.QtCore import QSettings, QStandardPaths, Qt, pyqtSignal
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
from qgis.core import QgsProject
from osgeo import gdal

from .curve_widget import ThresholdCurveDialog
from .layer_utils import canopy_at
from .native_engine import BAND_LABELS, dataset_wavelengths, describe_image, display_channels


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
        self.threads = QLabel("Automatic · {} of {} logical CPUs".format(self.thread_count, logical_cpus))
        self.threads.setObjectName("autoValue")
        self.threads.setToolTip("PPE detects the computer automatically, reserves capacity for QGIS, and caps concurrent GDAL work at 16 to avoid excessive memory and disk contention.")
        self.sample_size = QSpinBox()
        self.sample_size.setRange(1000, 2000000)
        self.sample_size.setSingleStep(5000)
        self.sample_size.setValue(100000)
        self.sample_size.setGroupSeparatorShown(True)
        self._plain_spin(self.sample_size)
        self.hsi_min_wavelength = QDoubleSpinBox(); self.hsi_min_wavelength.setRange(0, 2500); self.hsi_min_wavelength.setValue(0); self.hsi_min_wavelength.setSuffix(" nm")
        self.hsi_min_wavelength.setSpecialValueText("Read metadata")
        self.hsi_max_wavelength = QDoubleSpinBox(); self.hsi_max_wavelength.setRange(0, 2500); self.hsi_max_wavelength.setValue(0); self.hsi_max_wavelength.setSuffix(" nm")
        self.hsi_max_wavelength.setSpecialValueText("Read metadata")
        self.hsi_interval = QDoubleSpinBox(); self.hsi_interval.setRange(0, 100); self.hsi_interval.setDecimals(3); self.hsi_interval.setValue(0); self.hsi_interval.setSuffix(" nm")
        self.hsi_interval.setSpecialValueText("Read metadata")
        for spin in (self.hsi_min_wavelength, self.hsi_max_wavelength, self.hsi_interval):
            self._plain_spin(spin)
        self.bright_guard = QCheckBox("Bright soil and residue")
        self.bright_guard.setToolTip(
            "Rejects pixels that are very bright, nearly colourless and show no vegetation signal. Off by default."
        )
        self.shadow_guard = QCheckBox("Dark shadows")
        self.shadow_guard.setToolTip(
            "Rejects dark pixels (the darkest 15 % of the image) unless they still reflect enough "
            "near-infrared light to be a lit leaf. Untick it to keep shaded parts of the canopy."
        )
        self.biofilm_guard = QCheckBox("Algae on the soil (needs red edge)")
        self.biofilm_guard.setToolTip(
            "Algae, moss and biofilm on wet soil look green but lack a leaf's rise from red edge to NIR. "
            "Rejects pixels whose NDRE is no higher than bare soil's. Used only when the image has red-edge and NIR bands."
        )
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

        guard_box, guard_body = self._card("Reject pixels that are not plants")
        guard_layout = QVBoxLayout(guard_body)
        guard_layout.setContentsMargins(0, 0, 0, 0)
        guard_layout.setSpacing(6)
        for box in (self.shadow_guard, self.biofilm_guard, self.bright_guard):
            guard_layout.addWidget(box)
        layout.addWidget(guard_box)

        advanced_body = QWidget()
        advanced_form = self._form(advanced_body)
        self.adaptation_mode = QComboBox()
        self.adaptation_mode.addItem("Follow local conditions", "adaptive")
        self.adaptation_mode.addItem("One set for the whole field", "global")
        self.adaptation_mode.setToolTip(
            "Thresholds are fitted to the whole field. With 'Follow local conditions' they are then moved up or down "
            "in each part of the field to follow its own soil and plants, which keeps pale or small plants in weak plots."
        )
        self.half_mixture = QCheckBox("Add half-mixture as a fifth method")
        self.half_mixture.setToolTip(
            "Adds a threshold at the index value of a pixel that is half plant and half soil. "
            "Not part of the original four-method design. It helps most in dense crops, where Otsu and k-means cut too high."
        )
        advanced_form.addRow("Thresholds", self.adaptation_mode)
        advanced_form.addRow(self.half_mixture)
        advanced_form.addRow("Fitting sample", self.sample_size)
        advanced_form.addRow("Threads", self.threads)
        self.sample_size.setToolTip("Pixels used to fit the thresholds. The mask is always full resolution.")
        layout.addWidget(self._collapsible("Advanced settings", advanced_body))

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

        self.curve_button = QPushButton("View threshold curve")
        self.curve_button.setProperty("secondary", True)
        self.curve_button.setEnabled(False)
        layout.addWidget(self.curve_button)

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
        detail = QLabel("Summarise the plant pixels inside each plot polygon: every band and vegetation index (for hyperspectral images, every original band).")
        detail.setWordWrap(True); detail.setObjectName("ppeHint")
        layout.addWidget(title); layout.addWidget(detail)
        box, box_body = self._card("Plot boundaries"); form = self._form(box_body)
        self.aoi_path = QLineEdit(); self.aoi_path.setPlaceholderText("GeoPackage, Shapefile, GeoJSON or KML")
        form.addRow("Polygon file", self._aoi_path_row())
        self.aoi_id_field = QComboBox(); self.aoi_id_field.addItem("Automatic · feature ID", "")
        self.aoi_id_field.setToolTip("Choose the boundary attribute that identifies each plot. Automatic uses the vector feature ID.")
        form.addRow("Plot identifier", self.aoi_id_field); layout.addWidget(box)
        output_note = QLabel("Output: one CSV with plot ID, variable, source band, pixel count, mean, median, SD and CV (%), saved in the export folder of tab 4.")
        output_note.setWordWrap(True); output_note.setObjectName("ppeHint"); layout.addWidget(output_note)
        self.aoi_export_button = QPushButton("Export plot statistics")
        self.aoi_export_button.setProperty("primary", True); self.aoi_export_button.setEnabled(False)
        layout.addWidget(self.aoi_export_button)
        self.aoi_status = QLabel("Finalize the mask on tab 3 before exporting plot statistics.")
        layout.addWidget(self._note(self.aoi_status))
        layout.addStretch(); return self._scroll_tab(page)

    def _aoi_path_row(self):
        row = QWidget(); line = QHBoxLayout(row); line.setContentsMargins(0, 0, 0, 0); line.setSpacing(6)
        button = QPushButton("Browse…"); button.setProperty("secondary", True); button.clicked.connect(self._browse_aoi)
        button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed); self.aoi_path.textChanged.connect(self.aoi_path.setToolTip)
        line.addWidget(self.aoi_path, 1); line.addWidget(button); return row

    def _browse_aoi(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select polygon boundaries", self.aoi_path.text(), "Vector boundaries (*.gpkg *.shp *.geojson *.json *.kml);;All files (*)")
        if not path: return
        self.aoi_path.setText(path); self.aoi_id_field.clear(); self.aoi_id_field.addItem("Automatic · feature ID", "")
        try:
            from osgeo import ogr
            source = ogr.Open(path); layer = source.GetLayer(0) if source else None
            if layer:
                definition = layer.GetLayerDefn()
                for index in range(definition.GetFieldCount()):
                    name = definition.GetFieldDefn(index).GetName()
                    self.aoi_id_field.addItem(name, name)
        except Exception as err:
            self.append_log("Could not read the fields of the AOI file: {}".format(err))

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
                "folder_path": self.folder_path.text().strip(),
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
        self.stacked_path.clear(); self.folder_path.clear()
        for edit in self.band_edits.values():
            edit.clear()
        self.source_stack.setCurrentIndex(index)
        self._image_gone()
        self._refresh_source_detection()

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
        if self._has_source():
            self._refresh_source_detection()          # the remaining bands form a new image
        else:
            self._image_gone()

    def image_layer_removed(self):
        """The PPE image layer was deleted in the QGIS Layers panel: forget the image too, so the
        panel never refers to something that is no longer on the map."""
        self.stacked_path.clear(); self.folder_path.clear()
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
        self.threshold_slider.blockSignals(True); self.threshold_slider.setValue(50); self.threshold_slider.blockSignals(False)
        self.threshold_slider.setEnabled(False)
        self.threshold_value.setText("0.50"); self.canopy_value.setText("—")
        for button in (self.curve_button, self.apply_button, self.review_button, self.export_bands_button,
                       self.export_vis_button, self.export_all_button, self.aoi_export_button):
            button.setEnabled(False)
        self.progress.setValue(0); self.progress.setFormat("Ready")
        self.export_progress.setValue(0); self.export_progress.setFormat("Ready")
        self.run_summary.setText("No mask generated yet.")
        self.applied_status.setText("Run PPE first; the mask can then be reviewed here.")
        self.export_status.setText("Finalize the mask on tab 3 before exporting.")
        self.aoi_status.setText("Finalize the mask on tab 3 before exporting plot statistics.")
        self.aoi_path.clear(); self.aoi_id_field.clear(); self.aoi_id_field.addItem("Automatic · feature ID", "")
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
        key = repr([config[k] for k in ("source_mode", "stacked_path", "folder_path", "ortho_type", "rgb_order", "hsi_min_wavelength", "hsi_max_wavelength", "hsi_interval")] + [sorted(config["bands"].items())])
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
        self.bright_guard.setChecked(self.settings.value(prefix + "bright_guard", False, type=bool))
        self.shadow_guard.setChecked(self.settings.value(prefix + "shadow_guard", True, type=bool))
        self.biofilm_guard.setChecked(self.settings.value(prefix + "biofilm_guard", True, type=bool))
        self.adaptation_mode.setCurrentIndex(max(0, self.adaptation_mode.findData(self.settings.value(prefix + "adaptation_mode_v371", "adaptive", type=str))))
        self.half_mixture.setChecked(self.settings.value(prefix + "half_mixture", False, type=bool))
        self.rgb_order.setCurrentIndex(max(0, self.rgb_order.findData(self.settings.value(prefix + "rgb_order", "rgb", type=str))))
        self.hsi_min_wavelength.setValue(self.settings.value(prefix + "hsi_min_wavelength", 0.0, type=float))
        self.hsi_max_wavelength.setValue(self.settings.value(prefix + "hsi_max_wavelength", 0.0, type=float))
        self.hsi_interval.setValue(self.settings.value(prefix + "hsi_interval", 0.0, type=float))
        self.export_path.setText("")

    def _save_settings(self):
        prefix = "ppe_qgis/"
        self.settings.setValue(prefix + "output", self.output_path.text().strip())
        self.settings.setValue(prefix + "work", self.work_path.text().strip())
        self.settings.setValue(prefix + "bright_guard", self.bright_guard.isChecked())
        self.settings.setValue(prefix + "shadow_guard", self.shadow_guard.isChecked())
        self.settings.setValue(prefix + "biofilm_guard", self.biofilm_guard.isChecked())
        self.settings.setValue(prefix + "adaptation_mode_v371", self.adaptation_mode.currentData())
        self.settings.setValue(prefix + "half_mixture", self.half_mixture.isChecked())
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
            "bands": bands,
            "output_dir": self.output_path.text().strip(),
            "work_dir": self.work_path.text().strip(),
            "ortho_type": ortho_type,
            "rgb_order": self.rgb_order.currentData(),
            "hsi_min_wavelength": self.hsi_min_wavelength.value() if is_hsi_selected else 0.0,
            "hsi_max_wavelength": self.hsi_max_wavelength.value() if is_hsi_selected else 0.0,
            "hsi_interval": self.hsi_interval.value() if is_hsi_selected else 0.0,
            "hsi_grid_confirmed": self.hsi_grid_confirmed if is_hsi_selected else False,
            "threads": self.thread_count,
            "adaptation_mode": self.adaptation_mode.currentData(),
            "half_mixture": self.half_mixture.isChecked(),
            "sample_size": self.sample_size.value(),
            "initial_threshold": 0.5,
            "bright_guard": self.bright_guard.isChecked(),
            "shadow_guard": self.shadow_guard.isChecked(),
            "biofilm_guard": self.biofilm_guard.isChecked(),
        }
        if action == "apply":
            config.update({
                "vote_path": self.result.get("vote_path", ""),
                "threshold": self.threshold(),
                "run_dir": self.result.get("run_dir", config["output_dir"]),
            })
        elif action.startswith("export"):
            config.update({"mask_path": self.result.get("mask_path", ""), "export_dir": self.export_path.text().strip(), "aoi_path": self.aoi_path.text().strip(), "aoi_id_field": self.aoi_id_field.currentData()})
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
            if config["action"] == "export_aoi" and not os.path.isfile(config.get("aoi_path", "")): return "Select a valid AOI polygon boundary file."
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
        canopy = canopy_at(self.curve_points, threshold)
        self.canopy_value.setText("—" if canopy is None else "{:.1f}%".format(canopy))
        if self.curve_dialog is not None:
            self.curve_dialog.set_threshold(threshold)

    def _show_curve(self):
        if not self.curve_points:
            return
        dialog = ThresholdCurveDialog(self.curve_points, self.threshold(), self)
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
        if running and str(action).startswith("export"):
            self.export_progress.setValue(0)
            self.export_progress.setFormat("Starting…")
        elif running:
            self.progress.setValue(0)
            self.progress.setFormat("Starting…")
            if action == "run":
                self.run_summary.setText("PPE is processing the imagery in the background.")

    def set_progress(self, percent, message):
        bar = self.export_progress if str(getattr(self, "_action", "run")).startswith("export") else self.progress
        bar.setValue(percent)
        bar.setFormat("{}% · {}".format(percent, message))

    def append_log(self, message):
        if message:
            self.log_view.appendPlainText(message)

    def set_results(self, result, curve_points):
        self.result = dict(result)
        self.curve_points = list(curve_points)
        threshold = float(result.get("threshold", 0.5))
        self.threshold_slider.blockSignals(True)
        self.threshold_slider.setValue(round(threshold * 100))
        self.threshold_slider.blockSignals(False)
        self.threshold_slider.setEnabled(True)
        self.curve_button.setEnabled(bool(curve_points))
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
            "{}Canopy at {:.2f}: {}\nLayers: {} · {} votes\nMap: {}{}\nGuards removed: {}\nPPE {} · {}".format(
                warning, threshold, "—" if canopy is None else "{:.1f} %".format(canopy),
                layers, result.get("n_voters", "—"),
                result.get("display_mode") or ("true colour" if result.get("true_rgb", False) else "single band"),
                proxy_note.replace("\nBands used:", "\nBands:"),
                ", ".join(guards) if guards else "none",
                str(result.get("ppe_version", "—")).replace("native-", ""), ("{:.1f} s".format(seconds) if seconds < 10 else "{:.0f} s".format(seconds)),
            )
        )
        self.append_log("Run folder: {}".format(result.get("run_dir", "")))
        self.applied_status.setText(("⚠ " + warning.strip().replace("\n", "\n⚠ ") + "\n\n" if warning else "") +
                                    "Move the slider and watch the map. When the mask looks right, finalize it.")

    def set_applied(self, result):
        self.result.update(result)
        self.applied_status.setText(
            "Mask finalized at {:.2f} · canopy {:.1f} %\n{}".format(
                float(result.get("threshold", self.threshold())),
                float(result.get("canopy_pct", 0.0)),
                result.get("mask_path", ""),
            )
        )
        for button in (self.export_bands_button, self.export_vis_button, self.export_all_button, self.aoi_export_button): button.setEnabled(True)
        self.export_status.setText("Mask finalized. Choose what to export.")

    def set_exported(self, result):
        self.export_progress.setValue(100); self.export_progress.setFormat("Complete")
        if result.get("aoi_csv"):
            self.aoi_status.setText("Plot statistics written · {} plots · {} rows\n{}".format(result.get("plot_count", 0), result.get("row_count", 0), result["aoi_csv"]))
        else:
            self.export_status.setText("Export complete · {} GeoTIFFs\n{}".format(result.get("export_count", 0), result.get("export_dir", "")))

    def show_error(self, message):
        QMessageBox.critical(self, "Plant Pixel Extractor", message)
        bar = self.export_progress if str(getattr(self, "_action", "run")).startswith("export") else self.progress
        bar.setFormat("Failed")

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

"""Main QGIS plugin controller."""

import os

from contextlib import contextmanager

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction
from qgis.core import Qgis, QgsProject

from .dock import PPEDockWidget
from .layer_utils import (
    load_result_layers,
    load_import_layer,
    read_curve,
    remove_previous_layers,
    replace_applied_mask,
    style_vote_threshold,
    validate_aligned,
)
from .process_runner import PPEPreviewRunner, PPEProcessRunner


class PPEPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dock = None
        self.runner = PPEProcessRunner(iface.mainWindow())
        self.previewer = PPEPreviewRunner(iface.mainWindow())
        self.layers = {}
        self._removing = False            # True while PPE itself removes its layers
        self._project_hooked = False
        self._image_removed = False
        self.current_action = "run"
        self.preview_timer = QTimer(iface.mainWindow())
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(45)
        self.preview_timer.timeout.connect(self._refresh_preview)

    def initGui(self):  # noqa: N802 - QGIS plugin API
        icon_path = os.path.join(os.path.dirname(__file__), "icons", "ppe.svg")
        self.action = QAction(QIcon(icon_path), "Plant Pixel Extractor", self.iface.mainWindow())
        self.action.setObjectName("PPEOpenAction")
        self.action.setToolTip("Open Plant Pixel Extractor")
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToRasterMenu("Plant Pixel Extractor", self.action)

    def unload(self):
        if self.runner.is_running:
            self.runner.cancel()
        if self.action is not None:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginRasterMenu("Plant Pixel Extractor", self.action)
            self.action.deleteLater()
            self.action = None
        if self._project_hooked:
            try:
                QgsProject.instance().layersWillBeRemoved.disconnect(self._layers_will_be_removed)
                QgsProject.instance().layersRemoved.disconnect(self._layers_removed)
            except (TypeError, RuntimeError):
                pass
            self._project_hooked = False
        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None

    def run(self):
        if self.dock is None:
            self._create_dock()
        self.dock.show()
        self.dock.raise_()

    def _create_dock(self):
        self.dock = PPEDockWidget(self.iface)
        self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock)
        self.dock.generate_requested.connect(self._start_generate)
        self.dock.apply_requested.connect(self._start_apply)
        self.dock.export_requested.connect(self._start_export)
        self.dock.cancel_requested.connect(self.runner.cancel)
        self.dock.preview_requested.connect(self._start_preview)
        self.dock.reset_requested.connect(self._reset_layers)
        QgsProject.instance().layersWillBeRemoved.connect(self._layers_will_be_removed)
        QgsProject.instance().layersRemoved.connect(self._layers_removed)
        self._project_hooked = True
        self.previewer.ready.connect(self._preview_ready)
        self.previewer.failed.connect(self._preview_failed)
        self.dock.threshold_slider.valueChanged.connect(lambda _value: self.preview_timer.start())
        self.runner.progress.connect(self.dock.set_progress)
        self.runner.import_ready.connect(self._import_ready)
        self.runner.log.connect(self.dock.append_log)
        self.runner.succeeded.connect(self._job_succeeded)
        self.runner.failed.connect(self._job_failed)
        self.runner.running_changed.connect(self._runner_state_changed)

    # ---- keeping the map and the panel in step -----------------------------------------
    @contextmanager
    def _own_changes(self):
        """Layer removals PPE makes itself are not the user deleting a layer."""
        self._removing = True
        try:
            yield
        finally:
            self._removing = False

    def _clear_ppe_layers(self, keep=()):
        self._removing = True
        try:
            remove_previous_layers(keep=keep)
        finally:
            self._removing = False

    def _reset_layers(self):
        """The image was changed or removed in the panel: take every PPE layer off the map."""
        self._clear_ppe_layers()
        self.layers = {}
        self.iface.mapCanvas().refresh()

    def _layer_ids(self):
        ids = {}
        for role, layer in list(self.layers.items()):
            try:
                if layer is not None: ids[layer.id()] = role
            except RuntimeError:          # the layer object is already gone
                self.layers[role] = None
        return ids

    def _layers_will_be_removed(self, layer_ids):
        """Something removed layers from the project. When it was the user and it was a PPE
        layer, bring the panel in line with what is still on the map."""
        if self._removing or self.dock is None:
            return
        known = self._layer_ids()
        removed = {known[i] for i in layer_ids if i in known}
        if not removed:
            return
        for role in removed:
            self.layers[role] = None
        if "rgb" in removed:
            if self.runner.is_running:
                self.runner.cancel()
            # The image is gone: its results go with it, and the panel forgets the file. That is
            # done in _layers_removed, once QGIS has finished removing the layer.
            self._image_removed = True
        elif "votes" in removed:
            self.dock.mask_layers_removed(True)

    def _layers_removed(self, _layer_ids):
        if self._image_removed and not self._removing:
            self._image_removed = False
            self._image_layer_gone()

    def _image_layer_gone(self):
        self._reset_layers()
        self.dock.image_layer_removed()
        self._message("The PPE image was removed from the map, so PPE was reset.", Qgis.Info)

    def _start_preview(self, config):
        """The user has just chosen an image: find its bands and show it, before any analysis."""
        if self.runner.is_running:
            return
        self.previewer.request(config)

    def _preview_ready(self, result):
        if self.runner.is_running or self.dock is None:
            return
        if result.get("preview_key") and result.get("preview_key") != self.dock._preview_key:
            return                      # the image was changed or removed while this one was being read
        self._clear_ppe_layers()        # a new image: masks and votes of the previous one no longer apply
        self.layers = {}
        with self._own_changes():
            layer = load_import_layer(self.iface, result)
        if layer is None:
            self.dock.set_preview_failed("QGIS could not draw this image.")
            return
        self.layers["rgb"] = layer
        self.dock.set_preview(result)

    def _preview_failed(self, message):
        if self.dock is not None:
            self.dock.set_preview_failed(message)

    def _start_generate(self, config):
        self.current_action = "run"
        self._clear_ppe_layers(keep=("rgb",))       # the image stays on the map while PPE works
        self.layers = {"rgb": self.layers.get("rgb")} if self.layers.get("rgb") is not None else {}
        self.dock.log_view.clear()
        self.dock.set_running(True, "run")
        self._start_process(config)

    def _start_apply(self, config):
        self.current_action = "apply"
        self.dock.set_running(True, "apply")
        self._start_process(config)

    def _start_export(self, config):
        self.current_action = config.get("action", "export_all")
        self.dock.set_running(True, self.current_action)
        self._start_process(config)

    def _start_process(self, config):
        try:
            self.runner.start(config)
        except RuntimeError as exc:
            self.dock.set_running(False, self.current_action)
            self.dock.show_error(str(exc))

    def _runner_state_changed(self, running):
        if self.dock is not None:
            self.dock.set_running(running, self.current_action)

    def _job_succeeded(self, result):
        if self.current_action.startswith("export"):
            self.dock.set_exported(result)
            message = "AOI plot statistics export complete." if result.get("aoi_csv") else "Plant-only GeoTIFF export complete."
            self._message(message, Qgis.Success)
            return
        if self.current_action == "apply":
            aligned, detail = validate_aligned([self.dock.result.get("vote_path"), result.get("mask_qgis_path")])
            if not aligned:
                self._job_failed("Exported mask alignment check failed: {}".format(detail))
                return
            with self._own_changes():
                replace_applied_mask(result, self.layers)
            self.dock.set_applied(result)
            self.dock.tabs.setCurrentIndex(3)
            self._message("Native PPE mask exported and loaded.", Qgis.Success)
            return

        aligned, detail = validate_aligned([result.get("rgb_path"), result.get("vote_path")])
        if not aligned:
            self._job_failed("RGB and vote-raster alignment check failed: {}".format(detail))
            return
        self.dock.append_log("Alignment verified: RGB and vote raster have identical CRS, extent, dimensions, origin, rotation, and pixel size.")
        curve = read_curve(result.get("curve_path"))
        with self._own_changes():
            self.layers = load_result_layers(self.iface, result, float(result.get("threshold", 0.5)))
        if self.layers.get("votes") is None:
            self._job_failed("PPE completed, but QGIS could not load the vote-fraction raster.")
            return
        self.dock.set_results(result, curve)
        self.dock.tabs.setCurrentIndex(2)
        if not result.get("true_rgb", False):
            bands = result.get("source_bands", [])
            missing = [name for name in ("red", "green", "blue") if name not in bands]
            self._message(
                "True colour is not possible ({} missing). The image is shown as {}.".format(
                    ", ".join(missing) if missing else "red, green or blue", result.get("display_mode", "a single band")
                ),
                Qgis.Info,
            )
        checks = []
        if result.get("one_class_warning") == "soil": checks.append("the image looks like bare soil only")
        elif result.get("one_class_warning") == "plant": checks.append("the image looks like closed canopy only")
        if result.get("weak_green_warning"): checks.append("plants are barely greener than soil")
        if checks:
            self.iface.messageBar().pushMessage(
                "Plant Pixel Extractor", "Mask generated, but check it closely: {}.".format("; ".join(checks)),
                level=Qgis.Warning, duration=0)
        else:
            self._message("PPE mask generated. Review the live threshold preview.", Qgis.Success)

    def _import_ready(self, result):
        with self._own_changes():
            rgb = load_import_layer(self.iface, result)
        if rgb is None:
            self.dock.append_log("QGIS could not open the imported imagery source.")
            self._message("Import succeeded, but QGIS could not display the imagery.", Qgis.Warning)
            return
        self.layers["rgb"] = rgb
        bands = result.get("rgb_bands", [])
        display = result.get("display_mode") or ("true colour" if result.get("true_rgb", False) else "single band")
        self.dock.append_log(
            "Image displayed as {} ({}); {} vegetation indices are available.".format(
                display, ", ".join(bands), len(result.get("available_vis", []))
            )
        )

    def _job_failed(self, message):
        if self.dock is not None:
            self.dock.set_running(False, self.current_action)
            self.dock.show_error(message)
        self._message(message, Qgis.Critical)

    def _refresh_preview(self):
        if self.dock is None:
            return
        votes = self.layers.get("votes")
        if votes is None:
            return
        style_vote_threshold(votes, self.dock.threshold())
        self.iface.mapCanvas().refresh()

    def _message(self, message, level):
        self.iface.messageBar().pushMessage(
            "Plant Pixel Extractor", message, level=level, duration=6
        )

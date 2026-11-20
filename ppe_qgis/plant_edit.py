"""Correct the plant count on the map (4.5): add a missed plant, remove a wrong point, or switch a point
between plant and weed (weeds are not counted). Each click is written to ppe_plants.gpkg at once."""
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.core import QgsCoordinateTransform, QgsFeature, QgsFeatureRequest, QgsGeometry, QgsPointXY, QgsProject, QgsRectangle
from qgis.gui import QgsMapTool

from .layer_utils import ROLE_KEY

STATUS_PLANT, STATUS_CHECK, STATUS_WEED, STATUS_ADDED = "plant", "check", "weed?", "added"
PICK_PIXELS = 12            # a click picks the nearest point within this many screen pixels


def plants_layer():
    for layer in QgsProject.instance().mapLayers().values():
        if layer.customProperty(ROLE_KEY, "") == "plants":
            return layer
    return None


class PlantEditTool(QgsMapTool):
    """mode: 'add', 'remove' or 'switch'. Emits changed() after every edit; Esc emits done()."""
    changed = pyqtSignal(str)
    done = pyqtSignal()

    def __init__(self, canvas, layer, mode):
        super().__init__(canvas)
        self.layer, self.mode = layer, mode
        self.setCursor(Qt.CrossCursor)
        self.undo = []                   # (kind, data) to undo the last edits

    def _to_layer(self, point):
        tr = QgsCoordinateTransform(self.canvas().mapSettings().destinationCrs(), self.layer.crs(), QgsProject.instance())
        return tr.transform(point)

    def _nearest(self, event):
        p = self.toMapCoordinates(event.pos()); r = self.canvas().mapUnitsPerPixel() * PICK_PIXELS
        tr = QgsCoordinateTransform(self.canvas().mapSettings().destinationCrs(), self.layer.crs(), QgsProject.instance())
        box = tr.transformBoundingBox(QgsRectangle(p.x() - r, p.y() - r, p.x() + r, p.y() + r)); c = tr.transform(p)
        best = None
        for f in self.layer.getFeatures(QgsFeatureRequest().setFilterRect(box)):
            d = f.geometry().asPoint().distance(c)
            if best is None or d < best[0]: best = (d, f)
        return best[1] if best else None

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.LeftButton: return
        prov = self.layer.dataProvider(); fields = self.layer.fields()
        if self.mode == "add":
            f = QgsFeature(fields); f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(self._to_layer(self.toMapCoordinates(event.pos())))))
            ids = [x["plant_id"] for x in self.layer.getFeatures() if x["plant_id"] is not None]
            f["plant_id"] = (max(ids) if ids else 0) + 1; f["status"] = STATUS_ADDED; f["on_row"] = 1
            ok, out = prov.addFeatures([f])
            if ok: self.undo.append(("add", out[0].id())); self._changed("Plant added.")
            return
        f = self._nearest(event)
        if f is None:
            self.changed.emit("No point there: click on a point."); return
        if self.mode == "remove":
            self.undo.append(("remove", QgsFeature(f)))
            prov.deleteFeatures([f.id()]); self._changed("Point removed.")
        elif self.mode == "switch":
            old = f["status"]; new = STATUS_PLANT if old == STATUS_WEED else STATUS_WEED
            prov.changeAttributeValues({f.id(): {fields.indexOf("status"): new}})
            self.undo.append(("switch", (f.id(), old))); self._changed("Marked as {}.".format("weed (not counted)" if new == STATUS_WEED else "plant"))

    def undo_last(self):
        if not self.undo: return False
        kind, data = self.undo.pop(); prov = self.layer.dataProvider()
        if kind == "add": prov.deleteFeatures([data])
        elif kind == "remove": prov.addFeatures([data])
        else: prov.changeAttributeValues({data[0]: {self.layer.fields().indexOf("status"): data[1]}})
        self._changed("Last change undone."); return True

    def _changed(self, message):
        self.layer.triggerRepaint(); self.changed.emit(message)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape: self.done.emit()
        elif event.key() in (Qt.Key_Backspace, Qt.Key_Z) and (event.key() == Qt.Key_Backspace or event.modifiers() & Qt.ControlModifier): self.undo_last()

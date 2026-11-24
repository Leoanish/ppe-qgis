"""Correct the plant count on the map (4.5): add a missed plant, remove a wrong point, or switch a point
between plant and weed (weeds are not counted). Each click is written to ppe_plants.gpkg at once."""
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.core import QgsCoordinateTransform, QgsFeature, QgsFeatureRequest, QgsGeometry, QgsPointXY, QgsProject, QgsRectangle
from qgis.gui import QgsMapTool

from .layer_utils import ROLE_KEY

STATUS_PLANT, STATUS_CHECK, STATUS_WEED, STATUS_ADDED, STATUS_SPLIT = "plant", "check", "weed?", "added", "split"
STATUS_AUTO = "auto split"     # 4.7: split by PPE from the plant spacing
PICK_PIXELS = 12            # a click picks the nearest point within this many screen pixels


def plants_layer():
    for layer in QgsProject.instance().mapLayers().values():
        if layer.customProperty(ROLE_KEY, "") == "plants":
            return layer
    return None


class PlantEditTool(QgsMapTool):
    """mode: 'add', 'remove', 'switch', 'split' (4.6.6: a point of touching plants becomes one point
    per plant) or 'merge' (4.7: points split from one group become one point again; or click two points
    to make them one plant). Emits changed() after every edit; Esc emits done()."""
    changed = pyqtSignal(str)
    done = pyqtSignal()

    def __init__(self, canvas, layer, mode):
        super().__init__(canvas)
        self.layer, self.mode = layer, mode
        self.setCursor(Qt.CrossCursor)
        self.undo = []                   # (kind, data) to undo the last edits
        self.first = None                # merge: the first of two points

    def _to_layer(self, point):
        tr = QgsCoordinateTransform(self.canvas().mapSettings().destinationCrs(), self.layer.crs(), QgsProject.instance())
        return tr.transform(point)

    def _nearest(self, event):
        p = self.toMapCoordinates(event.pos())
        r = self.canvas().mapUnitsPerPixel() * PICK_PIXELS
        tr = QgsCoordinateTransform(self.canvas().mapSettings().destinationCrs(), self.layer.crs(), QgsProject.instance())
        box = tr.transformBoundingBox(QgsRectangle(p.x() - r, p.y() - r, p.x() + r, p.y() + r))
        c = tr.transform(p)
        best = None
        for f in self.layer.getFeatures(QgsFeatureRequest().setFilterRect(box)):
            d = f.geometry().asPoint().distance(c)
            if best is None or d < best[0]:
                best = (d, f)
        return best[1] if best else None

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        prov = self.layer.dataProvider()
        fields = self.layer.fields()
        if self.mode == "add":
            f = QgsFeature(fields)
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(self._to_layer(self.toMapCoordinates(event.pos())))))
            ids = [x["plant_id"] for x in self.layer.getFeatures() if x["plant_id"] is not None]
            f["plant_id"] = (max(ids) if ids else 0) + 1
            f["status"] = STATUS_ADDED
            f["on_row"] = 1
            ok, out = prov.addFeatures([f])
            if ok:
                self.undo.append(("add", out[0].id()))
                self._changed("Plant added.")
            return
        f = self._nearest(event)
        if f is None:
            self.changed.emit("No point there: click on a point.")
            return
        if self.mode == "remove":
            self.undo.append(("remove", QgsFeature(f)))
            prov.deleteFeatures([f.id()])
            self._changed("Point removed.")
        elif self.mode == "split":
            self._split(f)
        elif self.mode == "merge":
            self._merge(f)
        elif self.mode == "switch":
            old = f["status"]
            new = STATUS_PLANT if old == STATUS_WEED else STATUS_WEED
            prov.changeAttributeValues({f.id(): {fields.indexOf("status"): new}})
            self.undo.append(("switch", (f.id(), old)))
            self._changed("Marked as {}.".format("weed (not counted)" if new == STATUS_WEED else "plant"))

    def _split(self, f):
        """Ask how many plants the user sees in the group, find that many lobes and replace the point by
        one point per lobe. PPE does not guess the number: on PhenoBench neither the group's size over a
        typical plant nor the number of lobes in its shape got it right often enough (weeds are small,
        beets large, and a quarter of the flagged groups were one big plant)."""
        from qgis.PyQt.QtWidgets import QInputDialog
        if f["status"] == STATUS_WEED:
            self.changed.emit("This point is a possible weed (not counted): switch it to plant first.")
            return
        group = self._siblings(f)
        if len(group) > 1:
            by = "PPE (plant spacing)" if f["status"] == STATUS_AUTO else "you"
            n, ok = QInputDialog.getInt(self.canvas().window(), "Split this part",
                                        "This point is one of {} plants that {} split this patch into.\n"
                                        "How many plants do you see in THIS part?\n(⊕ Merge makes the whole patch one plant again.)".format(len(group), by),
                                        2, 2, 50, 1)
            if not ok:
                return
            self._split_part(f, n, group)
            return
        n, ok = QInputDialog.getInt(self.canvas().window(), "Split into plants",
                                    "How many plants do you see in this group?\nPPE puts one point on each of them (on the thickest parts of the patch).",
                                    2, 2, 50, 1)
        if not ok:
            return
        self._split_into(f, n)

    def _split_part(self, f, n, group):
        """Split one part of an already split patch; its other points stay."""
        from .plant_count import split_part
        path = self.layer.source().split("|")[0]
        p = f.geometry().asPoint()
        area = sum(self._area(g) or 0.0 for g in group)
        parts = split_part(path, p.x(), p.y(), n, [(g.geometry().asPoint().x(), g.geometry().asPoint().y()) for g in group], area or None)
        if len(parts) < 2:
            self.changed.emit("Could not find {} separate plants in this part. Use + Add plant instead.".format(n))
            return
        fields = self.layer.fields()
        prov = self.layer.dataProvider()
        next_id = max([x["plant_id"] for x in self.layer.getFeatures() if x["plant_id"] is not None] or [0]) + 1
        new = []
        for k, (x, y, a) in enumerate(parts):
            g = QgsFeature(fields)
            g.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
            g["plant_id"] = next_id + k
            g["status"] = STATUS_SPLIT
            g["split_of"] = f["split_of"]
            if fields.indexOf("on_row") >= 0:
                g["on_row"] = f["on_row"] if f["on_row"] is not None else 1
            if fields.indexOf("area_cm2") >= 0:
                g["area_cm2"] = a
            new.append(g)
        old = [QgsFeature(f)]
        ok, out = prov.addFeatures(new)
        if not ok:
            self.changed.emit("Could not write the new points.")
            return
        prov.deleteFeatures([f.id()])
        self.undo.append(("split", (old, [o.id() for o in out])))
        self._changed("Part split into {} plants; the patch now has {} plants.".format(len(parts), len(group) - 1 + len(parts)))

    def _siblings(self, f):
        """The points split from the same group as f (f included), or [f]."""
        if self.layer.fields().indexOf("split_of") < 0 or not f["split_of"]:
            return [f]
        return [g for g in self.layer.getFeatures() if g["split_of"] == f["split_of"]] or [f]

    def _area(self, f):
        return f["area_cm2"] if self.layer.fields().indexOf("area_cm2") >= 0 else None

    def _merge(self, f):
        """A point split from a group (by PPE or by you): all points of that group become one plant again.
        Any other point: remembered; the next point clicked is merged with it."""
        from .plant_count import merge_points
        group = self._siblings(f)
        if len(group) < 2:
            if self.first is None or self.first.id() == f.id():
                self.first = QgsFeature(f)
                self.changed.emit("Now click the point to merge it with.")
                return
            group = [self.first, f]
        self.first = None
        x, y, a = merge_points([(g.geometry().asPoint().x(), g.geometry().asPoint().y(), self._area(g) or 0.0) for g in group])
        fields = self.layer.fields()
        prov = self.layer.dataProvider()
        m = QgsFeature(fields)
        m.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        m["plant_id"] = min(g["plant_id"] for g in group if g["plant_id"] is not None)
        m["status"] = STATUS_PLANT
        if fields.indexOf("on_row") >= 0:
            m["on_row"] = group[0]["on_row"] if group[0]["on_row"] is not None else 1
        if fields.indexOf("area_cm2") >= 0 and a:
            m["area_cm2"] = a
        if fields.indexOf("split_of") >= 0:
            m["split_of"] = 0
        old = [QgsFeature(g) for g in group]
        ok, out = prov.addFeatures([m])
        if not ok:
            self.changed.emit("Could not write the merged point.")
            return
        prov.deleteFeatures([g.id() for g in group])
        self.undo.append(("split", (old, [o.id() for o in out])))
        self._changed("{} points merged into one plant.".format(len(group)))

    def _split_into(self, f, n):
        from .plant_count import split_group
        path = self.layer.source().split("|")[0]
        p = f.geometry().asPoint()
        group = self._siblings(f)          # (called with a single point; a split patch is split anew as a whole)
        area = sum(self._area(g) or 0.0 for g in group) if len(group) > 1 else self._area(f)
        try:
            parts = split_group(path, p.x(), p.y(), n, area)
        except Exception as exc:
            self.changed.emit("Could not split it: {}".format(exc))
            return
        if len(parts) < 2:
            self.changed.emit("Could not find separate plants there. Use + Add plant instead.")
            return
        prov = self.layer.dataProvider()
        fields = self.layer.fields()
        ids = [x["plant_id"] for x in self.layer.getFeatures() if x["plant_id"] is not None]
        next_id = (max(ids) if ids else 0) + 1
        new = []
        for k, (x, y, a) in enumerate(parts):
            g = QgsFeature(fields)
            g.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
            g["plant_id"] = next_id + k
            g["status"] = STATUS_SPLIT
            if fields.indexOf("on_row") >= 0:
                g["on_row"] = f["on_row"] if f["on_row"] is not None else 1
            if fields.indexOf("area_cm2") >= 0:
                g["area_cm2"] = a
            if fields.indexOf("split_of") >= 0:
                g["split_of"] = (f["split_of"] or f["plant_id"]) if len(group) > 1 else f["plant_id"]
            new.append(g)
        old = [QgsFeature(g) for g in group]
        ok, out = prov.addFeatures(new)
        if not ok:
            self.changed.emit("Could not write the new points.")
            return
        prov.deleteFeatures([g.id() for g in group])
        self.undo.append(("split", (old, [o.id() for o in out])))
        self._changed("Split into {} plants (each counted).".format(len(out)))

    def undo_last(self):
        if not self.undo:
            return False
        kind, data = self.undo.pop()
        prov = self.layer.dataProvider()
        if kind == "split":
            old = data[0] if isinstance(data[0], list) else [data[0]]
            prov.deleteFeatures(data[1])
            prov.addFeatures(old)
            self._changed("Last change undone.")
            return True
        if kind == "add":
            prov.deleteFeatures([data])
        elif kind == "remove":
            prov.addFeatures([data])
        else:
            prov.changeAttributeValues({data[0]: {self.layer.fields().indexOf("status"): data[1]}})
        self._changed("Last change undone.")
        return True

    def _changed(self, message):
        self.layer.triggerRepaint()
        self.changed.emit(message)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.done.emit()
        elif event.key() in (Qt.Key_Backspace, Qt.Key_Z) and (event.key() == Qt.Key_Backspace or event.modifiers() & Qt.ControlModifier):
            self.undo_last()


# ---- checking label points (4.6) ---------------------------------------------------------------
def labels_layer():
    for layer in QgsProject.instance().mapLayers().values():
        if layer.customProperty(ROLE_KEY, "") == "labels":
            return layer
    return None


class LabelEditTool(PlantEditTool):
    """Check the label points: mode 'switch' (plant <-> soil; a weed point becomes plant), 'weed' (4.10.1:
    weed <-> plant), 'remove', 'add_plant', 'add_soil' or 'add_weed'.
    A switched point gets status 'changed' (back to 'proposed' when switched back); an added point 'added'."""

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        prov = self.layer.dataProvider()
        fields = self.layer.fields()
        if self.mode in ("add_plant", "add_soil", "add_weed"):
            f = QgsFeature(fields)
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(self._to_layer(self.toMapCoordinates(event.pos())))))
            ids = [x["id"] for x in self.layer.getFeatures() if x["id"] is not None]
            f["id"] = (max(ids) if ids else 0) + 1
            f["label"] = self.mode[4:]
            f["ppe_label"] = ""
            f["status"] = "added"
            ok, out = prov.addFeatures([f])
            if ok:
                self.undo.append(("add", out[0].id()))
                self._changed("{} point added.".format(f["label"].capitalize()))
            return
        f = self._nearest(event)
        if f is None:
            self.changed.emit("No point there: click on a point.")
            return
        if self.mode == "remove":
            self.undo.append(("remove", QgsFeature(f)))
            prov.deleteFeatures([f.id()])
            self._changed("Point removed.")
        elif self.mode in ("switch", "weed"):
            old = (f["label"], f["status"])
            if self.mode == "weed":
                new = "plant" if f["label"] == "weed" else "weed"
            else:
                new = "soil" if f["label"] == "plant" else "plant"
            status = "added" if f["status"] == "added" else ("proposed" if new == f["ppe_label"] else "changed")
            prov.changeAttributeValues({f.id(): {fields.indexOf("label"): new, fields.indexOf("status"): status}})
            self.undo.append(("switch", (f.id(), old)))
            self._changed("Now {}.".format(new))

    def undo_last(self):
        if not self.undo:
            return False
        kind, data = self.undo.pop()
        prov = self.layer.dataProvider()
        fields = self.layer.fields()
        if kind == "add":
            prov.deleteFeatures([data])
        elif kind == "remove":
            prov.addFeatures([data])
        else:
            prov.changeAttributeValues({data[0]: {fields.indexOf("label"): data[1][0], fields.indexOf("status"): data[1][1]}})
        self._changed("Last change undone.")
        return True


# ---- reviewing weeds of What PPE found (4.10.0) -------------------------------------------------
class WeedReviewTool(QgsMapTool):
    """Click a plant piece: it switches between weed (What PPE found, codes 7/8) and crop. Emits reviewed(result)
    after every click (see plant_count.toggle_weed_piece); Esc emits done(). A second click on a piece undoes it."""
    reviewed = pyqtSignal(dict)
    done = pyqtSignal()

    def __init__(self, canvas, weeds, findings_path, vote_path):
        super().__init__(canvas)
        from osgeo import gdal
        from qgis.core import QgsCoordinateReferenceSystem
        self.weeds, self.findings_path, self.vote_path = weeds, findings_path, vote_path
        ds = gdal.Open(weeds["groups_path"])
        self.crs = QgsCoordinateReferenceSystem.fromWkt(ds.GetProjection() if ds else "")
        ds = None
        self.setCursor(Qt.CrossCursor)

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        from .plant_count import toggle_weed_piece
        p = self.toMapCoordinates(event.pos())
        if self.crs.isValid():
            p = QgsCoordinateTransform(self.canvas().mapSettings().destinationCrs(), self.crs, QgsProject.instance()).transform(p)
        try:
            r = toggle_weed_piece(self.weeds, self.findings_path, self.vote_path, p.x(), p.y())
        except Exception as exc:
            r = {"state": None, "message": "Could not change it: {}".format(exc)}
        self.reviewed.emit(r)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.done.emit()

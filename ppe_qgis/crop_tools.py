"""Drawing a rough crop outline on the map, and the 'PPE · Crop areas' layer that shows them."""

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPalLayerSettings,
    QgsPointXY,
    QgsProject,
    QgsSingleSymbolRenderer,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand

try:
    from qgis.PyQt.QtCore import QVariant
except ImportError:  # pragma: no cover
    QVariant = None

from .layer_utils import ROLE_KEY, place_layer, remove_role

CROP_LAYER_NAME = "PPE · Crop areas"


class PolygonDrawTool(QgsMapTool):
    """Click the corners of a polygon; right-click (or double-click) to finish, Esc to cancel.

    `finished` gives the polygon in the map canvas CRS; `cancelled` fires on Esc or when another
    map tool takes over before the polygon is finished."""

    finished = pyqtSignal(object)
    cancelled = pyqtSignal()

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.points = []
        self.done = False
        self.band = QgsRubberBand(canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        self.band.setColor(QColor(225, 25, 25, 50))  # red while drawing, red once drawn (layer_utils.AREA_RED)
        self.band.setStrokeColor(QColor(225, 25, 25))
        self.band.setWidth(2)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _redraw(self, moving=None):
        self.band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        for point in self.points + ([moving] if moving is not None else []):
            self.band.addPoint(point, False)
        self.band.show()
        self.band.updatePosition()
        self.canvas.refresh()

    def canvasMoveEvent(self, event):  # noqa: N802 - Qt API
        if self.points:
            self._redraw(self.toMapCoordinates(event.pos()))

    def canvasReleaseEvent(self, event):  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton:
            self.points.append(QgsPointXY(self.toMapCoordinates(event.pos())))
            self._redraw()
        elif event.button() == Qt.MouseButton.RightButton:
            self._finish()

    def canvasDoubleClickEvent(self, event):  # noqa: N802 - Qt API
        self._finish()

    def keyPressEvent(self, event):  # noqa: N802 - Qt API
        if event.key() == Qt.Key.Key_Escape:
            self._stop()
            self.done = True
            self.cancelled.emit()
        elif event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and self.points:
            self.points.pop()
            self._redraw()

    def _finish(self):
        unique = []
        for point in self.points:
            if not unique or point != unique[-1]:
                unique.append(point)
        if len(unique) < 3:
            return  # not a polygon yet; keep drawing
        geometry = QgsGeometry.fromPolygonXY([unique + [unique[0]]])
        if not geometry.isGeosValid():
            geometry = geometry.makeValid()
        self._stop()
        self.done = True
        self.finished.emit(geometry)

    def _stop(self):
        self.points = []
        self.band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)

    switched_away = False

    def deactivate(self):
        # Another map tool was chosen (pan, zoom...) before the outline was finished: drawing is
        # over, so the panel must hear about it even when no corner was clicked yet.
        if not self.done:
            self._stop()
            self.done = True
            self.switched_away = True  # QGIS is switching to the tool the user chose: keep it
            self.cancelled.emit()
        self.band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        super().deactivate()


def to_crs(geometry, source_crs, target_wkt):
    """A copy of the geometry in the image CRS (given as WKT)."""
    target = QgsCoordinateReferenceSystem.fromWkt(target_wkt) if target_wkt else source_crs
    copy = QgsGeometry(geometry)
    if target.isValid() and source_crs.isValid() and target != source_crs:
        copy.transform(QgsCoordinateTransform(source_crs, target, QgsProject.instance()))
    return copy, target


def show_crop_areas(areas, crs):
    """Show the drawn outlines as 'PPE · Crop areas' (cyan outlines labelled with the crop).
    areas: list of (crop name, QgsGeometry in `crs`). Returns the layer, or None when there is nothing."""
    remove_role("crops")
    drawn = [(name, geometry) for name, geometry in areas if geometry is not None and not geometry.isEmpty()]
    if not drawn:
        return None
    layer = QgsVectorLayer(
        "MultiPolygon?crs={}".format(crs.authid() or "EPSG:4326") if crs.authid() else "MultiPolygon",
        CROP_LAYER_NAME,
        "memory",
    )
    if not crs.authid():
        layer.setCrs(crs)
    provider = layer.dataProvider()
    field = QgsField("crop", QVariant.String) if QVariant is not None else QgsField("crop")
    provider.addAttributes([field])
    layer.updateFields()
    features = []
    for name, geometry in drawn:
        feature = QgsFeature(layer.fields())
        feature.setAttribute("crop", name)
        feature.setGeometry(geometry)
        features.append(feature)
    provider.addFeatures(features)
    layer.updateExtents()
    layer.setCustomProperty(ROLE_KEY, "crops")
    from .layer_utils import area_symbol

    layer.setRenderer(QgsSingleSymbolRenderer(area_symbol()))
    settings = QgsPalLayerSettings()
    settings.fieldName = "crop"
    text = QgsTextFormat()
    text.setSize(11)
    text.setColor(QColor(150, 10, 10))
    buffer = QgsTextBufferSettings()
    buffer.setEnabled(True)
    buffer.setSize(1.2)
    buffer.setColor(QColor(255, 255, 255))
    text.setBuffer(buffer)
    settings.setFormat(text)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
    layer.setLabelsEnabled(True)
    return place_layer(layer, "crops")

"""Dependency-free Qt threshold/canopy curve for the PPE review panel."""

from qgis.PyQt.QtCore import QPointF, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget


class ThresholdCurveWidget(QWidget):
    threshold_selected = pyqtSignal(float)

    def __init__(self, points, threshold=0.5, parent=None):
        super().__init__(parent)
        self.points = list(points)
        self.threshold = float(threshold)
        self.setMinimumSize(620, 360)
        self.setMouseTracking(True)

    def set_threshold(self, threshold):
        self.threshold = float(threshold)
        self.update()

    def _plot_rect(self):
        return QRectF(64, 24, max(1, self.width() - 92), max(1, self.height() - 82))

    def _ranges(self):
        if not self.points:
            return 0.0, 1.0, 0.0, 100.0
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        ymin = min(0.0, min(ys))
        ymax = max(1.0, max(ys))
        return min(xs), max(xs), ymin, ymax

    def _to_pixel(self, x, y):
        rect = self._plot_rect()
        xmin, xmax, ymin, ymax = self._ranges()
        px = rect.left() + (x - xmin) / max(1e-9, xmax - xmin) * rect.width()
        py = rect.bottom() - (y - ymin) / max(1e-9, ymax - ymin) * rect.height()
        return QPointF(px, py)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#fbfdfc"))
        rect = self._plot_rect()

        painter.setPen(QPen(QColor("#d9e4de"), 1))
        for i in range(6):
            y = rect.top() + i * rect.height() / 5
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        painter.setPen(QPen(QColor("#52675d"), 1.4))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.drawLine(rect.bottomLeft(), rect.topLeft())

        if self.points:
            path = QPainterPath()
            first = self._to_pixel(*self.points[0])
            path.moveTo(first)
            for point in self.points[1:]:
                path.lineTo(self._to_pixel(*point))
            painter.setPen(QPen(QColor("#16855b"), 3.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawPath(path)

            nearest = min(self.points, key=lambda point: abs(point[0] - self.threshold))
            marker = self._to_pixel(*nearest)
            painter.setPen(QPen(QColor("#f3a928"), 2))
            painter.setBrush(QColor("#f3a928"))
            painter.drawEllipse(marker, 6, 6)
            painter.setPen(QColor("#243b31"))
            painter.drawText(
                QPointF(min(marker.x() + 10, rect.right() - 125), max(marker.y() - 10, rect.top() + 14)),
                "{:.2f}  ·  {:.1f}%".format(nearest[0], nearest[1]),
            )

        painter.setPen(QColor("#52675d"))
        font = QFont(painter.font())
        font.setPointSize(9)
        painter.setFont(font)
        painter.drawText(QRectF(rect.left(), rect.bottom() + 14, rect.width(), 24), Qt.AlignCenter, "Plant vote threshold")
        painter.save()
        painter.translate(18, rect.center().y())
        painter.rotate(-90)
        painter.drawText(QRectF(-rect.height() / 2, -14, rect.height(), 24), Qt.AlignCenter, "Canopy (%)")
        painter.restore()

        xmin, xmax, ymin, ymax = self._ranges()
        painter.drawText(QPointF(rect.left() - 8, rect.bottom() + 14), "{:.2f}".format(xmin))
        painter.drawText(QPointF(rect.right() - 22, rect.bottom() + 14), "{:.2f}".format(xmax))
        painter.drawText(QPointF(rect.left() - 42, rect.bottom() + 4), "{:.0f}".format(ymin))
        painter.drawText(QPointF(rect.left() - 42, rect.top() + 5), "{:.0f}".format(ymax))

    def mousePressEvent(self, event):
        rect = self._plot_rect()
        if not rect.contains(event.position() if hasattr(event, "position") else event.pos()):
            return
        pos = event.position() if hasattr(event, "position") else QPointF(event.pos())
        xmin, xmax, _ymin, _ymax = self._ranges()
        threshold = xmin + (pos.x() - rect.left()) / rect.width() * (xmax - xmin)
        threshold = round(max(xmin, min(xmax, threshold)), 2)
        self.threshold_selected.emit(threshold)


class ThresholdCurveDialog(QDialog):
    threshold_selected = pyqtSignal(float)

    def __init__(self, points, threshold, parent=None):
        super().__init__(parent)
        self.setWindowTitle("PPE threshold curve")
        self.resize(720, 480)
        layout = QVBoxLayout(self)
        title = QLabel("Canopy response to plant vote threshold")
        title.setObjectName("curveTitle")
        subtitle = QLabel("Click anywhere on the curve to move the Review slider.")
        subtitle.setObjectName("curveSubtitle")
        self.curve = ThresholdCurveWidget(points, threshold, self)
        self.curve.threshold_selected.connect(self.threshold_selected)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(self.curve, 1)
        layout.addWidget(buttons)
        self.setStyleSheet("""
            QDialog { background: #fbfdfc; }
            QLabel#curveTitle { font-size: 17px; font-weight: 700; color: #173d30; }
            QLabel#curveSubtitle { color: #64786e; margin-bottom: 4px; }
            QPushButton { padding: 7px 18px; border-radius: 8px; border: 1px solid #cddbd3; background: white; }
        """)

    def set_threshold(self, threshold):
        self.curve.set_threshold(threshold)

"""QGIS raster loading and lightweight threshold rendering."""

import csv
import os

from osgeo import gdal, osr

from qgis.PyQt.QtGui import QColor
from qgis.core import (
    Qgis,
    QgsColorRampShader,
    QgsFillSymbol,
    QgsPalLayerSettings,
    QgsSingleSymbolRenderer,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
    QgsContrastEnhancement,
    QgsMultiBandColorRenderer,
    QgsProject,
    QgsRasterLayer,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
)


GROUP_NAME = "Plant Pixel Extractor"
ROLE_KEY = "ppe/role"


def _grid_signature(path):
    dataset = gdal.Open(path) if path and os.path.isfile(path) else None
    if dataset is None:
        raise ValueError("Raster is missing or unreadable: {}".format(path or "(empty path)"))
    return dataset.RasterXSize, dataset.RasterYSize, dataset.GetGeoTransform(), dataset.GetProjection()


def validate_aligned(paths):
    """Require identical dimensions, affine grid, and CRS for every raster."""
    paths = [path for path in paths if path and os.path.isfile(path)]
    if len(paths) < 2:
        return True, ""
    reference = _grid_signature(paths[0])
    for path in paths[1:]:
        current = _grid_signature(path)
        if current[:2] != reference[:2]:
            return False, "Raster dimensions differ: {} is {}×{}, expected {}×{}.".format(path, current[0], current[1], reference[0], reference[1])
        tolerance = max(1e-10, max(abs(value) for value in reference[2]) * 1e-12)
        if any(abs(a-b) > tolerance for a, b in zip(current[2], reference[2])):
            return False, "Raster pixel grids differ in origin, resolution, or rotation: {}.".format(path)
        left, right = osr.SpatialReference(), osr.SpatialReference()
        left.ImportFromWkt(reference[3]); right.ImportFromWkt(current[3])
        if not bool(left.IsSame(right)):
            return False, "Raster coordinate reference systems differ: {}.".format(path)
    return True, ""


def _force_nearest(layer):
    """Disable visual interpolation for categorical/threshold overlays."""
    if layer is None or not layer.isValid():
        return
    resampling = layer.resampleFilter()
    if resampling is not None:
        resampling.setZoomedInResampler(None)
        resampling.setZoomedOutResampler(None)


def read_curve(path):
    points = []
    if not path or not os.path.isfile(path):
        return points
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                points.append((float(row["plant_thresh"]), float(row["canopy_pct"])))
            except (KeyError, TypeError, ValueError):
                continue
    return sorted(points)


def canopy_at(points, threshold):
    if not points:
        return None
    if threshold <= points[0][0]:
        return points[0][1]
    if threshold >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= threshold <= x1:
            if x1 == x0:
                return y1
            weight = (threshold - x0) / (x1 - x0)
            return y0 + weight * (y1 - y0)
    return points[-1][1]


def _project_group():
    root = QgsProject.instance().layerTreeRoot()
    group = root.findGroup(GROUP_NAME)
    if group is None:
        group = root.insertGroup(0, GROUP_NAME)
    return group


def remove_previous_layers(keep=()):
    """Remove the layers PPE added, except those whose role is in `keep`."""
    project = QgsProject.instance()
    layer_ids = [
        layer.id()
        for layer in project.mapLayers().values()
        if layer.customProperty(ROLE_KEY, "") and layer.customProperty(ROLE_KEY, "") not in keep
    ]
    if layer_ids:
        project.removeMapLayers(layer_ids)


def remove_role(role):
    project = QgsProject.instance()
    layer_ids = [
        layer.id() for layer in project.mapLayers().values()
        if layer.customProperty(ROLE_KEY, "") == role
    ]
    if layer_ids:
        project.removeMapLayers(layer_ids)


def _add_raster(path, name, role, group, visible=True):
    if not path or not os.path.isfile(path):
        return None
    layer = QgsRasterLayer(path, name, "gdal")
    if not layer.isValid():
        return None
    layer.setCustomProperty(ROLE_KEY, role)
    QgsProject.instance().addMapLayer(layer, False)
    node = group.insertLayer(0, layer)
    node.setItemVisibilityChecked(visible)
    return layer


def prepare_rgb_source(result):
    """Return a zero-copy RGB source plus its display band numbers."""
    display = result.get("rgb_path")
    if display and os.path.isfile(display):
        return display, [1, 2, 3]
    sources = [path for path in result.get("rgb_sources", []) if path and os.path.isfile(path)]
    bands = [int(value) for value in result.get("rgb_source_bands", [])]
    if not sources:
        legacy = result.get("rgb_path")
        return legacy, [1, 2, 3]
    if len(set(sources[:3])) == 1:
        # One file holds every display band (a stacked image): draw the file itself, no copy.
        return sources[0], (bands[:3] if len(bands) >= 3 else [1, 2, 3])

    run_dir = result.get("run_dir") or os.path.dirname(sources[0])
    vrt_path = os.path.join(run_dir, "ppe_rgb_display.vrt")
    selected_sources = []
    for index, source in enumerate(sources[:3]):
        band = bands[index] if index < len(bands) else 1
        if band == 1:
            selected_sources.append(source)
            continue
        band_vrt = os.path.join(run_dir, "ppe_rgb_band_{}.vrt".format(index + 1))
        dataset = gdal.Translate(band_vrt, source, format="VRT", bandList=[band])
        if dataset is None:
            return None, [1, 2, 3]
        dataset = None
        selected_sources.append(band_vrt)
    options = gdal.BuildVRTOptions(separate=True)
    dataset = gdal.BuildVRT(vrt_path, selected_sources, options=options)
    if dataset is None:
        return None, [1, 2, 3]
    dataset = None
    return vrt_path, [1, 2, 3]


def image_layer_name(result):
    return "PPE · Image ({})".format(result.get("display_mode") or ("true colour" if result.get("true_rgb", True) else "single band"))


def style_rgb(layer, band_indices=None, stretch=None):
    """Draw three bands as red, green and blue. `stretch` gives (low, high) for each of the
    three; without it QGIS works out a 2-98 % stretch itself."""
    if layer is None or layer.bandCount() < 1:
        return
    bands = list(band_indices or [1, 2, 3])
    if len(bands) < 3 or any(band < 1 or band > layer.bandCount() for band in bands[:3]):
        bands = [1, 1, 1] if layer.bandCount() == 1 else [1, 2, 3]
    provider = layer.dataProvider()
    renderer = QgsMultiBandColorRenderer(provider, bands[0], bands[1], bands[2])
    is_display_copy = os.path.basename(layer.source()) == "ppe_rgb_display.tif"
    setters = (renderer.setRedContrastEnhancement, renderer.setGreenContrastEnhancement, renderer.setBlueContrastEnhancement)
    for position, (band, setter) in enumerate(zip(bands[:3], setters)):
        if stretch and position < len(stretch):
            lower, upper = float(stretch[position][0]), float(stretch[position][1])
        elif is_display_copy:
            lower, upper = 0, 255
        else:
            lower, upper = provider.cumulativeCut(band, 0.02, 0.98, layer.extent(), 10000)
        if not (upper > lower):
            lower, upper = 0, 255
        # A new renderer has no contrast enhancement at all; without one, reflectance values
        # (0 to 1) are drawn as they are and the image comes out black.
        enhancement = QgsContrastEnhancement(provider.dataType(band))
        enhancement.setMinimumValue(lower)
        enhancement.setMaximumValue(upper)
        enhancement.setContrastEnhancementAlgorithm(QgsContrastEnhancement.StretchToMinimumMaximum)
        setter(enhancement)
    layer.setRenderer(renderer)
    layer.triggerRepaint()


def style_vote_threshold(layer, plant_threshold, opacity=0.62):
    """Show vote cells classified as plant by PPE's ambiguous='vote' rule."""
    if layer is None or not layer.isValid():
        return
    _force_nearest(layer)
    # PPE derives soil = plant - 0.1 and uses their midpoint for ambiguous='vote'.
    effective_cutoff = max(0.0, float(plant_threshold) - 0.05)
    ramp = QgsColorRampShader()
    ramp.setColorRampType(QgsColorRampShader.Discrete)
    ramp.setColorRampItemList([
        QgsColorRampShader.ColorRampItem(
            max(0.0, effective_cutoff - 0.000001), QColor(0, 0, 0, 0), "Below threshold"
        ),
        QgsColorRampShader.ColorRampItem(
            1.000001, QColor(220, 43, 155, 235), "Plant preview"
        ),
    ])
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(ramp)
    renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    renderer.setOpacity(float(opacity))
    layer.setRenderer(renderer)
    layer.triggerRepaint()


def style_plant_mask(layer, opacity=0.72):
    if layer is None or not layer.isValid():
        return
    _force_nearest(layer)
    ramp = QgsColorRampShader()
    ramp.setColorRampType(QgsColorRampShader.Discrete)
    ramp.setColorRampItemList([
        QgsColorRampShader.ColorRampItem(0.999999, QColor(0, 0, 0, 0), "Not plant"),
        QgsColorRampShader.ColorRampItem(1.000001, QColor(220, 43, 155, 240), "Applied plant mask"),
    ])
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(ramp)
    renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    renderer.setOpacity(float(opacity))
    layer.setRenderer(renderer)
    layer.triggerRepaint()


def style_bright_rejection(layer, opacity=0.72):
    if layer is None or not layer.isValid():
        return
    _force_nearest(layer)
    ramp = QgsColorRampShader()
    ramp.setColorRampType(QgsColorRampShader.Discrete)
    ramp.setColorRampItemList([
        QgsColorRampShader.ColorRampItem(0.999999, QColor(0, 0, 0, 0), "Retained"),
        QgsColorRampShader.ColorRampItem(1.000001, QColor(235, 91, 64, 235), "Rejected bright-neutral pixel"),
    ])
    shader = QgsRasterShader(); shader.setRasterShaderFunction(ramp)
    renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    renderer.setOpacity(float(opacity)); layer.setRenderer(renderer); layer.triggerRepaint()


def load_result_layers(iface, result, threshold=0.5):
    remove_previous_layers(keep=("plots", "crops"))   # plot and crop outlines stay on the map across runs
    group = _project_group()
    rgb_path, rgb_bands = prepare_rgb_source(result)
    rgb = _add_raster(rgb_path, image_layer_name(result), "rgb", group, True)
    style_rgb(rgb, rgb_bands)
    mask = _add_raster(result.get("mask_qgis_path"), "PPE · Applied plant mask", "mask", group, False)
    style_plant_mask(mask)
    votes = _add_raster(result.get("vote_path"), "PPE · Live threshold preview", "votes", group, True)
    style_vote_threshold(votes, threshold)
    if rgb is not None:
        iface.mapCanvas().setExtent(rgb.extent())
        iface.mapCanvas().refresh()
    # Guard diagnostics (bright-neutral, shadow, algae rejection rasters) stay in the run folder
    # only; they are not added to the map, so the Layers panel holds just image, preview and mask.
    _raise_role("crops")
    raise_plot_layer()
    return {"rgb": rgb, "mask": mask, "votes": votes}


def load_import_layer(iface, result):
    """Show validated imagery immediately while PPE continues processing."""
    remove_role("rgb")
    group = _project_group()
    rgb_path, rgb_bands = prepare_rgb_source(result)
    rgb = _add_raster(rgb_path, image_layer_name(result), "rgb", group, True)
    style_rgb(rgb, rgb_bands, result.get("display_stretch") if not result.get("rgb_path") else None)
    raise_plot_layer()
    if rgb is not None:
        iface.mapCanvas().setExtent(rgb.extent())
        iface.mapCanvas().refresh()
    return rgb


def replace_applied_mask(result, layers):
    old = layers.get("mask")
    if old is not None:
        QgsProject.instance().removeMapLayer(old.id())
    group = _project_group()
    mask = _add_raster(result.get("mask_qgis_path"), "PPE · Applied plant mask", "mask", group, True)
    style_plant_mask(mask)
    layers["mask"] = mask
    for role in ("pale", "crops"):
        _raise_role(role)
    raise_plot_layer()
    return mask


# ---- plot boundaries ------------------------------------------------------------------------
PLOT_LAYER_NAME = "PPE · Plots"


def plot_layer():
    """The plot layer PPE added, or None."""
    for layer in QgsProject.instance().mapLayers().values():
        if layer.customProperty(ROLE_KEY, "") == "plots":
            return layer
    return None


def raise_plot_layer():
    """Keep the plot outlines above the image and masks in the Layers panel."""
    layer = plot_layer()
    if layer is None:
        return
    root = QgsProject.instance().layerTreeRoot()
    node = root.findLayer(layer.id())
    if node is None:
        return
    parent = node.parent()
    if parent is None or parent.children() and parent.children()[0] is node:
        return
    clone = node.clone()
    parent.insertChildNode(0, clone)
    parent.removeChildNode(node)


def load_plot_layer(uri, name, id_field="", show_labels=True):
    """Show the plot polygons on the map: yellow outlines, no fill, labelled with their IDs.
    The layer can be switched off in the Layers panel and restyled like any QGIS layer."""
    remove_role("plots")
    layer = QgsVectorLayer(uri, "{} ({})".format(PLOT_LAYER_NAME, name), "ogr")
    if not layer.isValid():
        return None
    layer.setCustomProperty(ROLE_KEY, "plots")
    symbol = QgsFillSymbol.createSimple({"style": "no", "outline_color": "255,221,0,255",
                                         "outline_width": "0.6", "outline_width_unit": "MM"})
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    set_plot_labels(layer, id_field, show_labels)
    QgsProject.instance().addMapLayer(layer, False)
    _project_group().insertLayer(0, layer)
    return layer


def set_plot_labels(layer, id_field="", show=True):
    """Label each plot with its ID (the chosen attribute, or the feature ID when none is chosen)."""
    if layer is None:
        return
    settings = QgsPalLayerSettings()
    if id_field:
        settings.fieldName = id_field; settings.isExpression = False
    else:
        settings.fieldName = "$id"; settings.isExpression = True
    text = QgsTextFormat(); text.setSize(9); text.setColor(QColor(20, 20, 20))
    buffer = QgsTextBufferSettings(); buffer.setEnabled(True); buffer.setSize(1.0); buffer.setColor(QColor(255, 255, 255))
    text.setBuffer(buffer); settings.setFormat(text)
    try:
        settings.placement = Qgis.LabelPlacement.OverPoint
    except AttributeError:
        settings.placement = QgsPalLayerSettings.OverPoint
    layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
    layer.setLabelsEnabled(bool(show))
    layer.triggerRepaint()


def style_zoned_preview(layer, thresholds, names=None, opacity=0.62):
    """Live preview with one threshold per crop area. The preview raster holds vote + 10 x area
    (0 = outside every crop outline), so a single discrete colour ramp can cut each area at its
    own threshold: thresholds[0] for the rest of the image, thresholds[k] for crop k."""
    if layer is None or not layer.isValid():
        return
    _force_nearest(layer)
    items = []
    for k, threshold in enumerate(thresholds):
        cutoff = 10.0 * k + max(0.0, float(threshold) - 0.05)
        items.append(QgsColorRampShader.ColorRampItem(cutoff - 0.000001, QColor(0, 0, 0, 0), "Below threshold"))
        items.append(QgsColorRampShader.ColorRampItem(10.0 * k + 1.000001, QColor(220, 43, 155, 235),
                                                      "Plant preview" if k == 0 else "Plant preview ({})".format(
                                                          (names or [])[k - 1] if k - 1 < len(names or []) else "crop {}".format(k))))
    ramp = QgsColorRampShader()
    ramp.setColorRampType(QgsColorRampShader.Discrete)
    ramp.setColorRampItemList(items)
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(ramp)
    renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    renderer.setOpacity(float(opacity))
    layer.setRenderer(renderer)
    layer.triggerRepaint()


def replace_preview_layer(path, layers):
    """Swap the live-preview layer's raster (the plain vote, or vote by crop area) in place."""
    old = layers.get("votes")
    visible = True
    if old is not None:
        node = QgsProject.instance().layerTreeRoot().findLayer(old.id())
        visible = node.isVisible() if node is not None else True
        QgsProject.instance().removeMapLayer(old.id())
    layer = _add_raster(path, "PPE · Live threshold preview", "votes", _project_group(), visible)
    layers["votes"] = layer
    for role in ("pale", "crops", "plots"):
        _raise_role(role)
    return layer


def _raise_role(role):
    for layer in QgsProject.instance().mapLayers().values():
        if layer.customProperty(ROLE_KEY, "") == role:
            root = QgsProject.instance().layerTreeRoot(); node = root.findLayer(layer.id())
            if node is not None and node.parent() is not None:
                parent = node.parent(); clone = node.clone(); parent.insertChildNode(0, clone); parent.removeChildNode(node)


PALE_LAYER_NAME = "PPE · What PPE found"
FINDING_COLOURS = ((1, (255, 176, 0), "Plant parts that are not green"), (2, (90, 205, 255), "Leaf edges over soil"),
                   (3, (40, 80, 230), "Shaded leaves"), (4, (240, 50, 90), "Unclear"))


def show_pale_plant(path, visible=True):
    """Add (or remove) the 'What PPE found' layer: one colour per kind, clear elsewhere.
    Information only; it is not part of the mask."""
    remove_role("pale")
    if not visible:
        return None
    layer = _add_raster(path, PALE_LAYER_NAME, "pale", _project_group(), True)
    if layer is None:
        return None
    _force_nearest(layer)
    ramp = QgsColorRampShader()
    ramp.setColorRampType(QgsColorRampShader.Exact)
    items = [QgsColorRampShader.ColorRampItem(0, QColor(0, 0, 0, 0), "Nothing to point out")]
    items += [QgsColorRampShader.ColorRampItem(code, QColor(*rgb, 245), name) for code, rgb, name in FINDING_COLOURS]
    items.append(QgsColorRampShader.ColorRampItem(255, QColor(0, 0, 0, 0), "No data"))
    ramp.setColorRampItemList(items)
    shader = QgsRasterShader(); shader.setRasterShaderFunction(ramp)
    renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    layer.setRenderer(renderer); layer.triggerRepaint()
    _raise_role("crops"); raise_plot_layer()
    return layer

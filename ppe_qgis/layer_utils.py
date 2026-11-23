"""QGIS raster loading and lightweight threshold rendering."""

import logging
import csv
import os
from html import escape as _html_escape


def escape(text):
    """Escape &, < and > for XML text (what xml.sax.saxutils.escape did)."""
    return _html_escape(text, quote=False)

from osgeo import gdal, osr

from qgis.PyQt.QtGui import QColor
from qgis.core import (
    Qgis,
    QgsPalettedRasterRenderer,
    QgsColorRampShader,
    QgsFillSymbol,
    QgsLayerTreeGroup,
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

_LOG = logging.getLogger(__name__)    # errors PPE deliberately ignores are kept here (debug level)


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


# Where PPE's layers go in the Layers panel. The results are layers of their own at the top level;
# only the working layers sit in the "Plant Pixel Extractor" group. From top to bottom:
TOP_ORDER = ("labels", "label_buffers", "label_area", "plants", "count_area", "plots", "pale", "mask", "__group__", "rgb")
GROUP_ORDER = ("crops", "votes")


def _project_group():
    root = QgsProject.instance().layerTreeRoot()
    group = root.findGroup(GROUP_NAME)
    if group is None:
        group = root.insertGroup(0, GROUP_NAME)
    return group


def _node_role(node):
    if isinstance(node, QgsLayerTreeGroup):
        if node.name() == GROUP_NAME and not node.customProperty(ROLE_KEY, ""):
            return "__group__"
        return node.customProperty(ROLE_KEY, "") or None    # PPE's own sub-groups (What PPE found)
    layer = node.layer() if hasattr(node, "layer") else None
    return layer.customProperty(ROLE_KEY, "") if layer is not None else None


def _move(parent, node, index):
    clone = node.clone(); parent.insertChildNode(index, clone); parent.removeChildNode(node)


def arrange_layers():
    """Put PPE's own layers in order (TOP_ORDER, then GROUP_ORDER inside the group). The user's
    layers keep their places: PPE's top-level layers are gathered where the first of them is."""
    root = QgsProject.instance().layerTreeRoot()
    for node in list(root.children()):          # a PPE sub-group whose layers are all gone
        if isinstance(node, QgsLayerTreeGroup) and node.customProperty(ROLE_KEY, "") and not node.children():
            root.removeChildNode(node)
    group = root.findGroup(GROUP_NAME)
    if group is not None:
        if not group.children():
            root.removeChildNode(group); group = None
        else:
            ranked = sorted([n for n in group.children() if _node_role(n) in GROUP_ORDER], key=lambda n: GROUP_ORDER.index(_node_role(n)))
            for index, node in enumerate(ranked):
                if group.children().index(node) != index:
                    _move(group, node, index)
    ours = [n for n in root.children() if _node_role(n) in TOP_ORDER]
    if not ours:
        return
    start = min(root.children().index(n) for n in ours)
    for offset, role in enumerate(sorted({_node_role(n) for n in ours}, key=TOP_ORDER.index)):
        node = next(n for n in root.children() if _node_role(n) == role)
        target = start + offset
        if root.children().index(node) != target:
            _move(root, node, target)
    # The plant count points always sit at the very top of the Layers panel, so they draw over everything.
    plants = next((n for n in root.children() if _node_role(n) == "plants"), None)
    if plants is not None and root.children().index(plants) != 0:
        _move(root, plants, 0)


def place_layer(layer, role, visible=True, parent=None):
    """Add a layer PPE made to the project at its place (see TOP_ORDER). Its legend starts
    folded, so the Layers panel stays short; the arrow beside it opens it."""
    layer.setCustomProperty(ROLE_KEY, role)
    QgsProject.instance().addMapLayer(layer, False)
    root = QgsProject.instance().layerTreeRoot()
    if parent is not None:
        parent.addLayer(layer)
    else:
        (_project_group() if role in GROUP_ORDER else root).insertLayer(0, layer)
    arrange_layers()
    node = root.findLayer(layer.id())
    if node is not None:
        node.setItemVisibilityChecked(visible)
        node.setExpanded(False)
    return layer


def set_layer_visible(layer, visible):
    """Tick or untick a layer in the Layers panel."""
    if layer is None:
        return
    try:
        node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
    except RuntimeError:
        return
    if node is not None and node.itemVisibilityChecked() != bool(visible):
        node.setItemVisibilityChecked(bool(visible))


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
    arrange_layers()


def remove_role(role):
    project = QgsProject.instance()
    layer_ids = [
        layer.id() for layer in project.mapLayers().values()
        if layer.customProperty(ROLE_KEY, "") == role
    ]
    if layer_ids:
        project.removeMapLayers(layer_ids)
    arrange_layers()


def _add_raster(path, name, role, group, visible=True):
    if not path or not os.path.isfile(path):
        return None
    layer = QgsRasterLayer(path, name, "gdal")
    if not layer.isValid():
        return None
    return place_layer(layer, role, visible)


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
        # the upper bound is far above 1: faint plants added to the mask are drawn from vote x (1 / their
        # lowest vote) and reach up to 1 / 0.10 = 10 (layer_utils.preview_source); 1.000001 left them undrawn
        QgsColorRampShader.ColorRampItem(
            1000.0, QColor(220, 43, 155, 235), "Plant preview"
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


def zoom_to_layer(iface, layer):
    """Zoom the map to a layer, in the map's own coordinate system (the project may use another one,
    e.g. after an image from a different UTM zone)."""
    canvas = iface.mapCanvas()
    try:
        extent = canvas.mapSettings().layerExtentToOutputExtent(layer, layer.extent())
    except Exception:
        extent = layer.extent()
    canvas.setExtent(extent)


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
        zoom_to_layer(iface, rgb)
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
        zoom_to_layer(iface, rgb)
        iface.mapCanvas().refresh()
    return rgb


def replace_applied_mask(result, layers):
    old = layers.get("mask")
    if old is not None:
        QgsProject.instance().removeMapLayer(old.id())
    group = _project_group()
    mask = _add_raster(result.get("mask_qgis_path"), "PPE · Applied plant mask", "mask", group, True)
    if mask is not None:
        try:
            mask.dataProvider().reloadData()      # read the file as it is now, never a cached copy
        except Exception:
            _LOG.debug("PPE: ignored error", exc_info=True)
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
    """Keep the plot outlines above the image and masks (see TOP_ORDER)."""
    arrange_layers()


def load_plot_layer(uri, name, id_field="", show_labels=True):
    """Show the plot polygons on the map: yellow outlines, no fill, labelled with their IDs.
    The layer can be switched off in the Layers panel and restyled like any QGIS layer."""
    remove_role("plots")
    layer = QgsVectorLayer(uri, "{} ({})".format(PLOT_LAYER_NAME, name), "ogr")
    if not layer.isValid():
        return None
    symbol = QgsFillSymbol.createSimple({"style": "no", "outline_color": "255,221,0,255",
                                         "outline_width": "0.6", "outline_width_unit": "MM"})
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    set_plot_labels(layer, id_field, show_labels)
    return place_layer(layer, "plots")


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
    try:
        reuse = old is not None and old.isValid() and path and os.path.isfile(path)
    except RuntimeError:
        reuse = False
    if reuse:
        # the same layer reads the new raster: no new layer appears in the Layers panel
        old.setCustomProperty(PREVIEW_BASE_KEY, path)
        old.setDataSource(path, old.name(), "gdal")
        old.setCustomProperty(PREVIEW_BASE_KEY, path)
        return old
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


PREVIEW_BASE_KEY = "ppe/preview_base"
_FINDING_FILE_NAMES = {1: "plant_parts", 2: "leaf_edges", 3: "shaded", 4: "unclear", 5: "dry", 6: "sparse"}
_ADD_CODES = (6,)   # kept for older callers; since 4.6.5 every kind can be added


def _derived_vrt(path, ds, pixel_function, sources):
    """A one-band Float32 VRT computed by a built-in GDAL pixel function over `sources` (XML snippets)."""
    nodata = ds.GetRasterBand(1).GetNoDataValue()
    xml = ('<VRTDataset rasterXSize="{w}" rasterYSize="{h}">\n  <SRS>{srs}</SRS>\n  <GeoTransform>{gt}</GeoTransform>\n'
           '  <VRTRasterBand dataType="Float32" band="1" subClass="VRTDerivedRasterBand">\n{nd}'
           '    <PixelFunctionType>{fn}</PixelFunctionType>\n{src}  </VRTRasterBand>\n</VRTDataset>\n').format(
        w=ds.RasterXSize, h=ds.RasterYSize, srs=escape(ds.GetProjection() or ""), gt=", ".join(repr(v) for v in ds.GetGeoTransform()),
        nd="    <NoDataValue>{}</NoDataValue>\n".format(nodata) if nodata is not None else "", fn=pixel_function, src="".join(sources))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(xml)
    return path


def preview_source(base_path, findings_path, clip_codes, add_codes=(), add_vote_from=None):
    """The raster the live preview draws. Kinds left out: a small VRT multiplies the vote by 0 where
    such a kind is and by 1 elsewhere, so the preview drops them at every threshold. Kinds added
    (4.6.5: any kind): a second VRT adds 2 to the vote there, so they show at every threshold
    (the preview colours everything from the threshold up to 1000). Nothing is copied; the VRTs only
    point to the files. Without choices it is the vote itself. Not added with crop areas: their
    preview holds vote + 10 x crop number. (add_vote_from is no longer needed; kept for callers.)"""
    clip = sorted({int(c) for c in (clip_codes or []) if int(c) in _FINDING_FILE_NAMES})
    zoned = os.path.basename(str(base_path or "")).startswith("ppe_vote_by_crop")
    add = [] if zoned else sorted({int(c) for c in (add_codes or []) if int(c) in _FINDING_FILE_NAMES and int(c) not in clip})
    if not (clip or add) or not base_path or not findings_path or not os.path.isfile(findings_path):
        return base_path
    ds = gdal.Open(base_path)
    if ds is None:
        return base_path
    f_ds = gdal.Open(findings_path)
    if f_ds is None or f_ds.RasterXSize != ds.RasterXSize or f_ds.RasterYSize != ds.RasterYSize:
        return base_path
    codes = (0,) + tuple(sorted(_FINDING_FILE_NAMES)) + (255,)
    find = escape(os.path.abspath(findings_path))

    def lut_source(values):
        # NODATA 254 (a value findings never hold) so the no-data code 255 goes through the LUT
        return ('    <ComplexSource><SourceFilename relativeToVRT="0">{}</SourceFilename><SourceBand>1</SourceBand>'
                '<NODATA>254</NODATA><LUT>{}</LUT></ComplexSource>\n').format(find, ",".join("{}:{}".format(c, values(c)) for c in codes))
    stem = os.path.splitext(os.path.basename(base_path))[0]; folder = os.path.dirname(os.path.abspath(base_path))
    name = "{}_preview{}{}".format(stem, "_without_" + "_".join(_FINDING_FILE_NAMES[c] for c in clip) if clip else "",
                                   "_with_" + "_".join(_FINDING_FILE_NAMES[c] for c in add) if add else "")
    vote = '    <SimpleSource><SourceFilename relativeToVRT="0">{}</SourceFilename><SourceBand>1</SourceBand></SimpleSource>\n'.format(
        escape(os.path.abspath(base_path)))
    try:
        current = vote
        if clip:
            inner = _derived_vrt(os.path.join(folder, name + ("_cut" if add else "") + ".vrt"), ds, "mul",
                                 [vote, lut_source(lambda c: 0 if c in clip else 1)])
            if not add: return inner
            current = '    <SimpleSource><SourceFilename relativeToVRT="0">{}</SourceFilename><SourceBand>1</SourceBand></SimpleSource>\n'.format(escape(inner))
        return _derived_vrt(os.path.join(folder, name + ".vrt"), ds, "sum", [current, lut_source(lambda c: 2 if c in add else 0)])
    except OSError:
        return base_path


def set_preview_source(layer, base_path=None, findings_path="", clip_codes=(), add_codes=(), add_vote_from=None):
    """Point the live-preview layer at the vote, or at the vote without the left-out kinds. The
    layer stays the same layer (same place, same tick box); only what it reads changes."""
    if layer is None or not layer.isValid():
        return layer
    base = base_path or layer.customProperty(PREVIEW_BASE_KEY, "") or layer.source()
    layer.setCustomProperty(PREVIEW_BASE_KEY, base)
    wanted = preview_source(base, findings_path, clip_codes, add_codes, add_vote_from)
    if os.path.normpath(layer.source()) != os.path.normpath(wanted):
        layer.setDataSource(wanted, layer.name(), "gdal")
        layer.setCustomProperty(PREVIEW_BASE_KEY, base)
    return layer


def _raise_role(role):
    """Kept for the callers: PPE's layers are put in order by arrange_layers."""
    arrange_layers()


PALE_LAYER_NAME = "PPE · What PPE found"
FINDING_COLOURS = ((1, (255, 176, 0), "Plant parts that are not green"), (2, (90, 205, 255), "Leaf edges over soil"),
                   (3, (40, 80, 230), "Shaded leaves"), (4, (240, 50, 90), "Unclear"),
                   (5, (150, 90, 40), "Dry or yellowing plant parts"), (6, (150, 230, 60), "Faint plants in sparse spots"))
FINDING_COLOUR_NAMES = {1: "amber", 2: "light blue", 3: "blue", 4: "red", 5: "brown", 6: "light green"}


def _pale_group(create=True):
    root = QgsProject.instance().layerTreeRoot()
    for node in root.children():
        if isinstance(node, QgsLayerTreeGroup) and node.customProperty(ROLE_KEY, "") == "pale":
            return node
    if not create:
        return None
    group = root.insertGroup(0, PALE_LAYER_NAME)
    group.setCustomProperty(ROLE_KEY, "pale")
    return group


def show_pale_plant(path, visible=True, kinds=None):
    """Add (or remove) 'What PPE found': a group with one layer per kind, each in its own colour
    and clear elsewhere, so each kind can be switched on and off on its own. All the layers read
    the same file. Information only; it is not part of the mask.
    kinds: [(code, [codes drawn]), ...] to show; by default every kind."""
    remove_role("pale")
    if not visible or not path or not os.path.isfile(path):
        return None
    colours = {code: (rgb, name) for code, rgb, name in FINDING_COLOURS}
    kinds = kinds if kinds is not None else [(code, [code]) for code in colours]
    layers = []
    for code, codes in kinds:
        rgb, name = colours[code]
        # the colour is in the name: a folded layer shows no colour square in the Layers panel
        layer = QgsRasterLayer(path, "{} ({})".format(name, FINDING_COLOUR_NAMES[code]), "gdal")
        if not layer.isValid():
            continue
        _force_nearest(layer)
        classes = [QgsPalettedRasterRenderer.Class(c, QColor(*rgb, 245), name) for c in codes]
        layer.setRenderer(QgsPalettedRasterRenderer(layer.dataProvider(), 1, classes))
        layers.append(place_layer(layer, "pale", True, parent=_pale_group()))
    group = _pale_group(create=False)
    if group is not None:
        group.setExpanded(True)                  # the kinds stay listed: each has its own tick box
    arrange_layers()
    return layers[0] if layers else None


PLANT_POINT_SIZE = 3.8        # mm: large enough to see over the image at any zoom


def show_plant_points(path, name="PPE · Plants"):
    """Load the plant count points (ppe_plants.gpkg) as the top layer, one colour per status: plant
    yellow, check orange, added by the user blue, weed? grey with a red ring (not counted). Replaces an earlier count."""
    from qgis.core import QgsCategorizedSymbolRenderer, QgsMarkerSymbol, QgsRendererCategory, QgsUnitTypes, QgsVectorLayer
    remove_role("plants")
    layer = QgsVectorLayer(path + "|layername=plants", name, "ogr")
    if not layer.isValid():
        return None

    def dot(fill, outline="0,0,0"):
        sym = QgsMarkerSymbol.createSimple({"name": "circle", "color": fill, "outline_color": outline, "outline_width": "0.45",
                                            "size": str(PLANT_POINT_SIZE)})
        sym.setSizeUnit(QgsUnitTypes.RenderMillimeters)
        return sym
    renderer = QgsCategorizedSymbolRenderer("status", [
        QgsRendererCategory("plant", dot("255,230,0"), "Plant · counted"),
        QgsRendererCategory("check", dot("255,140,0"), "Check · counted as 1 plant; may be several touching plants (✂ Split)"),
        QgsRendererCategory("split", dot("150,230,0"), "Split by you · each counted"),
        QgsRendererCategory("added", dot("0,200,255"), "Added by you · counted"),
        QgsRendererCategory("weed?", dot("170,170,170", "220,0,0"), "Weed? (between rows) · NOT counted")])
    layer.setRenderer(renderer)
    place_layer(layer, "plants")
    layer.triggerRepaint()
    return layer


def show_label_points(path, name="PPE · Label points"):
    """The label points (ppe_labels.gpkg) as the top layer: plant green, soil orange; a point the user
    changed or added has a white ring. Replaces earlier label points."""
    from qgis.core import QgsCategorizedSymbolRenderer, QgsMarkerSymbol, QgsRendererCategory, QgsUnitTypes, QgsVectorLayer
    remove_role("labels"); remove_role("label_buffers")
    layer = QgsVectorLayer(path + "|layername=points", name, "ogr")
    if not layer.isValid():
        return None

    def dot(fill, outline="0,0,0", width="0.45"):
        sym = QgsMarkerSymbol.createSimple({"name": "circle", "color": fill, "outline_color": outline, "outline_width": width,
                                            "size": str(PLANT_POINT_SIZE)})
        sym.setSizeUnit(QgsUnitTypes.RenderMillimeters)
        return sym
    expr = "\"label\" || if(\"status\" in ('changed', 'added'), ' yours', '')"
    layer.setRenderer(QgsCategorizedSymbolRenderer(expr, [
        QgsRendererCategory("plant", dot("0,220,0"), "Plant"),
        QgsRendererCategory("soil", dot("255,150,0"), "Soil"),
        QgsRendererCategory("plant yours", dot("0,220,0", "255,255,255", "0.9"), "Plant · changed or added by you"),
        QgsRendererCategory("soil yours", dot("255,150,0", "255,255,255", "0.9"), "Soil · changed or added by you")]))
    place_layer(layer, "labels")
    layer.triggerRepaint()
    return layer


def show_label_buffers(path, name="PPE · Label buffers"):
    """The buffer polygons (outlines only: plant green, soil orange), just under the points."""
    from qgis.core import QgsCategorizedSymbolRenderer, QgsFillSymbol, QgsRendererCategory, QgsVectorLayer
    remove_role("label_buffers")
    layer = QgsVectorLayer(path + "|layername=buffers", name, "ogr")
    if not layer.isValid():
        return None
    layer.setRenderer(QgsCategorizedSymbolRenderer("label", [
        QgsRendererCategory("plant", QgsFillSymbol.createSimple({"color": "0,220,0,40", "outline_color": "0,220,0", "outline_width": "0.4"}), "Plant buffer"),
        QgsRendererCategory("soil", QgsFillSymbol.createSimple({"color": "255,150,0,40", "outline_color": "255,150,0", "outline_width": "0.4"}), "Soil buffer")]))
    place_layer(layer, "label_buffers")
    layer.triggerRepaint()
    return layer


AREA_RED = "225,25,25"          # every area drawn in PPE (labels, count, crops) has this solid red outline


def area_symbol():
    """The outline of an area drawn in PPE: solid red, 0.9 mm, no fill, the same everywhere."""
    from qgis.core import QgsFillSymbol
    return QgsFillSymbol.createSimple({"style": "no", "outline_color": AREA_RED, "outline_width": "0.9", "outline_width_unit": "MM",
                                       "outline_style": "solid", "joinstyle": "round"})


def show_label_area(geometries, crs, name="PPE · Label area"):
    return show_area_layer(geometries, crs, "label_area", name)


def show_area_layer(geometries, crs, role, name):
    """An area drawn for a goal (solid red outline, as every area drawn in PPE): the label area or the count area.
    geometries: QgsGeometry in `crs`; an empty list removes the layer."""
    from qgis.core import QgsFeature, QgsFillSymbol, QgsSingleSymbolRenderer, QgsVectorLayer
    remove_role(role)
    if not geometries or crs is None:
        return None
    layer = QgsVectorLayer("MultiPolygon", name, "memory"); layer.setCrs(crs)
    feats = []
    for g in geometries:
        f = QgsFeature(); f.setGeometry(g); feats.append(f)
    layer.dataProvider().addFeatures(feats); layer.updateExtents()
    layer.setRenderer(QgsSingleSymbolRenderer(area_symbol()))
    place_layer(layer, role)
    return layer


# ---- which layers belong to which goal (4.6.2) -------------------------------------------------
# Switching the goal hides the layers of the other goals and shows this goal's again, as the user
# last left them in this goal. Nothing is deleted: switching back brings them back. The image is
# common to every goal; the vote, mask, findings, crop and plot layers to the goals with Run/Review.
_MASK_ROLES = {"rgb", "votes", "mask", "pale", "crops"}
GOAL_ROLES = {"mask": _MASK_ROLES | {"plots"},
              "count": _MASK_ROLES | {"plots", "plants", "count_area"},
              "training": set(_MASK_ROLES),
              "labels": {"rgb", "labels", "label_buffers", "label_area"}}
GOAL_VIS_KEY = "ppe/goal_visible/"


def _ppe_nodes():
    """PPE's own layers and sub-groups (with their role), top level and inside the PPE group;
    the layers inside a sub-group follow the sub-group."""
    root = QgsProject.instance().layerTreeRoot(); out = []
    for node in root.children():
        role = _node_role(node)
        if role == "__group__":
            out += [(n, _node_role(n)) for n in node.children() if _node_role(n)]
        elif role:
            out.append((node, role))
    return out


def switch_goal_layers(old_goal, new_goal):
    """Hide the layers that do not belong to new_goal and show those that do (as they were when the
    user last left new_goal). Returns how many layers were hidden."""
    roles = GOAL_ROLES.get(new_goal)
    if roles is None: return 0
    hidden = 0
    for node, role in _ppe_nodes():
        if old_goal in GOAL_ROLES:
            node.setCustomProperty(GOAL_VIS_KEY + old_goal, bool(node.itemVisibilityChecked()))
        if role in roles:
            saved = node.customProperty(GOAL_VIS_KEY + new_goal, None)
            if saved is not None:
                node.setItemVisibilityChecked(saved in (True, "true", "True", 1, "1"))
        else:
            if node.itemVisibilityChecked(): hidden += 1
            node.setItemVisibilityChecked(False)
    group = QgsProject.instance().layerTreeRoot().findGroup(GROUP_NAME)
    if group is not None:                  # the PPE group itself: shown only when one of its layers is
        group.setItemVisibilityChecked(any(n.itemVisibilityChecked() for n in group.children()))
    return hidden


# ---- label buffers that follow the slider (4.6.8) ------------------------------------------------
LABEL_BUFFER_SOIL_OFFSET = 1000.0      # as label_points.SOIL_OFFSET


def style_label_buffers(layer, radius_cm):
    """Colour the buffer distance raster for one radius: plant pixels within radius_cm of their point
    green, soil pixels orange, the rest clear. Only the colours change, so this is instant."""
    from qgis.core import QgsColorRampShader, QgsRasterShader, QgsSingleBandPseudoColorRenderer
    if layer is None or not layer.isValid():
        return
    r = max(0.0, float(radius_cm)) + 1e-4
    ramp = QgsColorRampShader(); ramp.setColorRampType(QgsColorRampShader.Discrete)
    ramp.setColorRampItemList([
        QgsColorRampShader.ColorRampItem(r, QColor(0, 230, 0, 170), "Plant label (within {:.1f} cm)".format(radius_cm)),
        QgsColorRampShader.ColorRampItem(LABEL_BUFFER_SOIL_OFFSET - 0.5, QColor(0, 0, 0, 0), ""),
        QgsColorRampShader.ColorRampItem(LABEL_BUFFER_SOIL_OFFSET + r, QColor(255, 150, 0, 170), "Soil label (within {:.1f} cm)".format(radius_cm)),
        QgsColorRampShader.ColorRampItem(1e9, QColor(0, 0, 0, 0), "")])
    shader = QgsRasterShader(); shader.setRasterShaderFunction(ramp)
    renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    layer.setRenderer(renderer); _force_nearest(layer); layer.triggerRepaint()


def show_label_buffer_raster(path, radius_cm, name="PPE · Label buffers"):
    """The labelled pixels around the points for the slider's radius (filled: plant green, soil
    orange), just under the points. Replaces earlier buffers."""
    remove_role("label_buffers")
    if not path or not os.path.isfile(path):
        return None
    layer = QgsRasterLayer(path, name, "gdal")
    if not layer.isValid():
        return None
    style_label_buffers(layer, radius_cm)
    return place_layer(layer, "label_buffers")


def label_buffer_layer():
    for layer in QgsProject.instance().mapLayers().values():
        if layer.customProperty(ROLE_KEY, "") == "label_buffers":
            return layer
    return None

"""QGIS entry point for Plant Pixel Extractor."""


def classFactory(iface):  # noqa: N802 - QGIS requires this name
    from .plugin import PPEPlugin

    return PPEPlugin(iface)

#!/bin/sh
# Build the ZIP for plugins.qgis.org: one top-level folder, no caches.
set -e
cd "$(dirname "$0")"
VERSION=$(sed -n 's/^version=//p' ppe_qgis/metadata.txt)
rm -f "ppe_qgis-$VERSION.zip"
zip -qr "ppe_qgis-$VERSION.zip" ppe_qgis -x "*__pycache__*" -x "*.pyc"
echo "Built ppe_qgis-$VERSION.zip"

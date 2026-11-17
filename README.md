# Plant Pixel Extractor (PPE) for QGIS

PPE separates plant pixels from soil in drone orthomosaics, with no training data. It works with RGB cameras, multispectral sensors (MicaSense, Parrot Sequoia, DJI and others) and hyperspectral cubes, inside QGIS, using only the GDAL and NumPy that ship with QGIS.

**How it works.** PPE ranks the vegetation indices the image allows by how well they spread plant from soil, splits the three best with four automatic thresholds (Otsu, CDF knee, k-means and multi-Otsu) and lets the twelve results vote. Guards then remove shadows, algae on wet soil and grey objects. For images with a NIR band, PPE also suggests a vote threshold (the one that best matches pixels that are at least half plant) and the slider starts there; a live slider previews the mask on the map before it is written.

- Plugin source and full user guide: [`ppe_qgis/README.md`](ppe_qgis/README.md)
- Version history: `changelog` in [`ppe_qgis/metadata.txt`](ppe_qgis/metadata.txt)
- Unit tests: `tests/` (run from the repository root with `python -m unittest discover -s tests`; needs GDAL and NumPy)
- Build the ZIP for QGIS: `./build_plugin.sh`

## Install

In QGIS 3.28 or newer: **Plugins → Manage and Install Plugins**, search for **Plant Pixel Extractor**, then **Install Plugin**. Or install a ZIP from the releases page with **Install from ZIP**.

## Issues

Report problems at <https://github.com/Leoanish/ppe-qgis/issues>, with the log file from the run folder, the QGIS version, the sensor and the raster size.

## License

GNU General Public License, version 2 or later. See [`LICENSE`](LICENSE).

Author: Anish Bhattarai, University of Georgia.

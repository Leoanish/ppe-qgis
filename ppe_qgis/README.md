# Plant Pixel Extractor for QGIS

Separates plant pixels from soil in RGB, multispectral and hyperspectral drone imagery, without training data. It runs inside QGIS using the GDAL and NumPy that ship with QGIS. No R installation is needed.

## Install

From QGIS (once the plugin is approved in the official repository):

1. Open QGIS 3.28 or newer.
2. Choose **Plugins → Manage and Install Plugins → All**, search for **Plant Pixel Extractor** and click **Install Plugin**.
3. Open it from the Raster menu or the leaf toolbar button.

From a ZIP: choose **Plugins → Manage and Install Plugins → Install from ZIP** and select `ppe_qgis-*.zip` (from the [releases page](https://github.com/Leoanish/ppe-qgis/releases) or built with `build_plugin.sh`).

Nothing else needs installing: PPE uses the GDAL and NumPy that come with QGIS.

Bugs and questions: <https://github.com/Leoanish/ppe-qgis/issues>. Please attach the log file from the run folder (see *Support and logs*).

## Workflow

| Tab | What you do |
|---|---|
| **1 Image** | Choose a stacked GeoTIFF, a folder of band files, or individual band files. The image appears on the map straight away, and the tab lists the bands that were found. |
| **2 Run** | Build the plant vote. |
| **3 Review** | Move the threshold slider, look at the canopy curve, then finalize the mask. |
| **4 Export** | Write plant-only bands and vegetation indices as GeoTIFFs. |
| **5 Plots** | Statistics of plant pixels for each plot polygon, as a CSV. |

## What you see on the map

As soon as an image is chosen it is added to the map, before anything is analysed:

| Bands in the image | Shown as |
|---|---|
| Red, green and blue | True colour |
| One of those missing, but NIR, red and green present | False colour (NIR, red, green): plants red, soil grey-blue |
| Anything else | One band in grey, NIR if there is one: plants bright, soil darker |

Run then adds the plant vote on top of the same picture (the live preview), and Finalize adds the applied plant mask. PPE adds nothing else to the Layers panel: the rasters showing which pixels each guard removed (shadows, algae, bright soil) stay in the run folder (`ppe_YYYYMMDD_HHMMSS` inside the working folder set under Folders) for anyone who wants to inspect them.

## Changing or removing the image

- **Browse** when an image is already chosen asks before replacing it. So does switching the source type.
- A new image starts PPE afresh: results, review settings, logs, export and plot settings and the old PPE layers on the map are all cleared.
- **✕** beside the path removes the image from PPE and from the map.
- Deleting the PPE image layer in the QGIS Layers panel resets PPE as well. Deleting only the live-preview layer switches the slider off until PPE is run again.
- While PPE is running the image cannot be changed; cancel the run first.

## How the mask is made (version 3.7.7)

PPE follows three steps: pick the layers with the widest spread, split each one four ways, and count the votes.

1. **Sample.** A regular grid of pixels is read once and used for everything that is fitted.
2. **Rank layers by spread.** Every normalised-difference index the bands allow is a candidate. Spread is the interquartile range divided by the 1–99 % range of the same index, so it has no units and an index cannot win just by running on bigger numbers. Indices built from the same pair of bands form a group, and only the highest-spread member of a group can be chosen:

   | Group | Indices |
   |---|---|
   | NIR and red | NDVI, ARVI |
   | NIR and red edge | NDRE |
   | NIR and green | GNDVI |
   | Red edge and red | NDRER |
   | Visible | GLI, NGRDI, VARI |

   The three groups with the highest spread vote. An RGB image has only the visible group, so it uses up to three visible indices. All other indices (SAVI, EVI, ExG, simple ratios and so on) are still exported but never vote.
3. **Split each layer four ways.**
   - *Otsu*: the cut that best separates two groups of values.
   - *CDF*: the knee of the sorted-value curve, where the dominant class ends. The lowest and highest 0.5 % of values are ignored. If the curve has two knees of similar strength, both are kept and pixels between them get half a vote.
   - *k-means*: ten clusters, each called plant or soil by the Otsu cut.
   - *multi-Otsu*: three classes, soil 0, mixed 0.5, plant 1.

   The thresholds are fitted to the whole field. With **Follow local conditions** (the default, under Advanced settings) the field is then divided into tiles of about 512 pixels, Otsu is refitted in each tile that holds both soil and plants, and all four thresholds of a layer move up or down together by the difference, blended smoothly between tiles. This keeps pale or small plants in weak plots. The move is limited to the mixed band of multi-Otsu.

   **Half-mixture** (optional, off by default) adds a fifth threshold per layer: the index value of a pixel that is half typical plant and half typical soil. It does not depend on how much of the field each class covers, so it helps in dense crops, where Otsu and k-means cut too high.
4. **Vote.** Three layers times four methods gives 12 equal votes (15 with half-mixture). The vote raster holds the share that said plant, from 0 to 1.
5. **Guards** (each can be switched off).
   - *Dark shadows*: rejects the darkest pixels unless they still reflect enough near-infrared light to be a lit leaf, or their NDVI (and NDRE, when there is a red-edge band) is at plant level, which is a leaf in shade.
   - *Algae in furrows*: algae are green but lack a leaf's rise from red edge to NIR. A pixel whose NDRE is less than a quarter of the way from bare soil to a typical plant is rejected. Needs red-edge and NIR bands; skipped otherwise.
   - *Grey and neutral objects* (camera RGB images with no NIR band): a pixel stays plant only if it is greener than the soil's own colour. Vehicles, tarps, grey residue and black plastic no longer pass a visible index on brightness alone. If plants are barely greener than soil anywhere in the image, the run warns you.
   - *Bright soil and residue*: rejects very bright, colourless pixels with no vegetation signal. Off by default.
6. **Warnings.** Thresholds always split an image in two. If even the greenest 1 % of pixels look like soil, or even the least green 1 % look like canopy, or (camera images) plants are barely greener than soil, a yellow bar on the map and a note on the Review tab tell you to check the mask. The mask itself is not changed.
7. **Mask.** A pixel is plant when its vote is at least the threshold minus 0.05.

The log and `run_summary.json` list the spread ranking, the chosen layers and all four thresholds for each layer.

## Band detection

Bands are identified from, in order: colour tags and band names in the file, wavelength metadata, file names, then sensor order. Names from MicaSense (Blue, Green, Red, Red Edge, NIR), Parrot Sequoia (GRE, RED, REG, NIR), DJI multispectral (MS_G, MS_R, MS_RE, MS_NIR) and wavelength names such as `717nm` are recognised. Thermal bands (LWIR, TIR, thermal, temperature) are ignored. The Image tab reports the image type from the bands found, not the band count: a file or folder with only NIR, red edge and red is multispectral. A 3-band file with no names, colour tags or wavelengths asks for its band order: red, green, blue (camera RGB, the default); blue, green, red; NIR, red edge, red; or NIR, red, green (colour-infrared camera). For a hyperspectral cube, give the first wavelength, last wavelength and step; one band is chosen in each of the blue, green, red, red-edge and NIR windows.

## Tested on

Real fields, each processed whole in one run and checked on close-ups and labelled pixels. Details, figures and scripts are in `04_test_scripts/` (`tifton_test`, `ih_test`, `kritis_and_sensors_test`).

| Field | Sensor | Stage | Canopy | Check |
|---|---|---|---|---|
| Tifton, 22 May 2024 | 5-band MS | early, sparse rows, algae in furrows | 41.7 % | algae called plant 1.5 %, IoU 83 |
| IH, 6 June 2023 | 5-band MS | later, plots of very different vigour | 50.7 % | IoU 72 (81 with half-mixture) |
| Kritis, 8 Aug 2025 | 5-band MS, 1.5 cm | very dense, late; grass headlands | 82.6 % | shaded leaves kept 99.8 %, soil called plant 0 % |
| Midville, 14 July | NIR, red edge, red only | mid season, rows | 47.0 % | rows kept, alleys excluded (visual) |
| RGB_transparent | camera RGB, 0–255 | small plants | 12.6 % | plots kept; a truck no longer called plant |
| trigo | camera RGB | mostly residue | — | warns that the image looks like soil only |

The Kritis field was also cut into sensor variants and compared with the full 5-band mask (IoU): Altum file with an LWIR band 100, extra thermal file 100, uncalibrated uint16 100, Sequoia names 98.2, DJI names 98.2, red + NIR only 93.1, unnamed 8-bit RGB 92.2, RGB only 92.0, and a 57-band hyperspectral cube built from it 96.7.

## Known limits

- Real hyperspectral orthomosaics have not been run yet (only a cube made from real 5-band data). Thermal-only images cannot be segmented: they carry no plant–soil reflectance contrast.
- The default vote threshold of 0.5 is not equally right everywhere. In the dense IH field a threshold of 0.3 to 0.4 agrees better with the reference; at Tifton 0.5 to 0.6 does. Use the curve and the preview.
- The algae guard cannot work on images without a red-edge band, and at a quarter of the red-edge rise it lets the greenest algae through if the chosen layers also accept it.
- Plants with very low chlorophyll have a weak red-edge rise and could still be rejected by the algae guard. Untick it and compare if you see gaps in yellow or stressed canopy.
- On camera RGB images green weeds and grass are plants to PPE; it cannot tell crop from weed.
- An image of bare soil only, or of closed canopy only, is still split in two. PPE warns about it.

## Support and logs

Each output folder contains `logs/ppe_YYYYMMDD_HHMMSS.log`. Include that file, the QGIS version, sensor and raster size in a bug report.

## License

GPL-2.0-or-later. Copyright 2026 Anish Bhattarai.

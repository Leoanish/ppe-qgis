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
| **1 Image** | Choose a stacked GeoTIFF, a folder of band files, or individual band files. The image appears on the map straight away, and the tab lists the bands that were found. Optionally, describe the field (growth stage, dates, soil moisture, weeds, notes). |
| **2 Run** | Build the plant vote. |
| **3 Review** | Move the threshold slider, look at the canopy curve, then finalize the mask. With several crops in the image, draw each crop's area and give it its own slider. |
| **4 Export** | Write plant-only bands and vegetation indices as GeoTIFFs. |
| **5 Plots** | Canopy cover of each plot and statistics of its plant pixels, as CSV files, with their own progress bar. |

## Band folders: tick the band files

When you choose a **folder of band files**, PPE lists every GeoTIFF in it:

| Column | What it shows |
|---|---|
| Use | ticked = imported. Files PPE recognised as bands start ticked. |
| File | the file name (hover for the full path and size) |
| Band | the band PPE recognised from the file name, the band name inside the file or its wavelength; change it if it is wrong |
| Note | why a file starts unticked: thermal, a stacked file with several bands, band not recognised, a second file for the same band, or a different size |

Untick files that are not bands of the image, choose the band of files PPE could not recognise (for example `IMG_0001_4.tif`), and press **Use the ticked files**. PPE imports exactly those files; the dialog will not accept two files for one band, a ticked file without a band, or files of different sizes. Other files in the folder (`.aux.xml`, plot files, sub-folders) are not listed. **Choose band files…** under the folder opens the list again. Cancel keeps the automatic reading by file name. Long file names are shown in full (the table scrolls sideways; the dialog can be resized), and the dialog stays readable when macOS or Windows is in dark mode.

When a file name holds more than one band word, the one nearest the end is the band: `Field_MicaSense_RedEdge-P_ortho_blue.tif` is blue, not red edge.

## What you see on the map

As soon as an image is chosen it is added to the map, before anything is analysed:

| Bands in the image | Shown as |
|---|---|
| Red, green and blue | True colour |
| One of those missing, but NIR, red and green present | False colour (NIR, red, green): plants red, soil grey-blue |
| Anything else | One band in grey, NIR if there is one: plants bright, soil darker |

**True colour keeps the real colour balance.** Red, green and blue share one stretch, from zero reflectance to the 99.5th percentile of the three bands together, so soil looks brown or orange and leaves a natural green. (Stretching each band on its own, as PPE did before 3.9.0, made soil grey-white and leaves cyan-green, and made pale tassels look bright green.) The stretch is measured on a 10 × 10 grid of small windows when the image is chosen, so it appears within a second or two, and on the full fitting sample at Run, which writes the same look into `ppe_rgb_display.tif`. False-colour and single-band pictures keep a stretch per band.

Run then adds the plant vote on top of the same picture (the live preview), and Finalize adds the applied plant mask.

**In the Layers panel** the results are layers of their own; only the working layers sit in the *Plant Pixel Extractor* group. From top to bottom: *PPE · Plots*, the *PPE · What PPE found* group (one layer per kind, when shown), *PPE · Applied plant mask*, the *Plant Pixel Extractor* group (*PPE · Crop areas*, *PPE · Live threshold preview*), and the image. PPE's layers come in folded, so the panel stays short; the arrow beside a layer shows its legend. Your own layers keep their places. PPE adds nothing else to the Layers panel: the rasters showing which pixels each guard removed (shadows, algae, bright soil) stay in the run folder (`ppe_YYYYMMDD_HHMMSS` inside the working folder set under Folders) for anyone who wants to inspect them.

On tab **5 Plots**, the chosen plot file is added as **PPE · Plots**: yellow outlines with each plot's ID, kept above the image. Untick *Show plot IDs on the map* to hide the labels, untick the layer in the Layers panel to hide the plots, or restyle it there like any QGIS layer.

## Several crops in one image

Different crops can need different thresholds; on a corn field at tasselling, for example, a lower threshold keeps the tassels. On the **Review** tab, under *Crops in this image*:

1. **+ Add crop** and type its name (any number of crops).
2. **Draw area**: click the corners of a rough outline around that crop on the map, right-click to finish (Esc cancels, Backspace removes the last corner). While you draw, that crop's button turns grey and reads **✎ Drawing…**, the other crops cannot start drawing, the note above the crops and a message on the map say what to do, and choosing another map tool (Pan, Identify…) ends the drawing. A ✓ appears and the outline is shown as **PPE · Crop areas**. **Redraw** replaces it, ✕ removes the crop.
3. Each drawn crop gets its own slider and canopy; the slider above sets the rest of the image (headlands, roads). *Use the slider above for every crop* moves them all together. The live preview shows every area at its own threshold.
4. **Finalize** writes one mask, `ppe_plant_mask_by_crop.tif`, and reports the canopy of each crop.

Only the threshold differs between areas; the vote is the same as without crop areas, so a crop slider at the same value as the main slider gives exactly the usual mask. Where outlines overlap, the crop listed first wins. Every crop slider starts at the suggested threshold of the whole image: a suggestion per crop area was tested on Kritis and was not reliable on closed canopy (it gave 0.95 for soybean, which would drop shaded leaves). Preparing the areas takes a few seconds (7 s on a 9371 × 10056 field) and moving a slider is instant.

## What PPE found: four kinds of pixel, each can be left out and put back

Every run sorts out four kinds of pixel worth pointing out. Nothing is guessed from the crop: each is decided from the image's own references (its typical green leaf and its bare soil in sun) and from where the pixel sits.

| Kind | How PPE tells | What it means |
|---|---|---|
| ✿ Plant parts that are not green | as much near-infrared light as plant, a plant-level NDVI but not a leaf's green, inside closed canopy (90 % plant within 0.5 m, no soil within 15 cm) | tassels, flowers, heads, pods, yellowing leaves (named from the crop and stage you give) |
| ◌ Leaf edges over soil | the same colour, but beside soil or in open canopy | a little soil in the pixel makes a leaf edge look pale (what an earlier pale-plant note mistook for flowers in young corn) |
| ● Shaded leaves | less than half as bright as a typical leaf, but more near-infrared light than bare soil in full sun can reflect | leaves under the top of the canopy: plant |
| ? Unclear | as dark, but no more near-infrared light than soil in sun | could be a leaf in deep shade or soil in shade |

On tab **3 Review** a closed card, *What PPE found: n things to look at*, opens to one short note per kind present (from 0.2 % of the plant). Each has **Leave out of the mask**. A tick takes those pixels out of the live preview at once, the same live layer, and they stay out whatever the slider (or the crop sliders) says. The card gives the share of the plant and, without crop areas, the exact number of pixels. **Finalize mask** writes the mask without them (to a file of its own, such as `ppe_plant_mask_0.45_without_shaded.tif`) and reports the pixels it left out, counted in the file, with the canopy before and after. Untick, or **Put all back**, to return them, then finalize again. The mask is always rebuilt from the vote, so leaving out and putting back loses nothing. The live preview reads a small VRT next to the vote: the vote times 0 where a left-out kind is and times 1 elsewhere. Nothing is copied. Only one magenta layer is shown at a time: after Finalize the finalized mask; once a slider moves or a kind is ticked, the live preview (the finalized mask is switched off, and the Export tab says its exports still use it). **Show them on the map** adds a group, *PPE · What PPE found*, with one layer per kind in its own colour (plant parts amber, leaf edges light blue, shaded leaves blue, unclear red; the coloured square on the card is the same colour). Tick or untick each kind on its own; all four layers read the same file.

- The plot export gives each kind's share of every plot (`plant_parts_pct`, `leaf_edges_pct`, `shaded_leaves_pct`, `unclear_pct`, percent of the plot whatever the threshold) and `left_out_of_mask`; the run record keeps the shares and what was left out.
- Early in the season (*Early / vegetative* under *About this field*) pale spots are counted as leaf edges.
- Nothing is judged when leaves reflect less than 1.3 times the near-infrared light of sunlit soil (an uncalibrated band), or without red and NIR bands; shaded/unclear also need RGB and some bare soil.
- Tested (percent of the plant at 0.45): Kritis plant parts 1.6, leaf edges 1.8, shaded leaves 3.3, unclear 1.7 (dense corn plots 4–5 % plant parts, soybean plots ~0); IH corn at V6 plant parts 0.07, leaf edges 8.1, shaded 0.2, unclear 0.01; Tifton not judged; Midville not judged (no blue band). The vote is the same as 3.9.0 bit for bit on all four fields.
- Wording for each crop and stage lives in `plant_parts.py`.

## About this field (optional) and the run record

On the **Image** tab, *About this field* takes the growth stage, planting and flight dates, soil moisture, weeds and notes. Nothing there changes the mask. At *Flowering / tasselling* or *Drying down* the Review tab shows a threshold tip. Finalize saves `ppe_run_record.json` (image, chosen layers, suggestion, final thresholds, canopy per crop and the field information); the plot export copies it and adds the crop, the threshold used and the field information to `ppe_plot_canopy.csv`. Kept over many fields and dates, these records can show which thresholds suit which crops and stages.

## Plot statistics

**Plot files.** Any polygon file QGIS reads:

| Format | Choose |
|---|---|
| Shapefile | the `.shp`, or any of its side files (`.dbf`, `.shx`, `.prj`, `.cpg`); keep them together in one folder |
| Zipped Shapefile (or GeoPackage, GeoJSON, KML) | the `.zip`; PPE finds the plot file inside, also in a sub-folder |
| GeoPackage, GeoJSON, KML/KMZ, GML, FlatGeobuf, SpatiaLite, MapInfo TAB | the file; if it holds several polygon layers, choose one in *Layer* |

PPE suggests the plot identifier (a field called plot_id, plot, id or name) and tells you how many plots lie over the image. Plots in another coordinate system are reprojected to the image. A Shapefile without its `.prj` file has no coordinate system and is assumed to match the image; if none of the plots lie over the image, that is the usual reason. Point and line files are refused.

**Output** (in the export folder of tab 4). A pixel belongs to a plot when the polygon touches it.

- `ppe_plot_canopy.csv`, one row per plot: `crop` and `threshold` (the crop area holding most of the plot, and the threshold used there), `plot_pixels` (image pixels in the plot), `plant_pixels`, `canopy_pct`, `plot_area_m2` and `plant_area_m2` (areas only when the image is in metres). Plots outside the image are listed with 0 pixels. Then the field information from tab 1 (`growth_stage`, `planting_date`, `flight_date`, `irrigation`, `weeds`).
- `ppe_plot_statistics.csv`, one row per plot and variable (every band and vegetation index; for hyperspectral images every original band): the canopy columns above, then for the plant pixels only `pixel_count`, `mean`, `median`, `sd`, `cv_pct`, `min`, `p10`, `q25`, `q75`, `p90` and `max`. Percentiles are calculated as R's default (type 7), so they match the R package.

## Changing or removing the image

- **Browse** when an image is already chosen asks before replacing it. So does switching the source type.
- A new image starts PPE afresh: results, review settings, logs, export and plot settings and the old PPE layers on the map are all cleared.
- **✕** beside the path removes the image from PPE and from the map.
- Deleting the PPE image layer in the QGIS Layers panel resets PPE as well. Deleting only the live-preview layer switches the slider off until PPE is run again.
- While PPE is running the image cannot be changed; cancel the run first.

## How the mask is made (version 3.9.0)

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

   The thresholds are fitted to the whole field. The field is then divided into tiles of about 512 pixels, Otsu is refitted in each tile that holds both soil and plants, and all four thresholds of a layer move up or down together by the difference, blended smoothly between tiles. This keeps pale or small plants in weak plots. The move is limited to the mixed band of multi-Otsu.

   **Half-mixture** (only for scripts calling the engine with `half_mixture=True`; the panel never uses it) adds a fifth threshold per layer: the index value of a pixel that is half typical plant and half typical soil. It does not depend on how much of the field each class covers, so it helps in dense crops, where Otsu and k-means cut too high.
4. **Vote.** Three layers times four methods gives 12 equal votes. The vote raster holds the share that said plant, from 0 to 1.
5. **Guards** (always on in the panel; there are no settings to change).
   - *Dark shadows*: rejects the darkest pixels unless they still reflect enough near-infrared light to be a lit leaf, or their NDVI (and NDRE, when there is a red-edge band) is at plant level, which is a leaf in shade.
   - *Algae in furrows*: algae are green but lack a leaf's rise from red edge to NIR. A pixel whose NDRE is less than a quarter of the way from bare soil to a typical plant is rejected. Needs red-edge and NIR bands; skipped otherwise.
   - *Grey and neutral objects* (camera RGB images with no NIR band): a pixel stays plant only if it is greener than the soil's own colour. Vehicles, tarps, grey residue and black plastic no longer pass a visible index on brightness alone. If plants are barely greener than soil anywhere in the image, the run warns you.
   - *Bright soil and residue*: rejects very bright, colourless pixels with no vegetation signal. Only for scripts (`bright_guard=True`); the panel does not use it.
6. **Warnings.** Thresholds always split an image in two. If even the greenest 1 % of pixels look like soil, or even the least green 1 % look like canopy, or (camera images) plants are barely greener than soil, a yellow bar on the map and a note on the Review tab tell you to check the mask. The mask itself is not changed.
7. **Mask.** A pixel is plant when its vote is at least the threshold minus 0.05.

The log and `run_summary.json` list the spread ranking, the chosen layers and all four thresholds for each layer.

## Band detection

Bands are identified from, in order: colour tags and band names in the file, wavelength metadata, file names, then sensor order. Names from MicaSense (Blue, Green, Red, Red Edge, NIR), Parrot Sequoia (GRE, RED, REG, NIR), DJI multispectral (MS_G, MS_R, MS_RE, MS_NIR) and wavelength names such as `717nm` are recognised. Thermal bands (LWIR, TIR, thermal, temperature) are ignored. The Image tab reports the image type from the bands found, not the band count: a file or folder with only NIR, red edge and red is multispectral. A 3-band file with no names, colour tags or wavelengths asks for its band order: red, green, blue (camera RGB, the default); blue, green, red; NIR, red edge, red; or NIR, red, green (colour-infrared camera). For a hyperspectral cube, give the first wavelength, last wavelength and step; one band is chosen in each of the blue, green, red, red-edge and NIR windows.

## Suggested threshold

After a run the Review slider starts at a threshold suggested for this image, not at a fixed 0.50.

**The idea.** A pixel should count as plant when at least half of it is plant. PPE estimates how much of each sample pixel is plant by mixing two spectra from the image itself: the plant spectrum (pixels where every vote says plant) and the soil spectrum (pixels where no vote says plant). Both are taken per tile, so a pale plot is judged against its own pale plants. Pixels that a plant–soil mix explains poorly (algae, deep shade, objects) are left out. The suggestion is the slider value whose mask matches the "at least half plant" pixels best; the good range is every value within one point of agreement.

**How well it works** (IoU with the reference; higher is better):

| Field | Reference | 0.50 | Suggested | Best possible |
|---|---|---|---|---|
| Tifton, whole field | independent classified plant mask | 87.1 | 91.3 (0.80) | 91.6 (0.70) |
| IH, whole field | unmixing reference | 71.5 | 83.9 (0.40) | 92.0 (0.20) |
| Kritis, whole field | labelled leaves, shade, soil, gaps | 100 | 100 (0.45) | 100 |
| Ten 640-pixel Tifton clips | independent mask | 86.6 (mean) | 88.1 (mean) | 91.5 (mean) |

On whole fields it is clearly better than 0.50. On small clips it is better on average but not on every clip, so always look at the map and move the slider if the mask looks too wide or too narrow; **Back to suggested threshold** returns to it.

It needs a NIR band. On camera RGB images plant and soil differ too little in colour for a reliable plant share per pixel, so those start at 0.50 and the Review tab says so.

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
- The suggested threshold is a starting point, not a guarantee: on small clips it can be off by 0.2 or more. Use the curve and the preview.
- The algae guard cannot work on images without a red-edge band, and at a quarter of the red-edge rise it lets the greenest algae through if the chosen layers also accept it.
- Plants with very low chlorophyll have a weak red-edge rise and could still be rejected by the algae guard. If you see gaps in yellow or stressed canopy, open `biofilm_rejection.tif` in the run folder to see which pixels it removed.
- On camera RGB images green weeds and grass are plants to PPE; it cannot tell crop from weed.
- PPE does not find crop areas by itself: the user draws them. Telling crops apart automatically from one image without training data was not reliable enough.
- An image of bare soil only, or of closed canopy only, is still split in two. PPE warns about it.

## Settings, speed and memory

There are no settings to tune. Thresholds always follow local conditions, the four original methods vote, the shadow and algae guards are always on (the grey-object guard switches itself on for camera RGB images), and the fitting sample is 100,000 pixels; the mask is always full resolution.

PPE sets itself up for the computer it runs on:

- It uses all but one of the CPU's threads (up to 16), leaving one for QGIS. Each band file is decompressed twice per run (once for the fitting sample, once for the votes and the display image together), and both passes run on several threads.
- The number of image blocks processed at once is limited so they use at most a quarter of the computer's memory. A laptop with 8 GB uses up to five workers, a 4 GB machine two; a small machine is slower but does not run out of memory.

On a 2-core test machine a 9371 × 10056 five-band field (Kritis) runs in about 20 s (3.9.0 before this change: 46 s), with exactly the same vote raster, display image and guard rasters. Computers with more cores gain more.

## Support and logs

When something fails, the message says what PPE was doing, the kind of error and where the log is; it is never blank. Each output folder contains `logs/ppe_YYYYMMDD_HHMMSS.log`. Include that file, the QGIS version, sensor and raster size in a bug report.

## License

GPL-2.0-or-later. Copyright 2026 Anish Bhattarai.

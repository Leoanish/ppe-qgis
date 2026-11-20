# Plant Pixel Extractor (PPE) for QGIS

PPE separates plant pixels from soil in drone orthomosaics, with no training data. It works with RGB cameras, multispectral sensors (MicaSense, Parrot Sequoia, DJI and others) and hyperspectral cubes. It runs inside QGIS using only the GDAL and NumPy that ship with QGIS, so there is nothing else to install.

**What you get:** a plant mask you can check on the map before it is written, canopy cover for the whole field, for each crop and for each plot, plant-only bands and vegetation indices, and statistics of the plant pixels in every plot. Two more goals use the same mask: a **plant count** you can correct on the map (early season, per image and per plot) and **training data** for a fast plant/soil model, with no hand labelling.

**How it works, in one paragraph.** PPE ranks the vegetation indices the image allows by how well they spread plant from soil. It splits the three best with four automatic thresholds (Otsu, CDF knee, k-means and multi-Otsu), and the twelve results vote. A pixel's vote is the share of those twelve that call it plant. Guards then remove shadows, algae on wet soil and grey objects. You choose the vote threshold with a live slider; for images with a NIR band PPE suggests one.

The screenshots below are from a real run in QGIS 3.34 on a five-band multispectral image of a corn and soybean field (9371 × 10056 pixels). Red numbers in each picture match the numbered notes under it.

---

## Contents

1. [Install](#1-install)
2. [Open PPE](#2-open-ppe)
3. [Tab 1 Image: choose the image](#3-tab-1-image-choose-the-image)
4. [Tab 2 Run: build the plant vote](#4-tab-2-run-build-the-plant-vote)
5. [Tab 3 Review: choose the threshold](#5-tab-3-review-choose-the-threshold)
6. [Several crops in one image (optional)](#6-several-crops-in-one-image-optional)
7. [What PPE found: tassels, leaf edges, shade, dry leaves, sparse spots](#7-what-ppe-found-tassels-leaf-edges-shade-dry-leaves-sparse-spots)
8. [Finalize the mask](#8-finalize-the-mask)
9. [Tab 4 Export: plant-only rasters](#9-tab-4-export-plant-only-rasters)
10. [Tab 5 Plots: canopy and statistics per plot](#10-tab-5-plots-canopy-and-statistics-per-plot)
11. [Tab 6 Batch: many images at once](#11-tab-6-batch-many-images-at-once)
12. [Goal Plant counter: count plants](#12-goal-plant-counter-count-plants)
13. [Goal Training data: tiles and labels for a model](#13-goal-training-data-tiles-and-labels-for-a-model)
14. [The Layers panel](#14-the-layers-panel)
15. [Files PPE writes](#15-files-ppe-writes)
16. [Tips and troubleshooting](#16-tips-and-troubleshooting)

---

## 1. Install

You need **QGIS 3.28 or newer** (Windows, macOS or Linux).

**From the QGIS plugin repository** (once approved): **Plugins → Manage and Install Plugins → All**, search for **Plant Pixel Extractor**, click **Install Plugin**.

**From a ZIP file** (from the [releases page](https://github.com/Leoanish/ppe-qgis/releases), or built with `./build_plugin.sh`):

![Install from ZIP](docs/images/00_install_from_zip.png)

1. **Plugins → Manage and Install Plugins**, then **Install from ZIP** on the left.
2. Choose `ppe_qgis-4.0.0.zip` with the **…** button.
3. **Install Plugin**.

When you update to a newer version, quit QGIS and open it again afterwards, so the new code is loaded.

## 2. Open PPE

![PPE opened beside the map](docs/images/01_open_ppe.jpg)

1. Click the **leaf button** in the toolbar, or choose **Raster → Plant Pixel Extractor**. The PPE panel opens on the right; you can drag it wider or float it.
2. Choose the **Goal** at the top: *Plant mask* (the default), *Plant counter* or *Training data for a model*. The tabs are used from left to right. Tabs 1 to 3 (Image, Run, Review) are the same for every goal; tab 4 is the goal's own step (Export, Count or Train data), and Batch is last. Finalize leads straight to tab 4. PPE remembers the goal.
3. **Imagery source**: how your image is stored. Choose one of the following:
   - *Stacked GeoTIFF*: one file holding every band;
   - *Folder containing bands*: one file per band in a folder;
   - *Individual band files*: pick each band file.

## 3. Tab 1 Image: choose the image

**A folder of band files.** When you choose a folder, PPE lists its raster files:

![Band files dialog](docs/images/02_band_files.png)

1. **Tick** the files that are bands of this image. Files PPE recognised start ticked. Thermal files, stacked files and files it could not recognise start unticked, and the *Note* column says why.
2. **Band**: the band PPE read from the file name, the band name inside the file or its wavelength. Change it if it is wrong (for example `IMG_0001_4.tif`).
3. **Use the ticked files**. PPE imports exactly those. **Choose band files…** opens the list again later.

**The image appears on the map at once**, before anything is analysed:

![Image on the map](docs/images/03_image_on_map.jpg)

1. The chosen folder (or file).
2. **The bands PPE found**, and the image type it detected (RGB, multispectral or hyperspectral). Check this line: it decides which indices PPE can use.
3. The map shows the image as follows:
   - in **true colour** (red, green and blue share one stretch, so colours look natural) when it has red, green and blue;
   - otherwise in false colour (NIR, red, green), or the NIR band in grey.

**About this field (optional).** Nothing here changes the mask; it is saved with the results so runs can be compared later.

![About this field](docs/images/04_about_this_field.jpg)

1. Fill in what you know:
   - growth stage, planting and flight dates, soil moisture, weeds and notes;
   - at *Flowering / tasselling* or *Drying down* the Review tab adds a threshold tip.

**Folders** (closed by default) sets where results and temporary files go. The defaults work; change them if you want results next to your data.

## 4. Tab 2 Run: build the plant vote

![Run tab](docs/images/05_run_tab.jpg)

1. **Run**. PPE reads the image, ranks the indices, runs the four thresholds on the top three and builds the vote. It works in the background, so QGIS stays usable.

![Running](docs/images/05b_running.jpg)

1. The progress bar names the current step. **Cancel** stops the run. The 9371 × 10056 five-band field took about 20 seconds on a 2-core computer; PPE limits its memory use to a quarter of the RAM.

When the run finishes, PPE opens the Review tab.

## 5. Tab 3 Review: choose the threshold

![Review tab](docs/images/06_review.jpg)

1. **Vote threshold slider.** A pixel is plant when at least this share of the votes call it plant. Drag it and watch the map: lower keeps more (paler plants, edges), higher keeps less. Nothing is written yet.
2. **Canopy cover** at the current threshold, updated as you drag.
3. **Suggested threshold** for this image, with a good range. For images with a NIR band, PPE estimates the threshold whose mask best matches the pixels that are at least half plant, from the image's own pure-plant and pure-soil spectra. The slider starts there; **Back to suggested threshold** returns to it.
4. **Live preview** of the plant mask in magenta, drawn over the image.

**View threshold curve** shows canopy cover at every threshold, with the suggestion and its good range marked. Click on the curve to move the slider there.

![Threshold curve](docs/images/07_threshold_curve.png)

A flat stretch of the curve means the mask hardly changes there: any threshold in it gives the same canopy. A steep part means the threshold matters, so check the map carefully there.

**"Plants and ground do not separate clearly."** PPE measures how clearly its top index splits the image into two groups (0 to 1). Under 0.65, the run summary, the message bar and the *What PPE found* card warn you to check the mask closely. This happens when plants are very few or tiny (seedlings) or with RGB-only images. In tests it warned on an RGB-only field and on seedling images, never on four multispectral fields or late-stage images. It is only a message: nothing in the mask changes.

## 6. Several crops in one image (optional)

Different crops, or the same crop at a different stage, can need different thresholds. Under **Crops in this image** on the Review tab:

![Drawing a crop area](docs/images/08_draw_crop.jpg)

1. **+ Add crop**, type its name, then **Draw area**. While you draw, the button turns grey and reads **Drawing…**, and the other crops wait.
2. Click the corners of a **rough outline** around that crop on the map. Right-click (or double-click) to finish; Esc cancels; Backspace removes the last corner.

![Crop with its own slider](docs/images/09_crop_slider.jpg)

1. Each drawn crop gets **its own slider and canopy**; the main slider sets the rest of the image (headlands, roads). *Use the slider above for every crop* moves them together.
2. The outline is shown as **PPE · Crop areas**. **Redraw** replaces it; **✕** removes the crop.

Only the threshold differs between areas; the vote itself is the same, so a crop slider at the main slider's value gives exactly the usual mask.

## 7. What PPE found: tassels, leaf edges, shade, dry leaves, sparse spots

After each run PPE sorts out six kinds of pixel worth a look: five you can leave out of the mask, and one you can add to it. Nothing is tied to a particular field or crop: every reference (the green leaves' NDVI, colour, brightness and near-infrared light, and the bare soil's) is measured on the image itself, from the pixels nearly all the voters agree on. Each check runs whenever the image has the bands it needs, and the card says which could not run and why.

| Kind | What it is |
|---|---|
| ✿ **Plant parts that are not green** | as much near-infrared light as plant, but not leaf-green, inside closed canopy: tassels, flowers, heads, pods, yellowing leaves |
| ◌ **Leaf edges over soil** | the same colour, but next to soil: a little soil in the pixel makes a leaf edge look pale |
| ● **Shaded leaves** | darker than half a typical leaf, but more near-infrared light than sunlit soil can reflect: leaves under the top of the canopy |
| ? **Unclear** | as dark, but no more near-infrared light than soil: could be deep shade on a leaf or on soil |
| ❧ **Dry or yellowing plant parts** | inside the canopy (no bare soil close by), red about as bright as green and a lower NDVI than green leaves, but more near-infrared light than sunlit soil: leaves drying or turning yellow-brown. Only pixels that can be in a mask are marked |
| ✚ **Faint plants in sparse spots** | where the canopy is thin (under 25 % plant in 3 m squares at the suggested threshold), the pixels a threshold 0.20 lower would add there: small, pale plants that get fewer votes. These are **added** to the mask when you tick them |

![What PPE found](docs/images/10_what_ppe_found.jpg)

1. Open the closed card **What PPE found: n things to look at**.
2. Each kind present shows its share of the plant and one plain sentence. The coloured square is its colour on the map.
3. **Show them on the map** adds a group, *PPE · What PPE found*, with **one layer per kind**:
   - *Plant parts that are not green (amber)*;
   - *Leaf edges over soil (light blue)*;
   - *Shaded leaves (blue)*;
   - *Unclear (red)*;
   - *Dry or yellowing plant parts (brown)*;
   - *Faint plants in sparse spots (light green)*.

   Tick or untick each kind to see it alone or compare it with the others. All the layers read the same file, so this costs no disk space.
4. Here, the tassels lie along the corn rows.

**Leave a kind out, or put it back, with one click:**

![Leaving out unclear pixels](docs/images/11_leave_out.jpg)

1. Tick **Leave out of the mask**.
2. The card says what is left out: the kinds, their share of the plant and, without crop areas, the exact number of pixels.
3. The **live preview** drops those pixels at once; they show as holes. It is still the same live layer, and they stay out whatever you do with the slider (or the crop sliders), so you tune the threshold with them already left out.

**Finalize mask** writes the mask without them, then reports the pixels it left out (counted in the mask file) and the canopy before and after. Untick, or **Put all back**, to return them, then finalize again. The mask is always rebuilt from the vote, so nothing is lost either way. Each choice is written to its own file (for example `ppe_plant_mask_0.45_without_unclear.tif`), and the plot export and run record say what was left out.

Which checks run depends only on the bands: plant parts, leaf edges and dry parts need NIR, red and green; shaded leaves and unclear pixels need a brightness (red, green and blue, or NIR alone) and are told apart by NDVI; a camera RGB image gets only unclear (dark) pixels. When the near-infrared band barely separates leaves from soil (an uncalibrated band), the checks that compare near-infrared brightness are not run and NDVI decides between shaded and unclear. Faint plants in sparse spots need only the vote; when more than 30 % of the 3 m squares are thin, the whole field is thin and PPE says to move the slider instead. The card lists anything not checked.

**Add faint plants in sparse spots:**

![Adding faint plants in sparse spots on Tifton](docs/images/12_sparse_spots.jpg)

1. If the field has thin patches, the card lists **Faint plants in sparse spots** and how much more plant they would add.
2. Tick **Add to the mask**. The live preview shows them at once, and the canopy on the panel goes up; they stay in whatever the slider.
3. **Finalize mask** writes them into the mask (for example `ppe_plant_mask_0.80_with_sparse.tif`) and reports the pixels added.

On Tifton, compared with a reference mask over 354 cells of 3 m × 3 m, adding them at the suggested threshold raised the pixel match (IoU) from 91.3 to 91.9 %, cut the canopy error per cell from 3.4 to 2.9 points, and raised the agreement of the plant NDVI mean from R² 0.83 to 0.92 and of the median from 0.86 to 0.93. In the thin patches alone the match rose from 78 to 87 %. Bringing back faint pixels along the crop rows was also tried and did less, so rows are not used. It is not available with crop areas: lower that crop's slider instead.

## 8. Finalize the mask

![Finalize](docs/images/12_finalize.jpg)

1. **Finalize mask** writes the plant mask at full resolution (1 = plant, 0 = not plant).
2. The status gives the canopy of the image and of each crop, and the mask file's path.
3. *PPE · Applied plant mask* is added to the map.

Only one magenta layer is shown at a time. After Finalize the map shows the finalized mask. As soon as you move a slider or tick a kind, the live preview comes back, and the finalized mask is switched off (it stays in the Layers panel). The Export tab then tells you that its exports still use the finalized mask, until you finalize again. You can go back, change things and finalize again at any time; the vote is kept.

## 9. Tab 4 Export: plant-only rasters

![Export tab](docs/images/13_export.jpg)

1. **Export folder**.
2. Choose what to write:
   - **Export plant-only bands**: every band with soil set to NoData;
   - **Export plant-only vegetation indices**: every index the image allows;
   - **Export bands and all indices**: both.

Each raster is a GeoTIFF on the original pixel grid. Exporting all indices of a large image can take time and disk space; PPE asks first.

## 10. Tab 5 Plots: canopy and statistics per plot

![Plots tab](docs/images/14_plots.jpg)

1. **Polygon file** with your plots. PPE accepts:
   - a Shapefile (choose the `.shp` or any of its side files) or a zipped Shapefile;
   - GeoPackage, GeoJSON, KML/KMZ, GML, FlatGeobuf or SpatiaLite.
   Plots in another coordinate system are reprojected to the image.
2. **Plot identifier**: the field that names each plot (PPE suggests `plot_id`, `plot`, `id` or `name`).
3. PPE says **how many plots lie over the image**. Check this before exporting: no plots over the image usually means a missing `.prj` file.
4. **Export plot statistics**.
5. The plots are shown as **PPE · Plots**, yellow outlines labelled with their IDs. Untick *Show plot IDs on the map* to hide the labels.

The export writes two CSV files into the export folder.

**`ppe_plot_canopy.csv`** has one row per plot. Its columns:
- `crop` and `threshold` used there;
- `plot_pixels`, `plant_pixels` and `canopy_pct`;
- `plot_area_m2` and `plant_area_m2`;
- the What PPE found shares (`plant_parts_pct`, `leaf_edges_pct`, `shaded_leaves_pct`, `unclear_pct`, `dry_parts_pct`, `sparse_faint_pct`), `left_out_of_mask` and `added_to_mask`;
- the field information from tab 1.

```
plot_id,crop,threshold,plot_pixels,plant_pixels,canopy_pct,plot_area_m2,plant_area_m2,plant_parts_pct,...
402-3,,0.45,296744,296670,99.98,62.91,62.89,0.01,...
```

**`ppe_plot_statistics.csv`** has one row per plot and variable (every band and index). It holds the canopy columns, then, for the plant pixels only: `pixel_count`, `mean`, `median`, `sd`, `cv_pct`, `min`, `p10`, `q25`, `q75`, `p90` and `max`. Percentiles follow R's default (type 7), so they match the `ppe` R package.

## 11. Tab 6 Batch: many images at once

![Batch tab](docs/images/13_batch.png)

For many images (dates, fields or flights), PPE can run a whole folder and let you check each image with the same slider.

1. **Choose the folder.** Every raster file in it (`.tif`, `.tiff`, `.vrt`, `.img`, `.jp2`) is one image, and every sub-folder holding band files is one image. The tab says how many it found. The output and working folders are the ones set on tab 1; each image gets its own sub-folder.
2. **Run all.** The images run one after another, each exactly as on its own: same vote, suggested threshold, What PPE found, warnings and confident labels. An image that fails is listed as failed and the rest go on. **Cancel** stops after the current image; the finished ones are kept.
3. **The list.** One row per image: threshold, plant cover and checks. Images to check first (for example "plants and ground do not separate clearly") are on top in red.
4. **Review selected** (or double-click a row) opens the image on the map and shows its review right below the list: threshold, canopy cover, the slider, **Suggested**, **✓ Finalize this mask** and **Next image**. No need to leave the tab. **More on tab 3** opens What PPE found and crop tools for the same image (a banner there says which image it is). Your threshold and any kinds left out are kept for that image. In the list, ⚠ marks images to check, ✓ reviewed or ✓ mask written; hover a row for the details.
5. **Same settings for all** gives every image the threshold (and kinds left out) of the image open on tab 3.
6. **Finalize all masks** writes every mask at its own threshold into the image's folder, plus `ppe_batch_summary.csv`: image, threshold, whether you reviewed it, plant cover, what was left out, checks, mask and labels paths. Images you did not open are written at their suggested (or starting) threshold.

**Open a previous batch…** reopens a batch later from its `ppe_batch.json`, with the settings you chose (kept in `ppe_batch_choices.json`).

## 12. Goal Plant counter: count plants

![The three goals: Plant counter (Count tab after a run), Training data](docs/images/16_goals.png)

Choose **Plant counter** at the top. Run and review as usual, finalize the mask, and tab 4 **Count** counts the plants.

- **One plant** gets one point. PPE first drops specks smaller than the **smallest plant** (in cm²; set it a little below your smallest seedling). Then pieces closer than **Join leaves closer than** (1 cm by default) become one plant: the leaves of one plant often show as separate pieces because the stem or petiole between them is thin. Keep this setting smaller than the gap between neighbouring plants.
- A plant also needs a **sure core**: at least 5 pixels where every PPE vote says plant. This keeps out soil spots that a low threshold lets in.
- **Touching plants** become one. Groups much bigger than the larger plants of the image (over 2.5 × the 75th percentile) are drawn orange and flagged **check**; they still count as one. Splitting them by size made counts much worse in tests. Count early in the season, before plants touch.
- **Crop row spacing** (optional): with it, plants between the rows are marked as possible weeds (grey with a red ring) and not counted. For straight, parallel rows. On PhenoBench it halved the weeds counted and marked a few crop plants as weeds, which you can switch back (below). It is off unless you set it.
- **Plot file** (optional): plants and plants per m² for each plot.
- **Count all batch masks** counts every mask written on the Batch tab.

How close is it? On 37 PhenoBench test images (sugar beet with weeds, every plant boxed by hand; plants smaller than 1 cm² left out), with masks at threshold 0.65, the share of plants that got exactly one point was 88 % at the seedling date, 57 % at the middle date and 50 % at the late date. Plants split into several points: 2, 6 and 5 %. Before leaves were joined (4.3.0): 72, 51 and 48 % right, with 21, 19 and 12 % split. The plants still missed later in the season are mostly plants that touch, and small weeds PPE's mask does not catch.

The points are in the layer **PPE · Plants**, kept at the top of the Layers panel: yellow = plant, orange = plant to check, blue = added by you, grey with a red ring = possible weed (not counted).

**Correct the count on the map.** PPE's points are a fast first count; it cannot tell every weed from the crop or separate plants that overlap, so check them and fix what is wrong:

![Correcting the count: added plant (blue), possible weeds between rows (grey)](docs/images/17_count_corrections.png)

- **＋ Add plant**: click where a plant was missed.
- **－ Remove point**: click a point that is not a plant (soil, a weed, a plant counted twice).
- **Plant ↔ weed**: click a point to switch it between plant (counted) and weed (not counted).
- **Undo last** (or Backspace), and **Done** (or Esc) to stop.

Each click is saved in `ppe_plants.gpkg` at once, and the count, plants per m², `ppe_count_summary.csv` and the counts per plot are updated.

## 13. Goal Training data: tiles and labels for a model

Choose **Training data for a model** at the top. After a run (no need to finalize: the labels do not depend on the slider), tab 4 **Train data** cuts the image and PPE's labels into tiles:

- **plant**: every PPE vote says plant (not *unclear*; groups of at least 30 pixels);
- **soil**: no PPE vote (at most a tenth) says plant, or *unclear* (too dark to tell);
- **unsure**: the rest; models skip these pixels.

Image tiles keep every band with its original values and data type. Train and validation tiles come from different images (from different parts of the image when there is only one). **From all batch images** uses every image of the batch.

Training happens outside QGIS with `ppe_train.py` (in the plugin's `tools` folder; **Open the tools folder** shows it). It needs Python with PyTorch (`pip install torch`) and GDAL, and uses an NVIDIA GPU, an Apple chip or the CPU:

```
python ppe_train.py train   ppe_training_data                 # about 2 minutes on a laptop CPU
python ppe_train.py predict ppe_model.pt new_flight.tif       # writes new_flight_model_plant_mask.tif
python ppe_train.py check   ppe_model.pt new_flight.tif <PPE run folder of new_flight>
python ppe_train.py update  ppe_model.pt <new training data> <old training data>
```

**Keeping the model up to date.** Fields change through the season, so a model trained early drifts later. For each new flight, run PPE and then `check`: it compares the model with PPE's sure pixels, against how well it agreed on its own validation tiles. If it misses plants PPE is sure of, or calls PPE's sure soil plant, update it with the new images **plus** old ones. Updating with new images only makes it forget the old ones. In a test on PhenoBench, a model trained on seedlings and updated with late images alone fell from 87 to 77 % overlap on seedlings; updated with new and old images it kept 85 % and reached 96 % on the late images.

On PhenoBench (143 training images, 37 unseen test images), the model trained in 2 minutes on PPE's labels alone reached 93.6 % overlap with hand labels on the test images; the same model trained on the hand labels reached 95.4 %.

## 14. The Layers panel

![Layers panel](docs/images/15_layers_panel.jpg)

1. PPE's results are layers of their own, kept in this order from the top:
   - **PPE · Plots**;
   - the **PPE · What PPE found** group, with one layer per kind (when shown);
   - **PPE · Applied plant mask**;
   - **PPE · Image**.
2. The **Plant Pixel Extractor** group holds only the working layers: *Crop areas* and *Live threshold preview*.

PPE's layers come in **folded**, so the panel stays short; the arrow beside a layer shows its legend. Your own layers keep their places. Removing the PPE image layer resets PPE; removing the live preview switches the slider off until the next run. Every layer can be restyled like any QGIS layer.

## 15. Files PPE writes

The output and working folders are set under **Folders** on tab 1; plot results go to the export folder of tab 4.

| File | What it is |
|---|---|
| `ppe_plant_mask_<threshold>.tif` | the finalized mask (1 plant, 0 not plant, 255 no data); `_by_crop` with crop areas, `_without_<kinds>` when kinds were left out |
| `ppe_run_record.json` | what was done, so the result can be repeated: image, chosen indices, suggestion, thresholds, canopy per crop, what was left out, field information |
| `ppe_plot_canopy.csv`, `ppe_plot_statistics.csv` | plot results (tab 5) |
| plant-only band and index GeoTIFFs | tab 4 |
| run folder `ppe_YYYYMMDD_HHMMSS` (in the working folder) | the vote raster, `ppe_findings.tif` (What PPE found), the true-colour display image and guard rasters (which pixels each guard removed) |
| `ppe_batch.json`, `ppe_batch_choices.json`, `ppe_batch_summary.csv` | a batch (tab 6): its list of images, the settings you chose, and the table written by Finalize all masks |
| `ppe_confident_labels.tif` (in the run folder) | PPE's sure pixels as labels: 1 plant (every vote agrees), 0 soil (at most a tenth of the votes say plant), 255 not labelled. Use it to train your own model or to check PPE. On a public sugar beet dataset, a small model trained on these labels alone reached 94.7 % overlap with hand labels on unseen images, against 95.4 % when trained on the hand labels themselves. |
| `ppe_count/ppe_plants.gpkg`, `ppe_count_summary.csv`, `ppe_plot_counts.csv`, `ppe_batch_counts.csv` | plant count (Count tab): one point per plant, totals, counts per plot, counts per batch image |
| `ppe_training_data/` (`images/`, `labels/`, `manifest.json`, `README.txt`) | training data (Train data tab) for `ppe_train.py` |
| `logs/ppe_YYYYMMDD_HHMMSS.log` (in the output folder) | the log of each run: attach it when you report a problem |

## 16. Tips and troubleshooting

- **The mask misses pale plants or flowers.** Lower the threshold a little, or draw that crop as its own area with a lower threshold. At tasselling, a corn field kept its tassels near 0.30 without letting soil in.
- **Shaded leaves or dark gaps.** Open *What PPE found*. Shaded leaves are plant; unclear pixels can be left out with one tick and put back. On RGB-only images (no NIR) PPE tells them apart by colour: dark pixels whose strongest colour is still green are shaded leaves, the rest (mostly soil in shadow) are unclear.
- **Wrong bands.** Check tab 1's *bands found* line, and for folders, the Band column of the band-files dialog.
- **No plots over the image.** The plot file has no coordinate system (a Shapefile without `.prj`) or is in the wrong place.
- **Errors** always name the step, the kind of error and the log file. Report problems at <https://github.com/Leoanish/ppe-qgis/issues> with the log, the QGIS version, the sensor and the raster size.

## For developers

- Plugin source and the full reference guide: [`ppe_qgis/README.md`](ppe_qgis/README.md)
- Version history: `changelog` in [`ppe_qgis/metadata.txt`](ppe_qgis/metadata.txt)
- Unit tests: `tests/` (run from the repository root with `python -m unittest discover -s tests`; needs GDAL and NumPy)
- Build the ZIP for QGIS: `./build_plugin.sh`

## License

GNU General Public License, version 2 or later. See [`LICENSE`](LICENSE).

Author: Anish Bhattarai, University of Georgia.

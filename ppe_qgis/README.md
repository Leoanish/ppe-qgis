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

## What PPE found: five kinds to leave out, one to add

Every run sorts out six kinds of pixel worth pointing out (five to leave out, one to add). Nothing is tied to a field or a crop: the references are measured on the image itself, from the sample pixels nearly all voters agree on (vote 0.9 or more: green plant; 0.1 or less: ground): the green leaves' NDVI, green/red, brightness and NIR (sunlit half), and bare soil's NIR (NDVI under 0.25, else the barer half of the ground; all of it for the pale-plant floor, its sunlit half for shaded leaves). Leaf-side NDVI cut-offs (green leaf 0.8, plant 0.6, canopy 0.7, pale 0.5–0.82, shaded 0.5) are the values first tuned on Kritis, scaled by this image's green-leaf NDVI over Kritis's 0.88 (0.6–1.1 times); colour cut-offs are shares of this image's green-leaf green/red (pale under 0.66 times, dry under 0.49 times; on Kritis 1.5 and 1.1). Bare-soil NDVI levels (0.25, dark 0.3) are physical and stay.

| Kind | How PPE tells | What it means |
|---|---|---|
| ✿ Plant parts that are not green | as much near-infrared light as plant, a plant-level NDVI but not a leaf's green, inside closed canopy (90 % plant within 0.5 m, no soil within 15 cm) | tassels, flowers, heads, pods, yellowing leaves (named from the crop and stage you give) |
| ◌ Leaf edges over soil | the same colour, but beside soil or in open canopy | a little soil in the pixel makes a leaf edge look pale (what an earlier pale-plant note mistook for flowers in young corn) |
| ● Shaded leaves | less than half as bright as a typical leaf, but more near-infrared light than bare soil in full sun can reflect | leaves under the top of the canopy: plant |
| ? Unclear | as dark, but no more near-infrared light than soil in sun | could be a leaf in deep shade or soil in shade |
| ✚ Faint plants in sparse spots | in 3 m squares where PPE's canopy at the suggested threshold is under 25 %, pixels with a vote from the suggested threshold minus 0.25 up to the mask (a threshold 0.20 lower), and at least 0.10; marked after the run, also when the other kinds are not judged | small, pale plants in thin patches: **Add to the mask** puts them in, at every slider value |
| ❧ Dry or yellowing plant parts | NDVI between the dark level and the pale band, green/red below 1.1 (red about as bright as green), more near-infrared light than bare soil in full sun, no bare soil within 15 cm, and a vote of at least 0.10 (the lowest mask the slider can make) | leaves drying or turning yellow-brown inside the canopy: plant. Leave them out for a green-only mask. The same material lying on bare soil, and leaf-soil mixes at row edges, are not marked |

On tab **3 Review** a closed card, *What PPE found: n things to look at*, opens to one short note per kind present (from 0.2 % of the plant). Each has **− Leave out** and **+ Add** (4.6.5; faint plants only + Add): `clip_codes` / `add_codes` may hold any kind, leaving out wins when both. − takes those pixels out of the live preview at once, the same live layer, and they stay out whatever the slider (or the crop sliders) says. The card gives the share of the plant and, without crop areas, the exact number of pixels. **Finalize mask** writes the mask without them (to a file of its own, such as `ppe_plant_mask_0.45_without_shaded.tif`) and reports the pixels it left out, counted in the file, with the canopy before and after. Untick, or **Put all back**, to return them, then finalize again. The mask is always rebuilt from the vote, so leaving out and putting back loses nothing. The live preview reads a small VRT next to the vote: the vote times 0 where a left-out kind is and times 1 elsewhere; for added kinds a second VRT (pixel function sum) adds 2 there, so they show at every threshold. Nothing is copied. Only one magenta layer is shown at a time: after Finalize the finalized mask; once a slider moves or a kind is ticked, the live preview (the finalized mask is switched off, and the Export tab says its exports still use it). **Show them on the map** adds a group, *PPE · What PPE found*, with one layer per kind in its own colour (plant parts amber, leaf edges light blue, shaded leaves blue, unclear red, dry or yellowing parts brown; the coloured square on the card is the same colour). Tick or untick each kind on its own; all the layers read the same file.

- The plot export gives each kind's share of every plot (`plant_parts_pct`, `leaf_edges_pct`, `shaded_leaves_pct`, `unclear_pct`, `dry_parts_pct`, `sparse_faint_pct`, percent of the plot whatever the threshold), `left_out_of_mask` and `added_to_mask`; the run record keeps the shares and what was left out.
- Early in the season (*Early / vegetative* under *About this field*) pale spots are counted as leaf edges.
- Each check runs when the image has its bands; the card and the log list the ones that could not run and why. Plant parts, leaf edges and dry parts need NIR, red and green and a usable NIR band (sunlit green leaves at least 1.3 times the NIR of sunlit bare soil); shaded/unclear need a brightness (RGB, else NIR) and use NDVI, plus the NIR test when NIR is usable and brightness is not NIR itself; without NIR and red, dark pixels with some plant votes are listed as unclear. Sparse spots need only the vote and are not marked when more than 30 % of the squares are thin.
- Tested (percent of the plant at 0.45, 4.0): Kritis plant parts 1.6, leaf edges 1.8, shaded leaves 2.7, unclear 1.6, dry 0; IH corn at V6 plant parts 0.04, leaf edges 4.3 (8.1 in 3.9: its green leaves have green/red 1.92 against Kritis 2.26, so pale now means under 1.27), shaded 0.01; Tifton: NIR not usable (sunlit leaves 0.061, sunlit soil 0.055), so shaded/unclear only (shaded 0.13 at 0.80) and sparse spots; Midville (no green or blue band): shaded leaves 2.5 at 0.75, brightness from NIR; a camera RGB field: unclear 0.04, sparse spots not marked (78 % of squares thin). Synthetic fields: leaves at NDVI 0.66 with a usable NIR band get their shaded leaves found (99 %); drying leaves inside the canopy are marked (90 %), the same material on bare soil, green leaves and row edges are not. The vote is the same as 3.9.0 bit for bit on all four fields.
- Faint plants in sparse spots, tested on Tifton against its reference mask (354 cells of 3 m): added at the suggested threshold 0.80, IoU 91.3 to 91.9, canopy RMSE per cell 3.35 to 2.85 points (bias -1.9 to -1.3), plant NDVI mean R² 0.83 to 0.92 and median 0.86 to 0.93; in the thin squares alone IoU 78 to 87. The rule (25 %, 0.20, 3 m) was chosen on half of the squares and checked on the other half. Bringing back faint pixels along the detected crop rows instead was tried: it found the rows (0.93 m spacing) but the pixels it added were mostly soil between plants, so rows are not used. Marked: Tifton 0.49 % of the image, Midville 0.52 %, Kritis 0.09 %, IH 0. Not offered with crop areas (their preview holds vote + 10 x crop number), where each crop has its own slider.
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

## 4.7.0: touching plants split by the plant spacing

With crop rows found, the plant counter measures the plant spacing along the rows from confident plants only (`confident_range`: median area x/÷ exp(2 sigma) of the image's single plants, sigma from the MAD of log areas, within x0.4-x2.5; gaps kept only between two neighbouring confident plants, `spacing_gaps`; gaps over a missing plant divided by the number of steps; at least 20 gaps) or takes the spacing the user types, and splits every *check* patch into round(length along the row / spacing) plants, status *auto split* (green, orange ring), field `split_of` = the original point (`plant_count.auto_split`). A patch shorter than 1.5 spacings stays *check*. On PhenoBench (61 check patches, spacing pooled per date) the spacing from all plants was 19.2 / 17.7 / 16.3 cm and from confident plants 20.7 / 19.5 / 20.6 cm (true 22.0 / 20.2 / 21.2); wrong plants in check patches 52 (not split) / 27 (all plants) / 22 (confident); crop plants with exactly one point over all 541 plants 56.4 / 61.2 / 61.6 %; from a single 1 m image the spacing was too rough to help, hence the 20-gap minimum. *⊕ Merge* makes a split patch one plant again (or two clicked points one plant); *✂ Split* on a split point splits only its part (`split_part`). The panel states the spacing, the split length (1.5 × spacing) and each split patch's length; split points are labelled 'part/total' (`SPLIT_LABEL_EXPRESSION`), check points '#id'. Showing the plant points unticks the other PPE layers (`show_only_image_and_points`). PPE opens on the Plant mask goal. The summary has `of_which_split_by_ppe`, `plant_spacing_cm` and `split_longer_than_cm`.

## 4.8.0: weeds inside the rows (4.7.0 was not published; 4.8.0 contains all of 4.7)

`plant_count.flag_row_weeds` (after `auto_split`; cfg `row_weeds`, default on): a 'plant' point closer than `ROW_WEED_CLOSE` (0.5) x the plant spacing to its nearest neighbour in the same row (|across| < 0.5 row spacing), with an area below the image's median plant and below that neighbour's, becomes 'weed?' with field `weed_reason` (between-row weeds get 'between the rows'). PhenoBench, real plugin code, spacing pooled per date (`/home/claude/spacing/exp7.py`): points on weeds 232 -> 176, per-image count error 258 -> 200, crops with exactly one point 61.6 -> 62.7 %, crops with 2+ points 21 -> 10, crops with no point 187 -> 192. Without the 'smaller than the neighbour' condition the count error fell further (177) but exactly-one dropped to 60.8 %. Size alone, RGB colour distance from the confident plants and elongation each cost more crops than they removed weeds. Summary: `weeds_in_row`, CSV `of_which_weeds_in_row`.

## 4.5.1: crop rows found automatically

No row spacing to type: `plant_count._rows` finds one direction and one spacing (15 to 150 cm) from up to 2 million plant pixels of the mask (coarse grid; the search uses 20 000), folding them by each candidate spacing in every 4 m tile and summing the tiles' strengths, so plots shifted against each other still fit. Rows count as found when their strength is at least 1.8 times the median over all directions and spacings; each tile then needs half the field's strength. Thin rows also line up at half their spacing, so the widest spacing that fits at least 80 percent as well is taken. Found: Tifton 91 cm, Midville 91 cm, IH 76 cm (30-inch corn); Kritis (closed canopy): no rows, nothing marked. The Count tab has one checkbox, *Find crop rows*, on by default.

## 4.5.0: plant count you can correct on the map

The plant counter (removed in 4.4.0) is back as a fast first count that the user checks and corrects. A count from a plant mask alone cannot tell every weed from the crop or split overlapping plants (PhenoBench crop count 24 to 49 percent off from PPE's mask with rows; 39 to 57 percent off even from a perfect crop-only hand mask), so the Count tab now has *Add plant*, *Remove point*, *Plant ↔ weed*, *Undo last* and *Done*. Each click is written to `ppe_plants.gpkg` and the totals (`ppe_count_summary.csv`, plants per m², `ppe_plot_counts.csv`) are recomputed (`plant_count.summarize_points`). Points have a `status`: plant, check, added, weed?. With a crop row spacing, plants between rows are kept as *weed?* (grey, red ring) and not counted, instead of being dropped, so they can be switched back. `ppe_count_info.json` keeps the field area and settings for the recount.

## 4.3.1: plant counter counts plants, not leaves

- Leaves of one plant that show as separate pieces (thin stem or petiole not in the mask) are joined: pieces closer than 1 cm (setting *Join leaves closer than*) are one plant. Specks smaller than the smallest plant are dropped first so they cannot chain plants together. PhenoBench test images, plants counted exactly once: seedlings 72 to 88 %, middle 51 to 57 %, late 48 to 50 %; split plants 21 to 2 %, 19 to 6 %, 12 to 5 %. Each plant's point is the centre of its plant pixels.
- *check* flag: over 2.5 × the 75th-percentile plant (was 3 × the median, which flagged single crop plants among small weeds).
- The layer *PPE · Plants* is always at the top of the Layers panel; yellow dots 3.8 mm with a black outline, orange for *check*.

## 4.3.0: goals, plant counter, training data, faster

- **Goal** at the top of the panel: *Plant mask*, *Plant counter*, *Training data for a model* or *Create labels*. Tabs 1 to 3 are the same for every goal; tab 4 is the goal's own step; Batch is last. Finalize leads to tab 4.
- **Goal switch hides other goals' layers** (4.6.2, `switch_goal_layers`, `GOAL_ROLES`): per-goal visibility is remembered on each layer tree node; nothing is deleted; non-PPE layers are untouched.
- **Labels tab** (Create labels, 4.6; tabs: Image, Labels): one click runs PPE, finalizes at the suggested threshold (`run_and_create`) and creates plant/soil label points, clear pixels only (plant: in the mask, vote >= 0.9, no PPE finding, 1 px from the edge; soil: outside the mask, vote <= 0.1, no finding, 1 px from the edge). Points chosen by conditioned Latin hypercube sampling (bands, NDVI, x, y; 20,000 candidates per class) inside an optional drawn area or polygon file. Check tools: Plant <-> soil, Remove, + Plant, + Soil, Undo. Buffer slider 0-50 cm (0.5 cm steps): disc cut to the clear pixels of the point's class; switched/added points keep their own pixel. Writes `ppe_labels.gpkg`, `ppe_label_buffers.gpkg`, `ppe_label_raster.tif` (1/0/255), `ppe_label_pixels.csv`, `ppe_labels_area.gpkg` (label_points.py).
- **Count area** (4.6.4): drawn polygons or a polygon file; `_mask_in_area` writes `ppe_count/ppe_mask_in_area.tif` (outside = 255) and the count runs on it, so plants, rows and plants per m² (area of the polygons) come only from inside. Area UI shared with Create labels (`_area_widget`, `show_area_layer`, roles `count_area` / `label_area`).
- **Count tab** (Plant counter): one plant = one separate group of plant pixels, at least the smallest plant size (cm²), with a sure core (at least 5 pixels where every vote says plant). Groups over 3 × the median plant are flagged *check* and count as one. Optional crop row spacing leaves out plants between rows; optional plot file gives counts and plants per m² per plot; batch masks can be counted at once. Writes `ppe_count/ppe_plants.gpkg` (shown as *PPE · Plants*), `ppe_count_summary.csv`, `ppe_plot_counts.csv`, `ppe_batch_counts.csv`. Uses GDAL only (Polygonize, Sieve, Rasterize).
- **Train data tab** (Training data): image tiles (every band, original values and data type) and PPE labels (1 plant: every vote, not unclear, groups of 30 pixels or more; 0 soil: vote at most 0.1 or unclear; 255 unsure), train and validation split by image, `manifest.json`, `README.txt`.
- **`tools/ppe_train.py`** (outside QGIS, PyTorch): `train`, `predict`, `check` (drift watchdog against PPE's sure pixels, relative to the model's own validation agreement) and `update` (new plus old images). CUDA, Apple MPS or CPU.
- **Faster:** bands stored in one file are read in one call, and GDAL keeps up to 1 GB (at most a quarter of the memory) of decoded blocks during a run, restored afterwards. A 5-band 5480 × 6559 field: 31 s to 13 s; IH 31 to 15 s. Vote, findings, labels and display image are identical (checked on five fields).

## 4.2.1

- **Batch review on the Batch tab:** slider, Finalize this mask and Next image appear right below the list.
- **RGB-only images:** dark pixels whose strongest colour is green count as shaded leaves; the rest stay unclear. On PhenoBench, leaving out unclear now raises the overlap at 0.70 from 84.5 to 87.3.
- **Fixed:** pure-black pixels in camera photos showed as white holes on the map; they are now drawn black.

## Batch: many images at once (4.2)

Tab 6 runs every image of a folder (raster files, or sub-folders with band files), each exactly as on its own. The list shows each image's threshold, plant cover and checks, with images to check first on top. Open any image to review it on tab 3 with the normal slider and What PPE found; your setting is kept per image (Next image moves on). Finalize all masks writes every mask at its own threshold, plus `ppe_batch_summary.csv`. A batch can be reopened later from its `ppe_batch.json`.

## Weak split warning and confident labels (4.1)

**Weak split warning.** PPE measures how clearly its top index splits the image into two groups (Otsu separability, 0 to 1). Under 0.65 the run summary, the message bar and the What PPE found card say that plants and ground do not separate clearly and that the mask needs a closer look. It warned on an RGB-only field and on seedling images, never on four multispectral fields or late-stage images. Message only.

**Confident labels.** Every run writes `ppe_confident_labels.tif` in the run folder: 1 plant where every vote agrees, 0 soil where at most a tenth of the votes say plant, 255 not labelled. It is meant for training a model or checking PPE. On PhenoBench (sugar beet, RGB), a small U-Net trained on these labels alone reached 94.7 IoU on unseen images, against 95.4 with hand labels.

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

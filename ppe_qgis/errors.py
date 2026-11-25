"""Plain-language messages for the errors users meet most (4.12.0). The technical first line is kept below
the plain text, so a report still says what happened."""

_KNOWN = (
    (
        ("memoryerror", "cannot allocate", "unable to allocate", "out of memory"),
        "Your computer ran out of memory for this image. Close other programs and run again. Very large mosaics "
        "can also be clipped into parts (Raster > Extraction > Clip) and run one by one, or together with Batch.",
    ),
    (
        ("no space left", "disk full", "errno 28"),
        "The disk is full. Free some space, or choose an output folder and working cache on another disk "
        "(Image > Folders), and run again.",
    ),
    (
        ("permission denied", "read-only file system", "errno 13", "access is denied"),
        "PPE may not write to the output folder. Choose a folder you can write to (Image > Folders), for example "
        "in Documents, and run again.",
    ),
    (
        (
            "not recognized as a supported file format",
            "not a supported file",
            "could not open",
            "cannot open",
            "no such file",
            "does not exist",
            "errno 2",
        ),
        "PPE could not open the image or one of its files. Check that the file still exists, is fully "
        "downloaded (OneDrive or Dropbox may keep only a placeholder) and is a GeoTIFF, then choose it again.",
    ),
    (
        ("not in metres", "geographic", "degrees"),
        "The image has no coordinates in metres, so sizes cannot be measured. Reproject it to a UTM or other "
        "metre-based coordinate system (Raster > Projections > Warp) and run again.",
    ),
    (("cancelled", "canceled"), "Stopped. Nothing was changed; press the button again to start over."),
)


def friendly(message):
    """A plain message with the next step, plus the technical first line; unknown errors are returned as they are."""
    text = str(message or "").strip()
    low = text.lower()
    for keys, plain in _KNOWN:
        if any(k in low for k in keys):
            first = next((line.strip() for line in text.splitlines() if line.strip()), "")
            return plain + ("\n\nDetails: " + first[:300] if first and plain[:30].lower() not in first.lower() else "")
    return text

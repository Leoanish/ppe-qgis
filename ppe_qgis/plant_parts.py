"""What the pale plant parts PPE finds probably are, by crop and growth stage.

PPE finds them from the image alone (see fit_pale_plant in native_engine): pixels inside the canopy
that reflect plant-level near-infrared light but are not leaf-green. What they are depends on the
crop and the stage, and that is what this table says. To teach PPE a new crop, add an entry to
CROPS: the names it is known by, and what pale parts look like at each stage. Stages:

    early     "Early / vegetative"
    closed    "Closed canopy"
    flowering "Flowering / tasselling"
    drying    "Drying down / senescence"
    any       the stage was not given

Anything not listed falls back to GENERAL. Wording is shown to the user as
"... these are probably <parts>.", so write the parts as a plural noun phrase.
"""

GENERAL = {
    "early": "pale young leaves or flowers",
    "closed": "flowers, tassels or seed heads starting to show, or pale leaves",
    "flowering": "flowers, tassels or seed heads",
    "drying": "yellowing or drying leaves, ripening heads or pods",
    "any": "flowers, tassels, seed heads, pods or yellowing leaves",
}

CROPS = [
    {
        "names": ("corn", "maize"),
        "early": "pale young leaves",
        "closed": "tassels starting to show",
        "flowering": "tassels (and silks)",
        "drying": "yellowing leaves and husks",
        "any": "tassels, or yellowing leaves late in the season",
        "tip": (
            "On a corn field at tasselling (Kritis, August 2025) a threshold near 0.30 kept the tassels without letting"
            " soil in."
        ),
    },
    {
        "names": ("sorghum", "milo"),
        "flowering": "panicles (seed heads)",
        "drying": "ripening panicles and yellowing leaves",
        "any": "panicles (seed heads)",
    },
    {
        "names": ("wheat", "barley", "oat", "oats", "rye", "triticale", "rice"),
        "flowering": "heads (ears)",
        "drying": "ripening heads turning golden and drying straw",
        "any": "heads (ears) or ripening straw",
    },
    {
        "names": ("soybean", "soybeans", "soy", "soya"),
        "flowering": "flowers (small; most stay under the leaves)",
        "drying": "yellowing leaves and maturing pods",
        "any": "pods or yellowing leaves",
    },
    {
        "names": ("peanut", "peanuts", "groundnut"),
        "flowering": "yellow flowers",
        "drying": "yellowing leaves",
        "any": "yellow flowers or yellowing leaves",
    },
    {
        "names": ("cotton",),
        "flowering": "flowers (cream, turning pink)",
        "drying": "open bolls (white lint) and reddening leaves",
        "any": "flowers or open bolls",
    },
    {
        "names": ("canola", "rapeseed", "oilseed rape", "mustard"),
        "flowering": "yellow flowers",
        "drying": "ripening pods",
        "any": "yellow flowers or pods",
    },
    {
        "names": ("sunflower", "sunflowers"),
        "flowering": "yellow flower heads",
        "drying": "drying heads and leaves",
        "any": "flower heads",
    },
    {
        "names": ("potato", "potatoes"),
        "flowering": "flowers",
        "drying": "yellowing haulm (dying leaves)",
        "any": "flowers or yellowing leaves",
    },
    {"names": ("alfalfa", "lucerne", "clover"), "flowering": "flowers", "any": "flowers"},
]

STAGE_KEYS = (("Early", "early"), ("Closed", "closed"), ("Flowering", "flowering"), ("Drying", "drying"))


def stage_key(stage_text):
    """'Flowering / tasselling' -> 'flowering'; anything else (or nothing) -> 'any'."""
    for word, key in STAGE_KEYS:
        if word.lower() in str(stage_text or "").lower():
            return key
    return "any"


def find_crop(name):
    """The CROPS entry for a crop name typed by the user ('Corn hybrid A' finds corn), or None."""
    text = " " + " ".join(str(name or "").lower().replace("-", " ").replace("_", " ").split()) + " "
    for entry in CROPS:
        if any(" {} ".format(n) in text for n in entry["names"]):
            return entry
    return None


def likely_parts(crop_name=None, stage_text=None):
    """(what the pale parts probably are, a tip or '', whether the crop was recognised)."""
    key = stage_key(stage_text)
    entry = find_crop(crop_name) if crop_name else None
    if entry:
        return entry.get(key) or entry.get("any") or GENERAL[key], entry.get("tip", ""), True
    return GENERAL[key], "", False

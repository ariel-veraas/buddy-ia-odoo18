"""Look of the buddy: which character, accessory and idle gestures the administrator chose.

Pure Python (no Odoo imports) so it can be tested alone. Everything the server tells the browser about the look goes
through `public_look()`: values come from the whitelists below, never from free text, and anything unknown falls back
to the default (Mochi, no accessory, gestures on, normal frequency, every group). The browser has its own registry
(static/src/js/buddy_registry.js) and ignores ids it does not know: to add a character, an accessory or a gesture
group, add its id HERE and THERE (see docs/CUSTOMIZE.md).
"""

import re

# (id, label) pairs: the ids are what is stored and sent; the labels are Spanish source texts for the settings screen.
# A list (not a tuple) on purpose: a companion module can add its own character with `register_character()`.
CHARACTERS = [
    ("kitsune", "Kitsune (zorrito espíritu)"),
    ("mochi", "Mochi (bolita blanda)"),
]
ACCESSORIES = (
    ("none", "Ninguno"),
    ("mustache", "Bigote"),
    ("wizard", "Sombrero de mago"),
    ("shades", "Lentes de sol"),
    ("headphones", "Auriculares"),
    ("party", "Gorrito de fiesta"),
)
FREQUENCIES = (
    ("low", "Poco"),
    ("normal", "Normal"),
    ("high", "Mucho"),
)
GESTURE_GROUPS = (
    ("expressions", "Expresiones"),
    ("accessories", "Accesorios"),
    ("sleep", "Sueño"),
)

DEFAULT_CHARACTER = "mochi"
DEFAULT_ACCESSORY = "none"
DEFAULT_FREQUENCY = "normal"

CHARACTER_PARAM = "buddy_ia.character"
ACCESSORY_PARAM = "buddy_ia.accessory"
IDLE_ENABLED_PARAM = "buddy_ia.idle_enabled"  # "1" / "0" (a missing parameter means "on")
IDLE_FREQUENCY_PARAM = "buddy_ia.idle_frequency"
IDLE_GROUPS_PARAM = "buddy_ia.idle_groups"  # "expressions,sleep" or the sentinel "none"; missing = every group
NO_GROUPS = "none"


def register_character(key, label):
    """Add a character from another module (see docs/CUSTOMIZE.md, "Un buddy propio de tu empresa").

    Call it when the module's Python is imported. Idempotent; the key must be a plain lowercase id (it is stored in a
    system parameter and used as a CSS class and a template name, so nothing else is accepted). The module must also
    add the key to the settings selection (`selection_add`) and register the drawing in the JS registry.
    """
    if not (isinstance(key, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key) and isinstance(label, str) and label.strip()):
        raise ValueError("invalid character: %r" % (key,))
    if key not in ids(CHARACTERS):
        CHARACTERS.append((key, label.strip()))


def ids(options):
    return tuple(key for key, _label in options)


def clean_choice(value, options, default):
    """The value if it is one of the allowed ids, else the default. Never raises, never returns free text."""
    return value if isinstance(value, str) and value in ids(options) else default


def parse_enabled(raw):
    """Gestures are ON unless the parameter explicitly says "0"/"false"."""
    return str(raw if raw is not None else "1").strip().lower() not in ("0", "false", "no", "off")


def parse_groups(raw):
    """The allowed gesture groups, in the canonical order. Missing parameter = all of them; "none" = none."""
    if not isinstance(raw, str) or not raw.strip():
        return list(ids(GESTURE_GROUPS))
    wanted = {part.strip() for part in raw.split(",")}
    return [group for group in ids(GESTURE_GROUPS) if group in wanted]


def serialize_groups(groups):
    """What is stored for a list of groups: only known ids, canonical order, or the "none" sentinel."""
    clean = [group for group in ids(GESTURE_GROUPS) if group in set(groups)]
    return ",".join(clean) or NO_GROUPS


def public_look(get_param):
    """The look fields of /buddy/config. `get_param(name)` reads a system parameter (ir.config_parameter)."""
    return {
        "character": clean_choice(get_param(CHARACTER_PARAM), CHARACTERS, DEFAULT_CHARACTER),
        "accessory": clean_choice(get_param(ACCESSORY_PARAM), ACCESSORIES, DEFAULT_ACCESSORY),
        "idle": {
            "enabled": parse_enabled(get_param(IDLE_ENABLED_PARAM) or None),
            "frequency": clean_choice(get_param(IDLE_FREQUENCY_PARAM), FREQUENCIES, DEFAULT_FREQUENCY),
            "groups": parse_groups(get_param(IDLE_GROUPS_PARAM)),
        },
    }

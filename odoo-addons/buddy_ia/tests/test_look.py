"""The look whitelists (character, accessory, idle gestures) without Odoo: values outside the lists never get through."""
import importlib.util
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
PATH = Path(__file__).resolve().parents[1] / "models" / "buddy_look.py"
spec = importlib.util.spec_from_file_location("buddy_look_under_test", PATH)
look = importlib.util.module_from_spec(spec)
spec.loader.exec_module(look)

EVIL = ['<img src=x onerror=alert(1)>', "pi; background:url(x)", "../../etc", "Pi", " pi", "PI", "", None, 5, ["pi"], {"a": 1}, True]


class LookWhitelistTests(unittest.TestCase):
    def store(self, **values):
        return lambda name: values.get(name)

    def test_defaults_are_mochi_without_accessory_gestures_on_normal_every_group(self):
        self.assertEqual(look.public_look(self.store()), {
            "character": "mochi", "accessory": "none",
            "idle": {"enabled": True, "frequency": "normal", "groups": ["expressions", "accessories", "sleep"]}})

    def test_the_documented_ids_exist(self):
        self.assertEqual(look.ids(look.CHARACTERS), ("kitsune", "mochi"))
        self.assertEqual(look.ids(look.ACCESSORIES), ("none", "mustache", "wizard", "shades", "headphones", "party"))
        self.assertEqual(look.ids(look.FREQUENCIES), ("low", "normal", "high"))
        self.assertEqual(look.ids(look.GESTURE_GROUPS), ("expressions", "accessories", "sleep"))

    def test_every_allowed_value_passes_through_unchanged(self):
        for character in look.ids(look.CHARACTERS):
            for accessory in look.ids(look.ACCESSORIES):
                result = look.public_look(self.store(**{look.CHARACTER_PARAM: character, look.ACCESSORY_PARAM: accessory}))
                self.assertEqual((result["character"], result["accessory"]), (character, accessory))
        for frequency in look.ids(look.FREQUENCIES):
            self.assertEqual(look.public_look(self.store(**{look.IDLE_FREQUENCY_PARAM: frequency}))["idle"]["frequency"], frequency)

    def test_anything_else_falls_back_to_the_default(self):
        for bad in EVIL:
            result = look.public_look(self.store(**{look.CHARACTER_PARAM: bad, look.ACCESSORY_PARAM: bad, look.IDLE_FREQUENCY_PARAM: bad}))
            self.assertEqual((result["character"], result["accessory"], result["idle"]["frequency"]), ("mochi", "none", "normal"), bad)

    def test_a_companion_module_can_register_a_character(self):
        before = list(look.CHARACTERS)
        try:
            look.register_character("acme_fox", " Zorro Acme ")
            look.register_character("acme_fox", "otra vez")  # idempotent
            self.assertEqual(look.ids(look.CHARACTERS), ("kitsune", "mochi", "acme_fox"))
            self.assertEqual(look.public_look(self.store(**{look.CHARACTER_PARAM: "acme_fox"}))["character"], "acme_fox")
            for bad in EVIL + ["Acme", "a-b", "a b", "_x", "1x", "x" * 33, "../x"]:
                with self.assertRaises(ValueError, msg=repr(bad)):
                    look.register_character(bad, "Nombre")
            with self.assertRaises(ValueError):
                look.register_character("ok_id", "  ")
            self.assertEqual(look.ids(look.CHARACTERS), ("kitsune", "mochi", "acme_fox"))
        finally:
            look.CHARACTERS[:] = before
        # an unregistered character (e.g. its module was uninstalled) goes back to the default
        self.assertEqual(look.public_look(self.store(**{look.CHARACTER_PARAM: "acme_fox"}))["character"], "mochi")

    def test_enabled_is_on_unless_explicitly_off(self):
        for raw, expected in (("1", True), ("0", False), ("false", False), ("False", False), ("off", False), (" no ", False),
                              (None, True), ("", True), ("true", True), ("<b>", True), ("2", True)):
            self.assertEqual(look.parse_enabled(raw), expected, raw)
        # public_look reads a missing parameter as False/None: that means "on".
        self.assertTrue(look.public_look(lambda name: False)["idle"]["enabled"])

    def test_groups_keep_only_known_ids_in_canonical_order(self):
        self.assertEqual(look.parse_groups("sleep,expressions"), ["expressions", "sleep"])
        self.assertEqual(look.parse_groups("sleep, <b> ,../x,sleep"), ["sleep"])
        self.assertEqual(look.parse_groups(look.NO_GROUPS), [])
        self.assertEqual(look.parse_groups(None), ["expressions", "accessories", "sleep"])
        self.assertEqual(look.parse_groups(False), ["expressions", "accessories", "sleep"])
        self.assertEqual(look.parse_groups("   "), ["expressions", "accessories", "sleep"])
        for bad in (5, ["sleep"], {"sleep": 1}):
            self.assertEqual(look.parse_groups(bad), ["expressions", "accessories", "sleep"])

    def test_serialize_groups_round_trips_and_never_stores_an_empty_value(self):
        self.assertEqual(look.serialize_groups([]), "none")
        self.assertEqual(look.serialize_groups(["sleep", "expressions", "evil"]), "expressions,sleep")
        for groups in ([], ["expressions"], ["sleep", "accessories"], ["expressions", "accessories", "sleep"]):
            self.assertEqual(look.parse_groups(look.serialize_groups(groups)), [g for g in look.ids(look.GESTURE_GROUPS) if g in groups])

    def test_the_public_look_is_plain_data_with_no_markup(self):
        result = look.public_look(self.store(**{look.CHARACTER_PARAM: "kitsune", look.ACCESSORY_PARAM: "wizard"}))
        text = repr(result)
        for forbidden in ("<", ">", "style", "url(", ";"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()

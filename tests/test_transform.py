"""Tests for the transform engine and CLI."""

from copy import deepcopy
from pathlib import Path
import subprocess
import sys
import unittest

import yaml

from keymap_drawer.config import Config
from keymap_drawer.transform.config import TransformConfig
from keymap_drawer.transform.transformer import (
    TransformError,
    parse_combo_move_spec,
    parse_layer_move_spec,
    transform_keymap,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
STACKED_DIR = REPO_ROOT / "examples" / "stacked"

SAMPLE_KEYMAP = {
    "layout": {"ortho_layout": {"rows": 1, "columns": 2}},
    "layers": {
        "Base": ["A", {"t": "B", "h": "sticky"}],
        "Nav": ["Left", "Right"],
        "Num": ["1", "2"],
        "Target": [{"tr": "Taken"}, ""],
        "Mouse": ["M1", "M2"],
    },
    "combos": [
        {"p": [0, 1], "k": "Esc", "l": ["Nav"]},
        {"p": [0, 1], "k": "Esc", "l": ["Num"]},
        {"p": [0, 1], "k": "Tab", "l": ["Base"], "draw_separate": True},
    ],
}


class TransformParserTest(unittest.TestCase):
    """Parse CLI flag payloads."""

    def test_parse_layer_move_spec(self):
        spec = parse_layer_move_spec("Nav:t", "Base:tr")
        self.assertEqual(spec.source_layer, "Nav")
        self.assertEqual(spec.source_field, "t")
        self.assertEqual(spec.target_layer, "Base")
        self.assertEqual(spec.target_field, "tr")

    def test_parse_combo_move_spec(self):
        spec = parse_combo_move_spec("Nav,Num", "Combos")
        self.assertEqual(spec.source_layers, ("Nav", "Num"))
        self.assertEqual(spec.target_layer, "Combos")

    def test_parse_combo_move_spec_rejects_invalid_set(self):
        with self.assertRaisesRegex(TransformError, "SOURCE_SET"):
            parse_combo_move_spec("Nav,,Num", "Combos")


class TransformEngineTest(unittest.TestCase):
    """Transform engine behavior."""

    def setUp(self) -> None:
        self.cfg = TransformConfig()

    def keymap(self) -> dict:
        """Return a fresh sample keymap."""
        return deepcopy(SAMPLE_KEYMAP)

    def test_full_layer_rename_preserves_layer_data(self):
        transformed = transform_keymap(
            self.keymap(),
            transform_config=self.cfg,
            move_layers=[parse_layer_move_spec("Base", "Main")],
        )

        self.assertIn("Main", transformed["layers"])
        self.assertNotIn("Base", transformed["layers"])
        self.assertEqual(transformed["layers"]["Main"][1]["h"], "sticky")

    def test_full_layer_rename_preserves_explicit_combo_memberships(self):
        keymap = self.keymap()
        keymap["combos"] = [{"p": [0, 1], "k": "Esc", "l": ["Base"]}]

        transformed = transform_keymap(
            keymap,
            transform_config=self.cfg,
            move_layers=[parse_layer_move_spec("Base", "Main")],
        )

        self.assertEqual(transformed["combos"][0]["l"], ["Main"])

    def test_field_move_creates_target_layer(self):
        transformed = transform_keymap(
            self.keymap(),
            transform_config=self.cfg,
            move_layers=[parse_layer_move_spec("Nav", "Overlay:tr")],
        )

        self.assertIn("Overlay", transformed["layers"])
        self.assertEqual(transformed["layers"]["Overlay"][0]["tr"], "Left")
        self.assertNotIn("Nav", transformed["layers"])

    def test_include_layers_filters_before_move(self):
        transformed = transform_keymap(
            self.keymap(),
            transform_config=self.cfg,
            move_layers=[parse_layer_move_spec("Base", "Main")],
            include_layers=["Base", "Nav"],
        )

        self.assertEqual(list(transformed["layers"]), ["Nav", "Main"])
        self.assertNotIn("Num", transformed["layers"])

    def test_move_combos_deduplicates_and_skips_draw_separate(self):
        transformed = transform_keymap(
            self.keymap(),
            transform_config=self.cfg,
            move_combos=[parse_combo_move_spec("Nav,Num", "ComboLayer")],
        )

        self.assertIn("ComboLayer", transformed["layers"])
        self.assertEqual(len(transformed["combos"]), 2)
        moved_combo = transformed["combos"][0]
        self.assertEqual(moved_combo["l"], ["ComboLayer"])
        self.assertEqual(moved_combo["k"], "Esc")
        self.assertEqual(transformed["combos"][1]["draw_separate"], True)

    def test_move_combos_preserves_distinct_trigger_keys(self):
        keymap = {
            "layers": {"Base": ["A", "B", "C", "D"]},
            "combos": [
                {"tk": ["A", "B"], "k": "Esc", "l": ["Base"]},
                {"tk": ["C", "D"], "k": "Esc", "l": ["Base"]},
            ],
        }
        transformed = transform_keymap(
            keymap,
            transform_config=self.cfg,
            move_combos=[parse_combo_move_spec("Base", "Combos")],
        )

        self.assertEqual(
            transformed["combos"],
            [dict(combo, l=["Combos"]) for combo in keymap["combos"]],
        )

    def test_wildcard_combo_moves_respect_included_layers(self):
        keymap = self.keymap()
        keymap["combos"] = [
            {"p": [0, 1], "k": "NavOnly", "l": ["Nav"]},
            {"p": [0, 1], "k": "BaseOnly", "l": ["Base"]},
            {"p": [0, 1], "k": "Shared", "l": ["Base", "Nav"]},
            {"p": [0, 1], "k": "Global"},
        ]
        transformed = transform_keymap(
            keymap,
            transform_config=self.cfg,
            include_layers=["Base"],
            move_combos=[parse_combo_move_spec("*", "Combos")],
        )

        self.assertEqual(list(transformed["layers"]), ["Base", "Combos"])
        self.assertEqual(
            transformed["combos"],
            [{"p": [0, 1], "k": key, "l": ["Combos"]} for key in ("BaseOnly", "Shared", "Global")],
        )

    def test_separate_combos_skip_overlapping_moves(self):
        keymap = self.keymap()
        combo = {"p": [0, 1], "k": "Esc", "l": ["Base", "Nav"], "draw_separate": True}
        keymap["combos"] = [combo]
        moves = [parse_combo_move_spec("Base", "BaseCombos"), parse_combo_move_spec("Nav", "NavCombos")]
        transformed = transform_keymap(keymap, transform_config=self.cfg, move_combos=moves)

        self.assertEqual(transformed["combos"], [combo])
        self.assertEqual(transformed["layers"], keymap["layers"])

        combo.pop("draw_separate")
        with self.assertRaisesRegex(TransformError, "matches multiple combo moves"):
            transform_keymap(keymap, transform_config=self.cfg, move_combos=moves)

    def test_move_combos_preserves_unmoved_layer_memberships(self):
        keymap = self.keymap()
        keymap["combos"] = [{"p": [0, 1], "k": "Esc", "l": ["Base", "Nav"]}]

        transformed = transform_keymap(
            keymap,
            transform_config=self.cfg,
            move_combos=[parse_combo_move_spec("Base", "ComboLayer")],
        )

        self.assertEqual(transformed["combos"][0]["l"], ["ComboLayer", "Nav"])

    def test_move_combos_expand_global_membership_before_rewrite(self):
        keymap = self.keymap()
        keymap["combos"] = [{"p": [0, 1], "k": "Esc"}]

        transformed = transform_keymap(
            keymap,
            transform_config=self.cfg,
            move_combos=[parse_combo_move_spec("Base", "ComboLayer")],
            include_layers=["Base", "Nav"],
        )

        self.assertEqual(list(transformed["layers"]), ["Base", "Nav", "ComboLayer"])
        self.assertEqual(transformed["combos"][0]["l"], ["ComboLayer", "Nav"])

    def test_move_combos_keep_full_layer_renames_for_unmoved_memberships(self):
        keymap = self.keymap()
        keymap["combos"] = [{"p": [0, 1], "k": "Esc"}]

        transformed = transform_keymap(
            keymap,
            transform_config=self.cfg,
            move_layers=[parse_layer_move_spec("Base", "Main")],
            move_combos=[parse_combo_move_spec("Nav", "ComboLayer")],
            include_layers=["Base", "Nav"],
        )

        self.assertEqual(list(transformed["layers"]), ["Nav", "Main", "ComboLayer"])
        self.assertEqual(transformed["combos"][0]["l"], ["Main", "ComboLayer"])

    def test_move_layers_resolve_against_current_state(self):
        transformed = transform_keymap(
            self.keymap(),
            transform_config=self.cfg,
            move_layers=[
                parse_layer_move_spec("Base", "Main"),
                parse_layer_move_spec("Main:h", "Overlay:tr"),
            ],
        )

        self.assertNotIn("Base", transformed["layers"])
        self.assertNotIn("Main", transformed["layers"])
        self.assertEqual(transformed["layers"]["Overlay"][1]["tr"], "sticky")

    def test_exclude_layers_apply_after_combo_moves(self):
        keymap = self.keymap()
        keymap["combos"] = [{"p": [0, 1], "k": "Esc", "l": ["Base"]}]

        transformed = transform_keymap(
            keymap,
            transform_config=self.cfg,
            move_combos=[parse_combo_move_spec("Base", "Nav")],
            include_layers=["Base", "Nav"],
            exclude_layers=["Nav"],
        )

        self.assertEqual(list(transformed["layers"]), ["Base"])
        self.assertEqual(transformed["combos"], [])

    def test_exclude_layers_can_target_created_or_renamed_layers(self):
        transformed = transform_keymap(
            self.keymap(),
            transform_config=self.cfg,
            move_layers=[parse_layer_move_spec("Nav", "Helper")],
            exclude_layers=["Helper"],
        )

        self.assertNotIn("Helper", transformed["layers"])
        self.assertNotIn("Nav", transformed["layers"])

    def test_hidden_legends_only_apply_to_written_fields(self):
        transformed = transform_keymap(
            self.keymap(),
            transform_config=TransformConfig(hidden_held_legends=["sticky"]),
            move_layers=[parse_layer_move_spec("Base", "Main")],
        )

        self.assertNotIn("h", transformed["layers"]["Main"][1])

    def test_collision_error_is_informative(self):
        with self.assertRaisesRegex(TransformError, r'Target\[0\]\.tr is already set to "Taken"'):
            transform_keymap(
                self.keymap(),
                transform_config=self.cfg,
                move_layers=[parse_layer_move_spec("Nav", "Target:tr")],
            )

    def test_repeated_target_field_is_rejected(self):
        with self.assertRaisesRegex(TransformError, r"already targeted by --move-layer Nav stacked:tr"):
            transform_keymap(
                self.keymap(),
                transform_config=self.cfg,
                move_layers=[
                    parse_layer_move_spec("Nav", "stacked:tr"),
                    parse_layer_move_spec("Num", "stacked:tr"),
                ],
            )

    def test_move_combos_reject_conflicting_metadata_during_dedup(self):
        keymap = self.keymap()
        keymap["combos"] = [
            {"p": [0, 1], "k": "Esc", "l": ["Nav"], "a": "mid"},
            {"p": [0, 1], "k": "Esc", "l": ["Num"], "a": "top"},
        ]

        with self.assertRaisesRegex(TransformError, "conflicting combo metadata"):
            transform_keymap(
                keymap,
                transform_config=self.cfg,
                move_combos=[parse_combo_move_spec("Nav,Num", "ComboLayer")],
            )

    def test_config_accepts_legacy_stack_config_alias(self):
        cfg = Config.model_validate({"stack_config": {"hidden_corner_legends": ["Nav"]}})
        self.assertEqual(cfg.transform_config.hidden_corner_legends, ["Nav"])


class TransformCliTest(unittest.TestCase):
    """CLI-level compatibility checks."""

    def test_stack_wrapper_list_layers_does_not_require_center(self):
        keymap_path = STACKED_DIR / "ferris_sweep.yaml"

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "keymap_drawer",
                "stack-layers",
                str(keymap_path),
                "--list-layers",
            ],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("Available layers:", proc.stdout)
        self.assertIn("colemak_dh", proc.stdout)

    def test_transform_and_stack_wrapper_match_checked_in_example(self):
        expected = yaml.safe_load((STACKED_DIR / "stacked.yaml").read_text(encoding="utf-8"))
        config_path = STACKED_DIR / "config.yaml"
        keymap_path = STACKED_DIR / "ferris_sweep.yaml"

        transform_proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "keymap_drawer",
                "-c",
                str(config_path),
                "transform",
                str(keymap_path),
                "--include-layers",
                "colemak_dh",
                "fun",
                "sys",
                "num",
                "nav",
                "--move-layer",
                "colemak_dh",
                "stacked",
                "--move-layer",
                "fun",
                "stacked:tl",
                "--move-layer",
                "sys",
                "stacked:tr",
                "--move-layer",
                "num",
                "stacked:bl",
                "--move-layer",
                "nav",
                "stacked:br",
                "--move-combos",
                "colemak_dh,nav,num",
                "stacked_combos",
            ],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        wrapper_proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "keymap_drawer",
                "-c",
                str(config_path),
                "stack-layers",
                "--center",
                "colemak_dh",
                "--tl",
                "fun",
                "--tr",
                "sys",
                "--bl",
                "num",
                "--br",
                "nav",
                str(keymap_path),
                "--include-combos",
                "colemak_dh",
                "nav",
                "num",
                "--separate-combo-layer",
            ],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(yaml.safe_load(transform_proc.stdout), expected)
        self.assertEqual(yaml.safe_load(wrapper_proc.stdout), expected)
        self.assertIn("deprecated", wrapper_proc.stderr)


if __name__ == "__main__":
    unittest.main()

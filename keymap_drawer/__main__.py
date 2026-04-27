"""
Given keymap description with layers and combos (in a yaml), and physical
keyboard layout definition (either via QMK info files or using a parametrized
ortho layout), print an SVG representing the keymap to standard output.
"""

import logging
import sys
from argparse import ArgumentParser, FileType, Namespace
from importlib.metadata import version
from pathlib import Path

import yaml

from keymap_drawer import logger
from keymap_drawer.config import Config, DrawConfig
from keymap_drawer.draw import KeymapDrawer
from keymap_drawer.keymap import KeymapData
from keymap_drawer.parse import KanataKeymapParser, QmkJsonParser, ZmkKeymapParser
from keymap_drawer.transform import (
    ComboMoveSpec,
    LayerMoveSpec,
    TransformConfig,
    TransformError,
    parse_combo_move_spec,
    parse_layer_move_spec,
    transform_keymap,
)


def _merge_keymaps(base: dict, new: dict) -> dict:
    return {
        "layout": base.get("layout", {}) or new.get("layout", {}),  # override layout
        "layers": base.get("layers", {}) | new.get("layers", {}),  # merge layers
        "combos": base.get("combos", []) + new.get("combos", []),  # append combos
        "draw_config": base.get("draw_config", {}) | new.get("draw_config", {}),  # merge config
    }


def draw(args: Namespace, config: Config) -> None:
    """Draw the keymap in SVG format to stdout."""
    yaml_data: dict[str, dict] = {}
    for yaml_arg in args.keymap_yaml:
        yaml_data = _merge_keymaps(yaml_data, yaml.safe_load(yaml_arg))

    cli_layout = {
        k: v
        for k, v in (
            ("qmk_keyboard", args.qmk_keyboard),
            ("zmk_keyboard", args.zmk_keyboard),
            ("qmk_info_json", args.qmk_info_json),
            ("dts_layout", args.dts_layout),
            ("ortho_layout", args.ortho_layout),
            ("cols_thumbs_notation", args.cols_thumbs_notation),
        )
        if v is not None
    }
    keymap_layout = yaml_data.get("layout", {})

    layout = {"layout_name": keymap_layout.get("layout_name")} | cli_layout if cli_layout else keymap_layout
    if args.layout_name:
        layout["layout_name"] = args.layout_name

    logger.debug("final physical layout spec for draw: %s", layout)

    assert layout, (
        "A physical layout needs to be specified either via "
        "--qmk-keyboard/--zmk-keyboard/--qmk-info-json/--dts-layout/--ortho-layout/--cols-thumbs-notation, "
        'or in a "layout" field in the keymap_yaml'
    )

    if custom_config := yaml_data.get("draw_config"):
        config.draw_config = DrawConfig.parse_obj(config.draw_config.model_dump() | custom_config)

    drawer = KeymapDrawer(
        config=config,
        out=args.output,
        layers=yaml_data.get("layers", {}),
        layout=layout,
        combos=yaml_data.get("combos", []),
    )
    drawer.print_board(
        draw_layers=args.select_layers,
        keys_only=args.keys_only,
        combos_only=args.combos_only,
        ghost_keys=args.ghost_keys,
    )


def parse(args: Namespace, config: Config) -> None:
    """Call the appropriate parser for given args and dump YAML keymap representation to stdout."""
    if args.base_keymap:
        yaml_data = yaml.safe_load(args.base_keymap)
        base = KeymapData(
            layers=yaml_data.get("layers", {}), combos=yaml_data.get("combos", []), layout=None, config=None
        )
    else:
        base = None

    if args.qmk_keymap_json:
        parsed = QmkJsonParser(
            config.parse_config,
            args.columns,
            base_keymap=base,
            layer_names=args.layer_names,
            virtual_layers=args.virtual_layers,
        ).parse(args.qmk_keymap_json)
    elif args.zmk_keymap:
        parsed = ZmkKeymapParser(
            config.parse_config,
            args.columns,
            base_keymap=base,
            layer_names=args.layer_names,
            virtual_layers=args.virtual_layers,
        ).parse(args.zmk_keymap)
    else:
        parsed = KanataKeymapParser(
            config.parse_config,
            args.columns,
            base_keymap=base,
            layer_names=args.layer_names,
            virtual_layers=args.virtual_layers,
        ).parse(args.kanata_keymap)

    yaml.safe_dump(parsed, args.output, width=160, sort_keys=False, default_flow_style=None, allow_unicode=True)


def dump_config(args: Namespace, config: Config) -> None:
    """Dump the currently active config, either default or parsed from args."""

    def cfg_str_representer(dumper, in_str):
        if "\n" in in_str:  # use '|' style for multiline strings
            return dumper.represent_scalar("tag:yaml.org,2002:str", in_str, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", in_str)

    yaml.representer.SafeRepresenter.add_representer(str, cfg_str_representer)
    yaml.safe_dump(config.model_dump(), args.output, sort_keys=False, allow_unicode=True)


def _load_keymap_yaml(in_buf) -> dict:
    data = yaml.safe_load(in_buf) or {}
    if not isinstance(data, dict):
        raise TransformError("input keymap YAML must deserialize to a mapping")
    return data


def _merge_transform_config(base: TransformConfig, yaml_data: dict, hidden_corner_legends: list[str] | None = None) -> TransformConfig:
    cfg_data = yaml_data.get("transform_config")
    if cfg_data is None:
        cfg_data = yaml_data.get("stack_config")

    merged = base
    if cfg_data:
        merged = TransformConfig.parse_obj(base.model_dump() | cfg_data)
    if hidden_corner_legends:
        merged = TransformConfig.parse_obj(merged.model_dump() | {"hidden_corner_legends": hidden_corner_legends})
    return merged


def _run_transform(
    *,
    yaml_data: dict,
    output,
    transform_config: TransformConfig,
    move_layers: list[LayerMoveSpec],
    move_combos: list[ComboMoveSpec],
    include_layers: list[str] | None,
    exclude_layers: list[str] | None,
) -> None:
    transformed = transform_keymap(
        yaml_data,
        transform_config=transform_config,
        move_layers=move_layers,
        move_combos=move_combos,
        include_layers=include_layers,
        exclude_layers=exclude_layers,
    )
    yaml.safe_dump(transformed, output, width=160, sort_keys=False, default_flow_style=None, allow_unicode=True)


def transform(args: Namespace, config: Config) -> None:
    """Transform a keymap YAML into another keymap YAML."""
    yaml_data = _load_keymap_yaml(args.keymap_yaml)
    transform_config = _merge_transform_config(config.transform_config, yaml_data)

    try:
        move_layers = [parse_layer_move_spec(source, target) for source, target in args.move_layer or []]
        move_combos = [parse_combo_move_spec(source_set, target) for source_set, target in args.move_combos or []]
        _run_transform(
            yaml_data=yaml_data,
            output=args.output,
            transform_config=transform_config,
            move_layers=move_layers,
            move_combos=move_combos,
            include_layers=args.include_layers,
            exclude_layers=args.exclude_layers,
        )
    except TransformError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)


def stack(args: Namespace, config: Config) -> None:
    """Legacy wrapper for the deprecated `stack-layers` command."""
    yaml_data = _load_keymap_yaml(args.keymap_yaml)
    if args.list_layers:
        layers = yaml_data.get("layers", {})
        print("Available layers:")
        for name in layers.keys():
            print(f"  - {name}")
        return

    print(
        'Warning: `keymap stack-layers` is deprecated and will be removed in a future release; '
        'use `keymap transform` instead.',
        file=sys.stderr,
    )

    transform_config = _merge_transform_config(
        config.transform_config,
        yaml_data,
        hidden_corner_legends=args.hidden_corner_legends,
    )

    move_layers = [parse_layer_move_spec(args.center, "stacked")]
    for layer_name, target_field in (
        (args.tl, "tl"),
        (args.tr, "tr"),
        (args.bl, "bl"),
        (args.br, "br"),
    ):
        if layer_name:
            move_layers.append(parse_layer_move_spec(layer_name, f"stacked:{target_field}"))

    move_sources = {spec.source_layer for spec in move_layers}
    combo_sources = args.include_combos or []
    include_layers = list(dict.fromkeys([*move_sources, *combo_sources]))
    exclude_layers = [layer for layer in combo_sources if layer not in move_sources] or None

    move_combos: list[ComboMoveSpec] = []
    if combo_sources:
        target_layer = "stacked_combos" if args.separate_combo_layer else "stacked"
        move_combos.append(parse_combo_move_spec(",".join(combo_sources), target_layer))

    try:
        _run_transform(
            yaml_data=yaml_data,
            output=args.output,
            transform_config=transform_config,
            move_layers=move_layers,
            move_combos=move_combos,
            include_layers=include_layers,
            exclude_layers=exclude_layers,
        )
    except TransformError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)


def main() -> None:
    """Parse the configuration and print SVG using KeymapDrawer."""
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--version", action="version", version=version("keymap-drawer"))
    parser.add_argument("-d", "--debug", action="store_true")
    parser.add_argument(
        "-c",
        "--config",
        help="A YAML file containing settings for parsing and drawing, "
        "default can be dumped using `dump-config` command and to be modified",
        type=FileType("rt", encoding="utf-8"),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    draw_p = subparsers.add_parser("draw", help="draw an SVG representation of the keymap")
    info_srcs = draw_p.add_mutually_exclusive_group()
    info_srcs.add_argument(
        "-j",
        "--qmk-info-json",
        help="Path to QMK info.json for a keyboard, containing the physical layout description",
        type=Path,
    )
    info_srcs.add_argument(
        "-k",
        "--qmk-keyboard",
        help="Name of the keyboard in QMK to fetch info.json containing the physical layout info, "
        "including revision if any",
    )
    info_srcs.add_argument(
        "-z",
        "--zmk-keyboard",
        help="Name of the keyboard in ZMK, to look up physical layout for",
    )
    info_srcs.add_argument(
        "-d",
        "--dts-layout",
        help="Path to file containing ZMK physical layout definition in devicetree format",
        type=Path,
    )
    draw_p.add_argument(
        "-l",
        "--layout-name",
        "--qmk-layout",
        help='Name of the layout to use in the QMK keyboard info file (starting with "LAYOUT_"), '
        "or ZMK DTS physical layout (node name). Use the first defined one if not specified",
    )
    draw_p.add_argument(
        "--ortho-layout",
        help="Parametrized ortholinear layout definition in a YAML format, "
        "for example '{split: false, rows: 4, columns: 12}'",
        type=yaml.safe_load,
    )
    draw_p.add_argument(
        "-n",
        "--cols-thumbs-notation",
        help='Parametrized ortholinear layout definition in "cols+thumbs" notation, '
        "for example '23332+2 2+33331' for an asymmetric 30 key split keyboard",
    )
    draw_p.add_argument("-s", "--select-layers", help="A list of layer names to draw, draw all by default", nargs="+")
    draw_p.add_argument("--keys-only", help="Only draw keys, not combos on layers", action="store_true")
    draw_p.add_argument("--combos-only", help="Only draw combos, not keys on layers", action="store_true")
    draw_p.add_argument(
        "-g",
        "--ghost-keys",
        help="A list of zero-based key indices to assign `type: ghost` in all drawn layers",
        nargs="+",
        type=int,
    )
    draw_p.add_argument(
        "keymap_yaml",
        help='YAML file (or stdin for "-") containing keymap definition with layers and (optionally) combos, '
        "see README for schema. Can specify multiple files, which are merged together before drawing",
        type=FileType("rt", encoding="utf-8"),
        nargs="+",
    )
    draw_p.add_argument(
        "-o",
        "--output",
        help="Output to path instead of stdout",
        type=FileType("wt", encoding="utf-8"),
        default=sys.stdout,
    )

    parse_p = subparsers.add_parser(
        "parse", help="parse a QMK/ZMK keymap to YAML representation to stdout, to be used with the `draw` command"
    )
    keymap_srcs = parse_p.add_mutually_exclusive_group(required=True)
    keymap_srcs.add_argument(
        "-q", "--qmk-keymap-json", help="Path to QMK keymap.json to parse", type=FileType("rt", encoding="utf-8")
    )
    keymap_srcs.add_argument(
        "-z", "--zmk-keymap", help="Path to ZMK *.keymap to parse", type=FileType("rt", encoding="utf-8")
    )
    keymap_srcs.add_argument(
        "-k",
        "--kanata-keymap",
        help="Path to Kanata *.cfg to parse (experimental!)",
        type=FileType("rt", encoding="utf-8"),
    )
    parse_p.add_argument(
        "-b",
        "--base-keymap",
        help="A base keymap YAML to inherit certain properties from",
        type=FileType("rt", encoding="utf-8"),
    )
    parse_p.add_argument(
        "-l",
        "--layer-names",
        help="List of layer names to override parsed names; its length should match the number of layers",
        nargs="+",
    )
    parse_p.add_argument(
        "--virtual-layers",
        help='List of layer names to append as empty "virtual" layers at the end; this can be used for assigning combos etc.',
        nargs="+",
    )
    parse_p.add_argument(
        "-c",
        "--columns",
        help="Number of columns in the layout to enable better key grouping in the output, optional",
        type=int,
    )
    parse_p.add_argument(
        "-o",
        "--output",
        help="Output to path instead of stdout",
        type=FileType("wt", encoding="utf-8"),
        default=sys.stdout,
    )

    transform_p = subparsers.add_parser(
        "transform",
        help="transform a keymap YAML into another YAML (lossy: comments/formatting are not preserved)",
    )
    transform_p.add_argument(
        "keymap_yaml",
        help="YAML file containing a keymap to transform",
        type=FileType("rt", encoding="utf-8"),
    )
    transform_p.add_argument(
        "--include-layers",
        nargs="+",
        metavar="LAYER",
        help="Only keep these layers in the working set before applying moves",
    )
    transform_p.add_argument(
        "--exclude-layers",
        nargs="+",
        metavar="LAYER",
        help="Drop these layers after all moves have been applied",
    )
    transform_p.add_argument(
        "--move-layer",
        action="append",
        nargs=2,
        metavar=("SRC", "TARGET"),
        help='Move a layer or a field, e.g. "Base stacked" or "Nav stacked:tr"',
    )
    transform_p.add_argument(
        "--move-combos",
        action="append",
        nargs=2,
        metavar=("SOURCE_SET", "TARGET"),
        help='Move combos from "*" or a comma-separated source layer list to TARGET',
    )
    transform_p.add_argument(
        "-o",
        "--output",
        help="Output to path instead of stdout",
        type=FileType("wt", encoding="utf-8"),
        default=sys.stdout,
    )

    stack_p = subparsers.add_parser(
        "stack-layers",
        help="deprecated: stack multiple layers into a single multi-position legend layer",
    )
    stack_p.add_argument(
        "keymap_yaml",
        help="YAML file containing keymap with layers to stack",
        type=FileType("rt", encoding="utf-8"),
    )
    stack_p.add_argument(
        "--center",
        required=True,
        help="Layer name for center position (primary layer)",
    )
    stack_p.add_argument("--tl", help="Layer name for top-left corner")
    stack_p.add_argument("--tr", help="Layer name for top-right corner")
    stack_p.add_argument("--bl", help="Layer name for bottom-left corner")
    stack_p.add_argument("--br", help="Layer name for bottom-right corner")
    stack_p.add_argument(
        "--hidden-corner-legends",
        nargs="+",
        help="Values to hide in corner positions (e.g., modifier symbols)",
    )
    stack_p.add_argument(
        "--include-combos",
        nargs="+",
        metavar="LAYER",
        help="Include combos from specified layers, remapped to 'stacked'",
    )
    stack_p.add_argument(
        "--separate-combo-layer",
        action="store_true",
        help="Add a 'stacked_combos' layer for separate combo diagram",
    )
    stack_p.add_argument(
        "--list-layers",
        action="store_true",
        help="List available layers and exit",
    )
    stack_p.add_argument(
        "-o",
        "--output",
        help="Output to path instead of stdout",
        type=FileType("wt", encoding="utf-8"),
        default=sys.stdout,
    )

    dump_p = subparsers.add_parser(
        "dump-config", help="dump default draw and parse config to stdout that can be passed to -c/--config option"
    )
    dump_p.add_argument(
        "-o",
        "--output",
        help="Output to path instead of stdout",
        type=FileType("wt", encoding="utf-8"),
        default=sys.stdout,
    )

    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)

    config = Config.parse_obj(yaml.safe_load(args.config)) if args.config else Config()

    match args.command:
        case "draw":
            draw(args, config)
        case "parse":
            parse(args, config)
        case "transform":
            transform(args, config)
        case "stack-layers":
            stack(args, config)
        case "dump-config":
            dump_config(args, config)


if __name__ == "__main__":
    main()

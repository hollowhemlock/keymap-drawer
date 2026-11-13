"""Keymap YAML transformation engine."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from keymap_drawer.keymap import ComboSpec, KeymapData, LayoutKey

from .config import TransformConfig

FIELD_ATTRS = {
    "t": "tap",
    "h": "hold",
    "s": "shifted",
    "left": "left",
    "right": "right",
    "tl": "tl",
    "tr": "tr",
    "bl": "bl",
    "br": "br",
}


class TransformError(ValueError):
    """Raised when a requested transform cannot be applied safely."""


@dataclass(frozen=True)
class LayerMoveSpec:
    """Parsed `--move-layer` specification."""

    source_layer: str
    target_layer: str
    source_field: str | None = None
    target_field: str | None = None
    raw_source: str = ""
    raw_target: str = ""

    @property
    def raw_flag(self) -> str:
        """Return the flag payload as the user provided it."""
        return f"{self.raw_source} {self.raw_target}".strip()


@dataclass(frozen=True)
class ComboMoveSpec:
    """Parsed `--move-combos` specification."""

    raw_source_set: str
    target_layer: str
    source_layers: tuple[str, ...] | None = None

    @property
    def raw_flag(self) -> str:
        """Return the flag payload as the user provided it."""
        return f"{self.raw_source_set} {self.target_layer}".strip()


@dataclass
class LayerState:
    """Flat layer data plus enough shape info to reconstruct rows."""

    keys: list[LayoutKey]
    segments: list[tuple[bool, int]]

    def clone(self) -> "LayerState":
        """Return a deep copy of the layer."""
        return LayerState(
            keys=[key.model_copy(deep=True) for key in self.keys],
            segments=list(self.segments),
        )

    def empty_like(self) -> "LayerState":
        """Return an empty layer with the same shape."""
        return LayerState(keys=[LayoutKey() for _ in self.keys], segments=list(self.segments))

    def is_empty(self) -> bool:
        """Return whether every key in the layer is empty."""
        return not any(key.full_serializer() for key in self.keys)

    def dump(self) -> list[object]:
        """Serialize the layer using its original row grouping."""
        flat: list[str | dict[str, str]] = [key.serialize_model() for key in self.keys]
        out: list[object] = []
        offset = 0
        for is_group, length in self.segments:
            chunk = flat[offset : offset + length]
            offset += length
            if is_group:
                out.append(chunk)
            else:
                assert length == 1, f"Unexpected scalar segment length: {length}"
                out.append(chunk[0])
        return out


@dataclass
class ComboState:
    """Combo model plus serialization details that should be preserved."""

    combo: ComboSpec
    preserve_key_dict: bool = False

    def clone(self) -> "ComboState":
        """Return a deep copy of the combo state."""
        return ComboState(combo=self.combo.model_copy(deep=True), preserve_key_dict=self.preserve_key_dict)

    def dump(self) -> dict:
        """Serialize the combo while preserving explicit single-field key dicts."""
        raw = self.combo.model_dump(by_alias=True, exclude_defaults=True)
        if self.preserve_key_dict and isinstance(raw.get("k"), str):
            raw["k"] = {"t": raw["k"]}
        return raw


def parse_layer_move_spec(source: str, target: str) -> LayerMoveSpec:
    """Parse one `--move-layer SRC[:FIELD] TARGET[:FIELD]` payload."""
    source_layer, source_field = _split_layer_ref(source, allow_field=True)
    target_layer, target_field = _split_layer_ref(target, allow_field=True)

    if source_field is not None and target_field is None:
        raise TransformError(f"invalid --move-layer {source} {target}: target needs a field when source uses one")

    return LayerMoveSpec(
        source_layer=source_layer,
        source_field=source_field,
        target_layer=target_layer,
        target_field=target_field,
        raw_source=source,
        raw_target=target,
    )


def parse_combo_move_spec(source_set: str, target: str) -> ComboMoveSpec:
    """Parse one `--move-combos SOURCE_SET TARGET` payload."""
    target_layer, target_field = _split_layer_ref(target, allow_field=True)
    if target_field is not None:
        raise TransformError(
            f"invalid --move-combos {source_set} {target}: combo targets must be layer names without a field"
        )

    if source_set == "*":
        return ComboMoveSpec(raw_source_set=source_set, target_layer=target_layer, source_layers=None)

    layers = tuple(layer.strip() for layer in source_set.split(",") if layer.strip())
    if not layers or ",".join(layers) != source_set:
        raise TransformError(
            f'invalid --move-combos {source_set} {target}: SOURCE_SET must be "*" or a comma-separated layer list'
        )

    return ComboMoveSpec(raw_source_set=source_set, target_layer=target_layer, source_layers=layers)


def transform_keymap(  # pylint: disable=too-many-locals
    keymap: Mapping[str, object],
    *,
    transform_config: TransformConfig,
    move_layers: Sequence[LayerMoveSpec] = (),
    move_combos: Sequence[ComboMoveSpec] = (),
    include_layers: Sequence[str] | None = None,
    exclude_layers: Sequence[str] | None = None,
) -> dict:
    """Transform a keymap YAML mapping into a new YAML mapping."""
    if "layers" not in keymap:
        raise TransformError('input keymap is missing a "layers" mapping')
    if not isinstance(keymap["layers"], Mapping):
        raise TransformError('input keymap "layers" must be a mapping of layer names to key lists')

    all_layers = _normalize_layers(keymap["layers"])
    if not all_layers:
        raise TransformError("input keymap does not define any layers")

    layer_names = list(all_layers)
    selected_names = _select_layers(layer_names, include_layers)
    layer_renames = _build_layer_rename_map(selected_names, move_layers)
    working_layers = {name: all_layers[name].clone() for name in selected_names}
    source_layers = {name: layer.clone() for name, layer in working_layers.items()}
    output_layers = {name: layer.clone() for name, layer in working_layers.items()}
    template_layer = next(iter(source_layers.values()))

    _validate_combo_specs(move_combos, source_layers)
    touched_fields = _apply_layer_moves(output_layers, move_layers, template_layer)

    output_combos = _transform_combos(
        keymap.get("combos", []),
        move_combos=move_combos,
        output_layers=output_layers,
        source_layers=source_layers,
        template_layer=template_layer,
        layer_renames=layer_renames,
    )
    _validate_exclude_layers(exclude_layers, output_layers)
    output_layers = {name: layer for name, layer in output_layers.items() if name not in set(exclude_layers or ())}
    if not output_layers:
        raise TransformError("transform would remove every layer from the output")
    output_combos = _filter_combos(output_combos, output_layers)
    _apply_hidden_legends(output_layers, transform_config, touched_fields)

    result = dict(keymap)
    result["layers"] = {name: layer.dump() for name, layer in output_layers.items()}

    if output_combos or "combos" in keymap:
        result["combos"] = [combo.dump() for combo in output_combos]

    return result


def _split_layer_ref(value: str, *, allow_field: bool) -> tuple[str, str | None]:
    if value.count(":") > 1:
        raise TransformError(f'invalid layer reference "{value}": expected at most one ":" separator')
    if ":" not in value:
        return value, None
    if not allow_field:
        raise TransformError(f'invalid layer reference "{value}"')
    layer_name, field_name = value.split(":", maxsplit=1)
    if field_name not in FIELD_ATTRS:
        valid = ", ".join(FIELD_ATTRS)
        raise TransformError(f'invalid layer reference "{value}": field must be one of {valid}')
    return layer_name, field_name


def _normalize_layers(raw_layers: Mapping[str, object]) -> dict[str, LayerState]:
    layers: dict[str, LayerState] = {}
    for layer_name, raw_layer in raw_layers.items():
        if not isinstance(raw_layer, list):
            raise TransformError(f'layer "{layer_name}" must be a list of keys')

        flat_keys: list[LayoutKey] = []
        segments: list[tuple[bool, int]] = []
        for item in raw_layer:
            if isinstance(item, list):
                segments.append((True, len(item)))
                flat_keys.extend(LayoutKey.from_key_spec(key_spec) for key_spec in item)
            else:
                segments.append((False, 1))
                flat_keys.append(LayoutKey.from_key_spec(item))

        layers[layer_name] = LayerState(keys=flat_keys, segments=segments)

    counts = {name: len(layer.keys) for name, layer in layers.items()}
    if len(set(counts.values())) > 1:
        details = ", ".join(f"{name}={count}" for name, count in counts.items())
        raise TransformError(f"input keymap has inconsistent layer lengths: {details}")

    return layers


def _select_layers(
    layer_names: Sequence[str],
    include_layers: Sequence[str] | None,
) -> list[str]:
    selected = list(layer_names)
    if include_layers is not None:
        unknown = [name for name in include_layers if name not in layer_names]
        if unknown:
            raise TransformError(f'unknown layer(s) in --include-layers: {", ".join(unknown)}')
        selected = [name for name in layer_names if name in set(include_layers)]

    return selected


def _build_layer_rename_map(layer_names: Sequence[str], move_layers: Sequence[LayerMoveSpec]) -> dict[str, str]:
    renames = {name: name for name in layer_names}
    for spec in move_layers:
        if spec.target_field is not None:
            continue
        for original_name, current_name in list(renames.items()):
            if current_name == spec.source_layer:
                renames[original_name] = spec.target_layer
    return renames


def _validate_exclude_layers(
    exclude_layers: Sequence[str] | None,
    output_layers: Mapping[str, LayerState],
) -> None:
    if exclude_layers is None:
        return
    unknown = [name for name in exclude_layers if name not in output_layers]
    if unknown:
        raise TransformError(f'unknown layer(s) in --exclude-layers: {", ".join(unknown)}')


def _validate_combo_specs(move_combos: Sequence[ComboMoveSpec], source_layers: Mapping[str, LayerState]) -> None:
    for spec in move_combos:
        if spec.source_layers is None:
            continue
        unknown = [name for name in spec.source_layers if name not in source_layers]
        if unknown:
            raise TransformError(
                f'cannot apply --move-combos {spec.raw_flag}: unknown source layer(s) {", ".join(unknown)}'
            )


def _apply_layer_moves(
    output_layers: dict[str, LayerState],
    move_layers: Sequence[LayerMoveSpec],
    template_layer: LayerState,
) -> dict[str, set[str]]:
    seen_target_fields: dict[tuple[str, str], LayerMoveSpec] = {}
    touched_fields: dict[str, set[str]] = {}

    for spec in move_layers:
        source = output_layers.get(spec.source_layer)
        if source is None:
            raise TransformError(
                f'cannot apply --move-layer {spec.raw_flag}: unknown source layer "{spec.source_layer}"'
            )
        if spec.target_layer == spec.source_layer:
            raise TransformError(
                f"cannot apply --move-layer {spec.raw_flag}: source and target layer names must differ"
            )

        if spec.target_field is None:
            _apply_full_layer_move(output_layers, source, spec)
            touched_fields.setdefault(spec.target_layer, set()).update(FIELD_ATTRS)
        else:
            seen_key = (spec.target_layer, spec.target_field)
            if seen_key in seen_target_fields:
                first_spec = seen_target_fields[seen_key]
                raise TransformError(
                    f"cannot apply --move-layer {spec.raw_flag}: "
                    f"destination {spec.target_layer}:{spec.target_field} is already targeted by "
                    f"--move-layer {first_spec.raw_flag}"
                )
            seen_target_fields[seen_key] = spec
            _apply_field_move(output_layers, source, spec, template_layer)
            touched_fields.setdefault(spec.target_layer, set()).add(spec.target_field)

        output_layers.pop(spec.source_layer, None)

    return touched_fields


def _apply_full_layer_move(output_layers: dict[str, LayerState], source: LayerState, spec: LayerMoveSpec) -> None:
    target = output_layers.get(spec.target_layer)
    if target is not None:
        if len(target.keys) != len(source.keys):
            raise TransformError(
                f"cannot apply --move-layer {spec.raw_flag}: "
                f'target layer "{spec.target_layer}" has {len(target.keys)} keys, expected {len(source.keys)}'
            )
        if not target.is_empty():
            raise TransformError(
                f'cannot apply --move-layer {spec.raw_flag}: target layer "{spec.target_layer}" is not empty'
            )
    output_layers[spec.target_layer] = source.clone()


def _apply_field_move(
    output_layers: dict[str, LayerState],
    source: LayerState,
    spec: LayerMoveSpec,
    template_layer: LayerState,
) -> None:
    source_field = spec.source_field or "t"
    target_field = spec.target_field
    assert target_field is not None
    target = output_layers.get(spec.target_layer)
    if target is None:
        target = source.empty_like()
        if len(target.keys) != len(template_layer.keys):
            target = template_layer.empty_like()
        output_layers[spec.target_layer] = target

    if len(target.keys) != len(source.keys):
        raise TransformError(
            f"cannot apply --move-layer {spec.raw_flag}: "
            f'target layer "{spec.target_layer}" has {len(target.keys)} keys, expected {len(source.keys)}'
        )

    for key_index, (source_key, target_key) in enumerate(zip(source.keys, target.keys)):
        source_value = _get_field(source_key, source_field)
        if not source_value:
            continue

        existing = _get_field(target_key, target_field)
        if existing:
            raise TransformError(
                f"cannot apply --move-layer {spec.raw_flag}: "
                f'{spec.target_layer}[{key_index}].{target_field} is already set to "{existing}"'
            )
        _set_field(target_key, target_field, source_value)


def _transform_combos(  # pylint: disable=too-many-locals
    raw_combos: object,
    *,
    move_combos: Sequence[ComboMoveSpec],
    output_layers: dict[str, LayerState],
    source_layers: Mapping[str, LayerState],
    template_layer: LayerState,
    layer_renames: Mapping[str, str],
) -> list[ComboState]:
    if raw_combos is None:
        raw_combos = []
    if not isinstance(raw_combos, list):
        raise TransformError('input keymap "combos" must be a list when present')

    combos = [
        ComboState(
            combo=ComboSpec(**ComboSpec.normalize_fields(dict(combo_spec))),
            preserve_key_dict=isinstance(combo_spec.get("k"), dict),
        )
        for combo_spec in raw_combos
    ]
    moved_seen: dict[tuple[str, tuple[int, ...], tuple[str, ...], str], ComboState] = {}
    out: list[ComboState] = []

    for combo_state in _filter_combos(combos, source_layers):
        combo = combo_state.combo
        if combo.trigger_keys:
            try:
                KeymapData(
                    layers={name: layer.keys for name, layer in source_layers.items()},
                    combos=[combo],
                    layout=None,
                )
            except ValueError as exc:
                raise TransformError(f"cannot resolve combo trigger keys: {exc}") from exc
            # Serialize only positions so drawing does not re-resolve legends on moved layers.
            combo.trigger_keys = []
        matching_specs = [
            spec for spec in move_combos if not combo.draw_separate and _combo_matches_spec(combo, spec, source_layers)
        ]
        if len(matching_specs) > 1:
            flags = ", ".join(f"--move-combos {spec.raw_flag}" for spec in matching_specs)
            raise TransformError(
                "cannot move combo at positions " f"{combo.key_positions}: it matches multiple combo moves ({flags})"
            )

        if matching_specs:
            spec = matching_specs[0]
            if spec.target_layer not in output_layers:
                output_layers[spec.target_layer] = template_layer.empty_like()

            moved = combo_state.clone()
            moved.combo.layers = _rewrite_combo_layers(combo, spec, source_layers, layer_renames)
            dedup_key = (
                spec.target_layer,
                tuple(sorted(moved.combo.key_positions)),
                tuple(sorted(_combo_key_signature(key) for key in moved.combo.trigger_keys)),
                _combo_key_signature(moved.combo.key),
            )
            if dedup_key in moved_seen:
                existing = moved_seen[dedup_key]
                if _combo_metadata_signature(existing.combo) != _combo_metadata_signature(moved.combo):
                    raise TransformError(
                        "cannot move combo at positions "
                        f"{combo.key_positions} to layer {spec.target_layer}: "
                        "conflicting combo metadata for combos sharing the same positions and key"
                    )
                existing.combo.layers = _merge_layer_names(existing.combo.layers, moved.combo.layers)
                continue
            moved_seen[dedup_key] = moved
            out.append(moved)
            continue

        untouched = combo_state.clone()
        if untouched.combo.layers:
            untouched.combo.layers = _rename_layer_names(untouched.combo.layers, layer_renames)
        out.append(untouched)

    return out


def _combo_matches_spec(combo: ComboSpec, spec: ComboMoveSpec, source_layers: Mapping[str, LayerState]) -> bool:
    if spec.source_layers is None:
        return True
    if not combo.layers:
        return any(layer in source_layers for layer in spec.source_layers)
    return any(layer in spec.source_layers for layer in combo.layers)


def _rewrite_combo_layers(
    combo: ComboSpec,
    spec: ComboMoveSpec,
    source_layers: Mapping[str, LayerState],
    layer_renames: Mapping[str, str],
) -> list[str]:
    original_layers = list(combo.layers) if combo.layers else list(source_layers)
    current_layers = [layer_renames.get(layer, layer) for layer in original_layers]
    moved_from = set(spec.source_layers or original_layers)
    rewritten: list[str] = []
    inserted_target = False

    for original_layer, current_layer in zip(original_layers, current_layers):
        if original_layer in moved_from:
            if not inserted_target:
                rewritten.append(spec.target_layer)
                inserted_target = True
            continue
        rewritten.append(current_layer)

    if not inserted_target:
        rewritten.append(spec.target_layer)

    return _merge_layer_names((), rewritten)


def _rename_layer_names(layers: Sequence[str], layer_renames: Mapping[str, str]) -> list[str]:
    return _merge_layer_names((), [layer_renames.get(layer, layer) for layer in layers])


def _merge_layer_names(existing: Sequence[str], new_layers: Sequence[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for layer in [*existing, *new_layers]:
        if layer in seen:
            continue
        seen.add(layer)
        merged.append(layer)
    return merged


def _filter_combos(combos: Sequence[ComboState], output_layers: Mapping[str, LayerState]) -> list[ComboState]:
    final_layers = set(output_layers)
    filtered: list[ComboState] = []
    for combo_state in combos:
        combo = combo_state.combo
        if combo.layers:
            combo.layers = [layer for layer in combo.layers if layer in final_layers]
            if not combo.layers:
                continue
        elif not final_layers:
            continue
        filtered.append(combo_state)
    return filtered


def _combo_key_signature(key: LayoutKey) -> str:
    return json.dumps(key.model_dump(exclude_defaults=True, by_alias=True), sort_keys=True)


def _combo_metadata_signature(combo: ComboSpec) -> str:
    raw = combo.model_dump(exclude_defaults=True, by_alias=True)
    for field in ("l", "p", "tk", "k"):
        raw.pop(field, None)
    return json.dumps(raw, sort_keys=True)


def _apply_hidden_legends(
    layers: Mapping[str, LayerState],
    cfg: TransformConfig,
    touched_fields: Mapping[str, set[str]],
) -> None:
    hidden_corner = set(cfg.hidden_corner_legends)
    hidden_held = set(cfg.hidden_held_legends)
    hidden_shifted = set(cfg.hidden_shifted_legends)

    for layer_name, layer in layers.items():
        active_fields = touched_fields.get(layer_name, set())
        if not active_fields:
            continue
        for key in layer.keys:
            if "h" in active_fields and key.hold in hidden_held:
                key.hold = ""
            if "s" in active_fields and key.shifted in hidden_shifted:
                key.shifted = ""
            if "tl" in active_fields and key.tl in hidden_corner:
                key.tl = ""
            if "tr" in active_fields and key.tr in hidden_corner:
                key.tr = ""
            if "bl" in active_fields and key.bl in hidden_corner:
                key.bl = ""
            if "br" in active_fields and key.br in hidden_corner:
                key.br = ""


def _get_field(key: LayoutKey, field: str) -> str:
    return getattr(key, FIELD_ATTRS[field])


def _set_field(key: LayoutKey, field: str, value: str) -> None:
    setattr(key, FIELD_ATTRS[field], value)

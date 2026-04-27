# Stacked Layer Example

Example files for the `transform` stacked-layer recipe. See [TRANSFORM.md](../../TRANSFORM.md) for full documentation.

## Files

- `ferris_sweep.yaml` - Parsed keymap with multiple layers
- `config.yaml` - Configuration with corner styling and filtering

## Commands

```bash
# Stack and draw
keymap -c config.yaml transform ferris_sweep.yaml \
  --include-layers colemak_dh fun sys num nav \
  --move-layer colemak_dh stacked \
  --move-layer fun stacked:tl \
  --move-layer sys stacked:tr \
  --move-layer num stacked:bl \
  --move-layer nav stacked:br \
  | keymap -c config.yaml draw -k "ferris/sweep" - -o stacked.svg

# With combos stacked
keymap -c config.yaml transform ferris_sweep.yaml \
  --include-layers colemak_dh fun sys num nav \
  --move-layer colemak_dh stacked \
  --move-layer fun stacked:tl \
  --move-layer sys stacked:tr \
  --move-layer num stacked:bl \
  --move-layer nav stacked:br \
  --move-combos colemak_dh,nav,num stacked -o stacked.yaml
keymap -c config.yaml draw -k "ferris/sweep" stacked.yaml -o stacked.svg

# With combos as a separate layer
keymap -c config.yaml transform ferris_sweep.yaml \
  --include-layers colemak_dh fun sys num nav \
  --move-layer colemak_dh stacked \
  --move-layer fun stacked:tl \
  --move-layer sys stacked:tr \
  --move-layer num stacked:bl \
  --move-layer nav stacked:br \
  --move-combos colemak_dh,nav,num stacked_combos -o stacked.yaml
keymap -c config.yaml draw -k "ferris/sweep" stacked.yaml -o stacked.svg
```

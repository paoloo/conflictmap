# Conflict Map for CyberEther

The world-conflict intel pipeline as a [CyberEther](https://github.com/luigifcruz/CyberEther)
flowgraph: Python blocks read the local `newsgather.db`, rasterize conflict
heat, UCDP incident dots and correlation lines into an RGBA frame tensor, and
publish the ranked list to the flowgraph environment. CyberEther's native
`frame` block displays the map — zoom, pan, pixel inspection for free.

## Layout

```
cyberether/
  ce_map.py               data layer: reads the TUI's data/newsgather.db, refreshes on an interval
  mapraster.py            numpy rasterizer: world outlines + heat blobs + dots + links
  intel/                  vendored pipeline (copy of ../conflictmap/intel)
  cot/                    vendored CoT listener (unused by the flowgraph yet)
  app_config.py           config dataclasses (for `python -m intel.pipeline`)
  generate_flowgraph.py   writes flowgraph.yml with absolute paths baked in
  flowgraph.yml           the CyberEther flowgraph (generated, gitignored)
  run.sh                  regenerate + launch
```

The map data (Natural Earth geojson) is shared with the TUI — `../data/naturalearth/`.
The database is the TUI's `../data/newsgather.db`: refresh the data once (TUI's `U` key
or the pipeline CLI) and every UI — curses, Qt, CyberEther — sees the same run.

## Blocks in the flowgraph

| Block | Module | Job |
|---|---|---|
| `map_source` | python | reads the DB every 30 s, renders the F32 `[480, 960, 4]` map frame, publishes counts to `ctx.env["conflictmap"]` |
| `ranking_source` | python | publishes the ranked conflict list to `ctx.env["ranking_text"]` |
| `map_view` | frame | displays the map tensor (zoom/pan/pixel inspect) |
| `ranking_note` | note | documentation panel |

## Run

```bash
./run.sh
```

Requires the CyberEther app and the conda `core` env (numpy, requests — the
same env the intel pipeline uses). The script passes `--python-runtime` so
the blocks run on that env.

## Refreshing the intel data

The flowgraph re-reads the database every 30 s but does **not** run the
pipeline itself. Update the data with the sibling repo:

```bash
cd .. && make run   # press U in the TUI, or:
python -m conflictmap.intel.pipeline --db data/newsgather.db
```

The CyberEther map picks up the new ranking on its next refresh tick.

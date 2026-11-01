#!/usr/bin/env python
"""Generate flowgraph.yml with absolute paths baked in (CyberEther python
blocks run in their own interpreter context; they import ce_map by path)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT.name  # cyberether

# --- python block sources (kept here, embedded into the YAML) -------------

MAP_BLOCK = '''\
import sys
sys.path.insert(0, r"{root!s}")

import numpy as np
import ce_map

_state = {{"frame": None, "snap": None, "last": 0.0, "frames": 0}}

def compute(ctx):
    now = __import__("time").monotonic()
    if _state["snap"] is None or now - _state["last"] >= ce_map.REFRESH_SECONDS:
        _state["snap"] = ce_map.load_snapshot(force=(_state["snap"] is None))
        _state["frame"] = ce_map.render_frame(_state["snap"])
        _state["last"] = now
    snap = _state["snap"]
    ctx.outputs[0][...] = _state["frame"]
    _state["frames"] += 1
    ctx.env["conflictmap"] = {{
        "frames": _state["frames"],
        "conflicts": len(snap["conflicts"]) if snap else 0,
        "events": len(snap["events"]) if snap else 0,
        "links": len(snap["links"]) if snap else 0,
    }}
'''.replace("{root!s}", str(ROOT)).replace("{{", "{").replace("}}", "}")

RANKING_BLOCK = '''\
import sys
sys.path.insert(0, r"{root!s}")

import ce_map

_state = {{"last": -1e9, "text": "loading..."}}

def compute(ctx):
    now = __import__("time").monotonic()
    if now - _state["last"] >= ce_map.REFRESH_SECONDS:
        snap = ce_map.load_snapshot()
        _state["text"] = ce_map.ranking_text(snap)
        _state["last"] = now
    ctx.env["ranking"] = {"text": _state["text"]}
'''.replace("{root!s}", str(ROOT)).replace("{{", "{").replace("}}", "}")


def main() -> None:
    yaml_text = f"""---
version: 2
title: Conflict Map
summary: World-conflict intel pipeline rendered as a live map frame.
author: Paolo Oliveira
license: MIT
description: >
  Reads the local intel database (newsgather.db), renders conflict heat,
  UCDP incident dots and correlation lines over a world map raster, and
  publishes the ranking to the flowgraph environment. Refreshes itself
  every {30} seconds; run the intel pipeline (conflictmap repo) to update
  the underlying data.
graph:
  - name: map_source
    module: python
    device: cpu
    runtime: python
    provider: generic
    config:
      code: |-
{chr(10).join('        ' + line for line in MAP_BLOCK.splitlines())}
      inputCount: 0
      outputCount: 1
      outputTensorSpecs:
        - shape: '[480, 960, 4]'
          axes: ''
          dtype: F32
          device: cpu
      throttled: true
    meta:
      node:
        x: 60
        y: 80
        width: 420
        height: 0
        configCollapsed: false

  - name: ranking_source
    module: python
    device: cpu
    runtime: python
    provider: generic
    config:
      code: |-
{chr(10).join('        ' + line for line in RANKING_BLOCK.splitlines())}
      inputCount: 0
      outputCount: 0
      throttled: true
    meta:
      node:
        x: 60
        y: 380
        width: 420
        height: 0
        configCollapsed: true

  - name: map_view
    module: frame
    device: cpu
    runtime: native
    provider: generic
    config:
      fit: contain
      colormap: grayscale
      autoRange: false
      interpolation: nearest
      xLabel: Longitude
      yLabel: Latitude
    input:
      frame: '${{graph.map_source.output.output0}}'
    meta:
      node:
        x: 560
        y: 80
        width: 700
        height: 0
        configCollapsed: false
      surface_default:
        attachedHeight: 700
        detachedWidth: 960
        detachedHeight: 480
        detached: false

  - name: ranking_note
    module: note
    device: cpu
    runtime: native
    provider: generic
    config:
      content: |-
        # Conflict ranking
        Live ranking comes from the flowgraph environment (conflictmap blocks).

        Open the **Environment** window to see `ranking_text` — the ranked
        conflict list refreshed by the ranking_source block.
    meta:
      node:
        x: 560
        y: 620
        width: 420
        height: 0
        configCollapsed: false
"""
    out = ROOT / "flowgraph.yml"
    out.write_text(yaml_text)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

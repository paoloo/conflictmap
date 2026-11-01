# tui — Terrascope-style terminal UI. `make` / `make run` starts the app,
# `make test` runs the smoke checks. Same conventions as ../new/Makefile.

ROOT   := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
CONFIG ?= $(ROOT)/config.toml
ENV    ?= core
PY     ?= $(if $(wildcard $(ROOT)/.venv/bin/python),$(ROOT)/.venv/bin/python,conda run -n $(ENV) --no-capture-output python)

.DEFAULT_GOAL := run
.PHONY: run test deps clean help

run:            ## start the terminal app
	cd "$(ROOT)" && $(PY) -m conflictmap --config $(CONFIG)

test:           ## import + render smoke checks (no terminal needed)
	cd "$(ROOT)" && conda run -n $(ENV) python -c "\
import sys; sys.path.insert(0, '.');\
import conflictmap.main as m;\
cfg, basemap, conflicts, links, tracks = m.build(m.PACKAGE_ROOT / 'config.toml');\
rings, labels, places = m.load_map_data();\
basemap.set_data(labels, places);\
from conflictmap.core import world as wm;\
w = wm.WorldMap([], [basemap, links, conflicts, tracks]);\
w.set_prepared_countries(wm.prepare_ring_set(rings));\
f = w.render(160, 48);\
print('render ok:', len(f.layers), 'layers,', len(f.country_lines), 'rows')"

deps:           ## install the few extra packages into conda env `core`
	cd "$(ROOT)" && conda run -n $(ENV) python -m pip install pyshp

clean:          ## remove Python caches
	find "$(ROOT)" -name __pycache__ -prune -exec rm -rf {} +

help:           ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

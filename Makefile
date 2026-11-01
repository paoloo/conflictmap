# tui — Terrascope-style terminal world-conflict map. `make` starts the app,
# `make test` runs the smoke checks, `make deps` installs into the venv/conda env.

ROOT   := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
ENV    ?= core
# --no-capture-output: curses needs the real terminal on stdin/stdout.
PY     ?= $(if $(wildcard $(ROOT)/.venv/bin/python),$(ROOT)/.venv/bin/python,conda run -n $(ENV) --no-capture-output python)
PIP    ?= $(if $(wildcard $(ROOT)/.venv/bin/pip),$(ROOT)/.venv/bin/pip,conda run -n $(ENV) python -m pip)

.DEFAULT_GOAL := run
.PHONY: run test deps clean help

run:            ## start the terminal app
	cd "$(ROOT)" && $(PY) -m terrascope_conflicts

test:           ## import + render smoke checks (no terminal needed)
	cd "$(ROOT)" && $(PY) -c "\
import sys; sys.path.insert(0, '.');\
import terrascope_conflicts.main as m;\
cfg, basemap, conflicts, links, tracks = m.build(m.PACKAGE_ROOT / 'config.toml');\
rings, labels, places = m.load_map_data();\
basemap.set_data(labels, places);\
from terrascope_conflicts.core import world as wm;\
w = wm.WorldMap([], [basemap, links, conflicts, tracks]);\
w.set_prepared_countries(wm.prepare_ring_set(rings));\
f = w.render(160, 48);\
print('render ok:', len(f.layers), 'layers,', len(f.country_lines), 'rows');\
from terrascope_conflicts.app.data import PROJECT_DB, PROJECT_NODES_DB;\
print('intel db:', PROJECT_DB);\
print('nodes db:', PROJECT_NODES_DB)"

deps:           ## install requirements (venv if present, else conda env)
	cd "$(ROOT)" && $(PIP) install -r requirements.txt

clean:          ## remove Python caches
	find "$(ROOT)" -name __pycache__ -prune -exec rm -rf {} +

help:           ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

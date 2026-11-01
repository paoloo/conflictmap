"""Terminal UI for the world-conflicts project (new/), Terrascope-style.

A curses app that renders the world map as Unicode Braille cells (2x4 dots per
terminal cell) with the intel pipeline's conflict ranking as a red heatmap
(correlation links as lines, node markers and CoT tracks on top). Rendering,
view maths and the layer protocol are modelled on a-shygun/Terrascope; the
data side (pipeline.load_latest, recent_events, correlate) is the project's
own, shared with the Qt app — no Qt involved.
"""

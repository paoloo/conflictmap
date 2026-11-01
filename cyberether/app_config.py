"""Configuration handling: TOML file -> typed dataclasses."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MapConfig:
    mbtiles: str = "data/map.mbtiles"
    start_lat: float = 34.7663
    start_lon: float = 2.5011
    start_zoom: float = 11.0
    tile_mem_mb: int = 128


@dataclass
class CoTConfig:
    enabled: bool = False
    group: str = "239.2.3.1"
    port: int = 6969
    send_interval_s: float = 1.0
    # Own position for the self-SA marker. 0.0 -> use the map start position.
    self_lat: float = 0.0
    self_lon: float = 0.0


@dataclass
class NodesConfig:
    share_over_cot: bool = True


@dataclass
class ApiConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass
class IntelConfig:
    """World-conflict pipeline (intel/pipeline.py): gather -> rank -> summarize."""
    db: str = "data/newsgather.db"
    refresh_min: float = 0.0        # background run every N minutes; 0 = manual only
    gather: bool = True             # fetch sources before ranking
    summarize: bool = True          # local chat model (off = rank only)
    model: str = "huggingface.co/TeskesLab/Liberation-Qwen3-8B:Q4_K_M"
    ollama_host: str = "http://127.0.0.1:11434"
    reranker_model: str = "gpustack/bge-reranker-v2-m3-GGUF:Q4_K_M"
    reranker_port: int = 8091
    hours: int = 72
    max_age_days: int = 14
    keep: int = 12
    min_score: float = -5.0


@dataclass
class AggressionConfig:
    """Temporary aggression points: one switch for the whole conflict overlay."""
    enabled: bool = True            # startup state; toggle live with Ctrl+Shift+A
    ttl_hours: float = 24.0         # conflict points vanish when the assessment is older
    manual_ttl_hours: float = 2.0   # hand-placed points (right-click) live this long
    min_level: int = 1              # hide conflicts below this situation level
    show_events: bool = True        # UCDP event dots (fatal incidents)
    events_days: int = 30


@dataclass
class LinksConfig:
    """Automatic correlation links (intel/correlate.py) drawn between related entities."""
    enabled: bool = True            # startup state; toggle live with Ctrl+Shift+L
    labels: bool = True             # draw the strongest reason at each link's midpoint
    min_strength: float = 2.0       # conflict<->conflict threshold (1 = shared country alone)
    entity_min_strength: float = 2.0
    radius_km: float = 150.0        # node/track/point within this of a conflict or its incidents
    nodes: bool = True              # correlate user nodes
    tracks: bool = True             # correlate CoT/API tracks
    points: bool = True             # correlate manual aggression points


@dataclass
class VideoSource:
    name: str
    url: str
    downscale: float = 1.0


@dataclass
class AppConfig:
    callsign: str = "AUGUR"
    marker_interval_s: float = 10.0
    map: MapConfig = field(default_factory=MapConfig)
    cot: CoTConfig = field(default_factory=CoTConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    nodes: NodesConfig = field(default_factory=NodesConfig)
    intel: IntelConfig = field(default_factory=IntelConfig)
    aggression: AggressionConfig = field(default_factory=AggressionConfig)
    links: LinksConfig = field(default_factory=LinksConfig)
    videos: list[VideoSource] = field(default_factory=list)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"Invalid TOML in {path}: {e}") from e


def _section(cls, d: dict[str, Any]):  # noqa: ANN001, ANN202
    """Dataclass from a TOML table: defaults for missing keys, coerced to the field type."""
    obj = cls()
    for name, default in vars(obj).items():
        if name in d:
            setattr(obj, name, type(default)(d[name]))
    return obj


def load_config(path: str = "config.toml") -> AppConfig:
    """Load config from TOML; missing file -> all defaults. Call from main thread once."""
    data = _read_toml(Path(path))
    cfg = AppConfig()

    cs = data.get("general", {})
    cfg.callsign = str(cs.get("callsign", cfg.callsign))
    cfg.marker_interval_s = float(cs.get("marker_interval_s", cfg.marker_interval_s))

    m = data.get("map", {})
    cfg.map = MapConfig(
        mbtiles=str(m.get("mbtiles", cfg.map.mbtiles)),
        start_lat=float(m.get("start_lat", cfg.map.start_lat)),
        start_lon=float(m.get("start_lon", cfg.map.start_lon)),
        start_zoom=float(m.get("start_zoom", cfg.map.start_zoom)),
        tile_mem_mb=int(m.get("tile_mem_mb", cfg.map.tile_mem_mb)),
    )

    c = data.get("cot", {})
    cfg.cot = CoTConfig(
        enabled=bool(c.get("enabled", cfg.cot.enabled)),
        group=str(c.get("group", cfg.cot.group)),
        port=int(c.get("port", cfg.cot.port)),
        send_interval_s=float(c.get("send_interval_s", cfg.cot.send_interval_s)),
        self_lat=float(c.get("self_lat", cfg.cot.self_lat)),
        self_lon=float(c.get("self_lon", cfg.cot.self_lon)),
    )

    n = data.get("nodes", {})
    cfg.nodes = NodesConfig(
        share_over_cot=bool(n.get("share_over_cot", cfg.nodes.share_over_cot)),
    )

    a = data.get("api", {})

    cfg.api = ApiConfig(
        enabled=bool(a.get("enabled", cfg.api.enabled)),
        host=str(a.get("host", cfg.api.host)),
        port=int(a.get("port", cfg.api.port)),
    )

    cfg.intel = _section(IntelConfig, data.get("intel", {}))
    cfg.aggression = _section(AggressionConfig, data.get("aggression", {}))
    cfg.links = _section(LinksConfig, data.get("links", {}))

    for v in data.get("videos", []):
        cfg.videos.append(
            VideoSource(
                name=str(v.get("name", "video")),
                url=str(v["url"]),
                downscale=float(v.get("downscale", 1.0)),
            )
        )

    return cfg

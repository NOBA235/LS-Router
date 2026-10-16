"""Settings for the Phase 1 data pipeline. Standard library only."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
REPO_ROOT = DATA_DIR.parent
CACHE_DIR = DATA_DIR / "cache"
RAW_DIR = CACHE_DIR / "raw"
CURATED_DIR = DATA_DIR / "curated"
NORMALIZED_PATH = CACHE_DIR / "normalized.json"
REPORT_MD_PATH = CACHE_DIR / "report.md"

PRIMARY_GRAPH = "landslide"  # synthetic test data is refused in this graph (see seed.py)

# Overpass endpoints are tried in order. Override with OVERPASS_ENDPOINTS (comma separated).
DEFAULT_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

# Placeholder planning speeds for hill roads, used ONLY when a way has no usable maxspeed tag.
# These are assumptions, not measurements. Every segment records which basis its time uses.
DEFAULT_SPEEDS_KMH = {
    "motorway": 50.0,
    "trunk": 35.0,
    "primary": 30.0,
    "secondary": 25.0,
    "tertiary": 20.0,
    "default": 20.0,
}

HIGHWAY_CLASSES = (
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link",
)


@dataclass(frozen=True)
class PipelineConfig:
    # (south, west, north, east). Corridor framing plus margin; edges leaving it are clipped.
    bbox: tuple = (25.55, 93.60, 26.00, 94.25)
    corridor_id: str = "nh29-dimapur-kohima"
    corridor_name: str = "Dimapur-Kohima corridor (NH29)"
    # A way counts as NH29 only if its ref matches. Check the ref summary printed by `fetch`.
    nh_ref_regex: str = r"(^|;)\s*NH[\s-]*29\s*(;|$)"
    highway_classes: tuple = HIGHWAY_CLASSES
    default_speeds_kmh: dict = field(default_factory=lambda: dict(DEFAULT_SPEEDS_KMH))
    use_assumed_speeds: bool = True
    # Max distance from a feature to its nearest road vertex before it is left unattached.
    snap_threshold_m: dict = field(default_factory=lambda: {"town": 2500.0, "village": 1000.0, "hospital": 800.0})
    reference_names: dict = field(default_factory=lambda: {"dimapur": "Dimapur", "kohima": "Kohima"})
    dedupe_place_m: float = 500.0
    dedupe_hospital_m: float = 300.0
    monsoon_start_month: int = 6
    monsoon_end_month: int = 9

    def to_dict(self) -> dict:
        return asdict(self)


def load_env(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from the repo-root .env without overriding real environment variables."""
    path = path or (REPO_ROOT / ".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

r"""Fetch OpenStreetMap data for the corridor, cache it, and normalize it. Standard library only.

    python data\fetch_osm.py fetch        # Overpass -> data/cache/raw/*.json (needs network, run once)
    python data\fetch_osm.py normalize    # cached raw -> data/cache/normalized.json + report.md (offline)
    python data\fetch_osm.py all
    python data\fetch_osm.py summary      # what the cached extract contains (ref values, counts)

Nothing here runs at application startup. Data (c) OpenStreetMap contributors, ODbL 1.0.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pipeline_config as pc
from osm_normalize import NormalizationError, normalize

ATTRIBUTION = {"name": "OpenStreetMap via Overpass API", "license": "ODbL 1.0",
               "license_url": "https://www.openstreetmap.org/copyright", "attribution": "\u00a9 OpenStreetMap contributors"}


class FetchError(RuntimeError):
    pass


class TransientError(RuntimeError):
    """Worth retrying: rate limit, gateway timeout, network hiccup, partial result."""


# --------------------------------------------------------------------------- queries
def build_queries(cfg: pc.PipelineConfig) -> dict:
    s, w, n, e = cfg.bbox
    box = f"({s},{w},{n},{e})"
    classes = "|".join(cfg.highway_classes)
    roads = (f'[out:json][timeout:180];\nway["highway"~"^({classes})$"]{box}->.r;\n'
             ".r out body;\nnode(w.r);\nout skel qt;")
    features = ('[out:json][timeout:120];\n(\n  node["place"~"^(city|town|village|hamlet)$"]' + box + ';\n'
                '  nwr["amenity"="hospital"]' + box + ';\n  nwr["healthcare"="hospital"]' + box + ';\n);\nout body center;')
    return {"roads": roads, "features": features}


def queries_hash(queries: dict) -> str:
    return hashlib.sha256("\n--\n".join(queries[k] for k in sorted(queries)).encode()).hexdigest()


# --------------------------------------------------------------------------- HTTP (injectable for tests)
def default_post(url: str, body: bytes, headers: dict, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransientError(f"network error: {exc}") from exc


def _parse_response(status: int, body: bytes) -> dict:
    if status == 400:
        raise FetchError(f"Overpass rejected the query (HTTP 400): {body[:300]!r}")
    if status in (429, 502, 503, 504):
        raise TransientError(f"HTTP {status} (rate limited or overloaded)")
    if status != 200:
        raise TransientError(f"HTTP {status}")
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise TransientError("response was not valid JSON") from exc
    remark = str(data.get("remark", "")) if isinstance(data, dict) else ""
    if "runtime error" in remark or "out of memory" in remark.lower():
        raise TransientError(f"incomplete result: {remark[:200]}")
    if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
        raise TransientError("response has no 'elements' list")
    return data


def fetch_overpass(query: str, endpoints, post=default_post, sleep=time.sleep, attempts: int = 3,
                   timeout_s: float = 200.0, contact: str = "unset") -> tuple[dict, str]:
    headers = {"User-Agent": f"landslide-router/0.1 (hackathon prototype; contact: {contact})",
               "Content-Type": "application/x-www-form-urlencoded"}
    body = urllib.parse.urlencode({"data": query}).encode()
    problems = []
    for url in endpoints:
        for attempt in range(attempts):
            try:
                return _parse_response(*post(url, body, headers, timeout_s)), url
            except TransientError as exc:
                problems.append(f"{url} attempt {attempt + 1}: {exc}")
                if attempt + 1 < attempts:
                    sleep(min(60, 5 * 2 ** attempt))
    raise FetchError("All Overpass endpoints failed:\n  " + "\n  ".join(problems))


# --------------------------------------------------------------------------- cache
def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def fetch_all(cfg: pc.PipelineConfig, force: bool = False, raw_dir: Path = pc.RAW_DIR, post=default_post,
              sleep=time.sleep) -> dict:
    queries = build_queries(cfg)
    qhash = queries_hash(queries)
    meta_path = raw_dir / "fetch_meta.json"
    files = {k: raw_dir / f"{k}.json" for k in queries}
    if meta_path.exists() and all(p.exists() for p in files.values()) and not force:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("query_sha256") == qhash:
            print("Cache is current; nothing fetched (use --force to refetch).")
            return meta
        print("WARNING: cache was built with a different query/bbox. Use --force to refetch.")
        return meta
    endpoints = [e for e in os.environ.get("OVERPASS_ENDPOINTS", "").split(",") if e.strip()] or list(pc.DEFAULT_ENDPOINTS)
    contact = os.environ.get("OVERPASS_CONTACT", "unset")
    if contact == "unset":
        print("Tip: set OVERPASS_CONTACT in .env so the Overpass operators can reach you.")
    results, used, base_ts = {}, None, None
    for i, name in enumerate(sorted(queries)):
        if i:
            sleep(5)  # be polite between requests
        print(f"Fetching {name} from Overpass ...")
        data, used = fetch_overpass(queries[name], endpoints, post=post, sleep=sleep, contact=contact)
        results[name] = data
        base_ts = base_ts or (data.get("osm3s") or {}).get("timestamp_osm_base")
        print(f"  {len(data['elements'])} elements")
    for name, data in results.items():  # write only after BOTH succeeded
        _write_json(files[name], data)
    meta = {**ATTRIBUTION, "retrieved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "osm_base_timestamp": base_ts, "endpoint": used, "query_sha256": qhash, "bbox": list(cfg.bbox),
            "queries": queries, "element_counts": {k: len(v["elements"]) for k, v in results.items()}}
    _write_json(meta_path, meta)
    return meta


# --------------------------------------------------------------------------- summary / curated / report
def summarize(roads: dict, features: dict) -> str:
    refs, hw, places, hosp = Counter(), Counter(), Counter(), 0
    for el in roads.get("elements", []):
        t = el.get("tags") or {}
        if el.get("type") == "way" and t.get("highway"):
            hw[t["highway"]] += 1
            refs[(t.get("ref") or "").strip() or "(no ref)"] += 1
    for el in features.get("elements", []):
        t = el.get("tags") or {}
        if t.get("place"):
            places[t["place"]] += 1
        if t.get("amenity") == "hospital" or t.get("healthcare") == "hospital":
            hosp += 1
    return ("Cached extract:\n"
            f"  ways by highway class: {dict(hw)}\n  top ref values: {refs.most_common(10)}\n"
            f"  place nodes by tag: {dict(places)}\n  hospital features: {hosp}\n"
            "  Check that the NH29 road carries a ref matching the NH29 pattern; otherwise pass --nh-ref-regex.")


def load_curated(directory: Path = pc.CURATED_DIR) -> dict:
    out = {}
    for key in ("verification", "additions", "risk_events", "hospital_reference"):
        p = directory / f"{key}.json"
        out[key] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return out


def render_report_md(ds: dict) -> str:
    r = ds["report"]
    lines = ["# Phase 1 normalization report", "", f"Dataset kind: {ds['meta']['kind']}  ",
             f"Retrieved: {ds['meta']['source'].get('retrieved_at')}  OSM base: {ds['meta']['source'].get('osm_base_timestamp')}", "",
             "## Counts", ""] + [f"- {k}: {v}" for k, v in r["counts"].items()]
    lines += ["", r["note_on_scope_target"], "", "## NH29", "", f"- {r['nh29']}", f"- directionality: {r['directionality']}",
              f"- travel-time basis per segment: {r['travel_time_basis']} (assumed speeds: {r['assumed_speeds_kmh']})",
              "", "## Refs seen (top)", ""] + [f"- {ref}: {n}" for ref, n in r["ways_seen_by_ref_top"]]
    lines += ["", "## Skipped / dropped", ""] + [f"- {k}: {v}" for k, v in r["skipped_or_dropped"].items()]
    lines += [f"- components without NH29 dropped: {len(r['components_without_nh29_dropped'])}"]
    lines += ["", "## Not attached to the road network (NOT in the graph)", ""]
    lines += [f"- place: {u['name']} ({u['type']}, {u['id']}): {u['reason']}, nearest vertex {u['nearest_road_vertex_m']} m"
              for u in r["unattached_places"]] or ["- no places"]
    lines += [f"- hospital: {u['name'] or '(unnamed)'} ({u['id']}): {u['reason']}, nearest vertex {u['nearest_road_vertex_m']} m"
              for u in r["unattached_hospitals"]] or ["- no hospitals"]
    lines += ["", "## Missing data (left unknown, never inferred)", ""] + [f"- {k}: {v}" for k, v in r["missing_data"].items()]
    lines += ["", "## Hospital cross-check against the Nagaland CMHIS empanelled list", "",
              f"- source: {r['hospital_reference_check']['source']}",
              f"- name hints matched: {r['hospital_reference_check']['matched']}",
              f"- NOT found in the graph (add via curated additions after checking coordinates): "
              f"{r['hospital_reference_check']['not_found_in_graph']}",
              "", "## Network", "", f"- reference places: {r['reference_places']}",
              f"- connected components (by place count): {r['network_components_by_place_count']}",
              f"- observed-event place links unresolved: {r['events_places_unresolved']}",
              "", "## Assumptions", ""] + [f"- {a}" for a in r["assumptions"]]
    v = r["validation"]
    lines += ["", f"## Validation: {len(v['errors'])} error(s), {len(v['warnings'])} warning(s)", ""]
    lines += [f"- ERROR {i['code']}: {i['message']}" for i in v["errors"]] + [f"- warning {i['code']}: {i['message']}" for i in v["warnings"]]
    return "\n".join(lines) + "\n"


def run_normalize(cfg: pc.PipelineConfig, raw_dir: Path = pc.RAW_DIR, out_path: Path = pc.NORMALIZED_PATH,
                  curated_dir: Path = pc.CURATED_DIR) -> dict:
    meta_path = raw_dir / "fetch_meta.json"
    if not meta_path.exists():
        raise SystemExit("No cached extract. Run: python data\\fetch_osm.py fetch")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    roads = json.loads((raw_dir / "roads.json").read_text(encoding="utf-8"))
    features = json.loads((raw_dir / "features.json").read_text(encoding="utf-8"))
    ds = normalize(roads, features, meta, cfg, curated=load_curated(curated_dir))
    _write_json(out_path, ds)
    out_path.with_name("report.md").write_text(render_report_md(ds), encoding="utf-8")
    return ds


def main(argv=None) -> int:
    pc.load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["fetch", "normalize", "all", "summary"])
    ap.add_argument("--force", action="store_true", help="refetch even if the cache is current")
    ap.add_argument("--bbox", help="south,west,north,east (changes the query, so refetch)")
    ap.add_argument("--nh-ref-regex", help="regex a way's ref must match to count as NH29")
    ap.add_argument("--no-assumed-speeds", action="store_true", help="leave travel time missing where maxspeed is untagged")
    a = ap.parse_args(argv)
    cfg = pc.PipelineConfig()
    if a.bbox:
        cfg = replace(cfg, bbox=tuple(float(x) for x in a.bbox.split(",")))
    if a.nh_ref_regex:
        cfg = replace(cfg, nh_ref_regex=a.nh_ref_regex)
    if a.no_assumed_speeds:
        cfg = replace(cfg, use_assumed_speeds=False)
    try:
        if a.command in ("fetch", "all"):
            fetch_all(cfg, force=a.force)
        if a.command in ("summary", "fetch", "all"):
            print(summarize(json.loads((pc.RAW_DIR / "roads.json").read_text(encoding="utf-8")),
                            json.loads((pc.RAW_DIR / "features.json").read_text(encoding="utf-8"))))
        if a.command in ("normalize", "all"):
            ds = run_normalize(cfg)
            print(render_report_md(ds))
            print(f"Wrote {pc.NORMALIZED_PATH} and report.md")
            return 1 if ds["report"]["validation"]["errors"] else 0
    except (FetchError, NormalizationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

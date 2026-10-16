"""Randomized cross-check of reroute and find_cut_off_places against an independent oracle (exhaustive simple-path search and a
naive directed BFS written here; no NetworkX, no tool code). Random one-way/two-way networks with parallel segments and
random, overlapping closures. Seeded, so failures reproduce."""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data"))

import scenarios as SC
from test_tools_fake import make

TRIALS, N = 60, 7


def adjacency(segs, blocked):
    adj = {}
    for s in segs:
        if s["id"] in blocked:
            continue
        adj.setdefault(s["a"], []).append((s["b"], s["minutes"], s["id"]))
        if s["two_way"]:
            adj.setdefault(s["b"], []).append((s["a"], s["minutes"], s["id"]))
    return adj


def oracle_best(adj, src, dst):
    """Minimum total minutes over ALL simple directed paths (None if there is none)."""
    best = [None]

    def dfs(node, seen, cost):
        if node == dst:
            best[0] = cost if best[0] is None else min(best[0], cost)
            return
        for nxt, w, _ in adj.get(node, []):
            if nxt not in seen:
                dfs(nxt, seen | {nxt}, cost + w)

    dfs(src, {src}, 0)
    return best[0]


def oracle_reach(adj, src):
    seen, stack = {src}, [src]
    while stack:
        for nxt, _, _ in adj.get(stack.pop(), []):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def random_network(rng):
    names = [f"P{i}" for i in range(N)]
    places = [SC.P("P0", "town", "Dimapur", "dimapur"), SC.P("P6", "town", "Kohima", "kohima")] + \
             [SC.P(n, "village", n) for n in names[1:6]]
    segs = []
    for i in range(rng.randint(8, 12)):
        a, b = rng.sample(names, 2)
        segs.append(SC.S(f"g{i}", a, b, rng.randint(1, 9), 1.0, two_way=rng.random() < 0.75))
    return names, places, segs


def test_reroute_and_cutoff_match_the_oracle_on_random_networks():
    rng = random.Random(20261014)
    checked_routes = checked_cut = changed = 0
    for _ in range(TRIALS):
        names, places, segs = random_network(rng)
        tb = make(SC.make_dataset(places, segs)).tb
        events, blocked = [], set()
        for _k in range(rng.randint(0, 3)):
            s = rng.choice(segs)
            events.append(tb.apply_closure(s["id"], f"closure {_k}")["result"]["event"]["id"])
            blocked.add(s["id"])
        base, now = adjacency(segs, set()), adjacency(segs, blocked)
        for _p in range(10):
            a, b = rng.sample(names, 2)
            r = tb.reroute(a, b)["result"]
            want, want_base = oracle_best(now, a, b), oracle_best(base, a, b)
            if want is None:
                assert r["status"] == "no_route" and r["route"] is None, (a, b, blocked)
            else:
                assert r["status"] == "route_found" and abs(r["route"]["total_travel_time_min"] - want) < 1e-9, (a, b, blocked, want, r["route"])
                used = [s["segment_id"] for s in r["route"]["segments"]]
                assert not (set(used) & blocked), "route used a blocked segment"
                assert abs(sum(s["travel_time_min"] for s in r["route"]["segments"]) - want) < 1e-9
                for st in r["route"]["segments"]:      # every step is a real, correctly directed road
                    seg = next(x for x in segs if x["id"] == st["segment_id"])
                    ok = (st["from"], st["to"]) == (seg["a"], seg["b"]) or (seg["two_way"] and (st["to"], st["from"]) == (seg["a"], seg["b"]))
                    assert ok, (st, seg)
            assert (r["baseline_route"] is None) == (want_base is None)
            if want is not None and want_base is not None:
                assert abs(r["additional_travel_time_min"] - (want - want_base)) < 1e-9
                changed += want != want_base
            checked_routes += 1
        if events:
            c = tb.find_cut_off_places(events[0])["result"]
            got = {i["id"] for g in ("villages", "towns", "junctions", "other") for i in c["newly_unreachable"][g]}
            assert got == oracle_reach(base, "P0") - oracle_reach(now, "P0"), (blocked, got)
            checked_cut += 1
        for e in events:       # clear everything (some are duplicates of the same segment): baseline must return exactly
            tb.clear_closure(e)
        a, b = rng.sample(names, 2)
        r = tb.reroute(a, b)["result"]
        want = oracle_best(base, a, b)
        assert (r["route"] is None) == (want is None)
        assert want is None or abs(r["route"]["total_travel_time_min"] - want) < 1e-9
    assert checked_routes == TRIALS * 10 and checked_cut > 20 and changed > 5, (checked_routes, checked_cut, changed)

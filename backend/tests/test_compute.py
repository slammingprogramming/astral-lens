from __future__ import annotations

import threading
import time

import pytest

from astral.compute.models import defaults_for, licence_preflight, load_registry
from astral.compute.preflight import run_preflight
from astral.compute.probe import Gpu, HostReport
from astral.compute.profiles import PROFILES, select_profile
from astral.compute.residency import (
    AdmissionDeferred, AdmissionRejected, BackendSpec, FootprintCache, ModelSpec, ResidencyManager,
)

GPU = lambda n, mb: BackendSpec(n, "cuda", vram_mb=mb)  # noqa: E731
CPU = lambda n: BackendSpec(n, "cpu", ram_mb=500)  # noqa: E731


def spec(mid, vram, accel="preferred"):
    return ModelSpec(mid, "cap", (GPU(f"{mid}-gpu", vram), CPU(f"{mid}-cpu")), accel)


def mgr(vram=8192, profile="small-gpu", **kw):
    return ResidencyManager(PROFILES[profile], vram, wait_timeout=kw.pop("wait", 0.3), **kw)


class Loader:
    def __init__(self):
        self.loaded, self.unloaded = [], []

    def load(self, b, device):
        self.loaded.append((b.name, device))
        return b.name

    def unload(self, h):
        self.unloaded.append(h)


# --------------------------------------------------------------- residency
def test_small_models_co_reside_but_only_one_large_at_a_time():
    m, ld = mgr(), Loader()
    with m.use(spec("face", 1200), ld.load, ld.unload), m.use(spec("ocr", 900), ld.load, ld.unload):
        assert len(m.resident()) == 2
    with m.use(spec("llm", 5000), ld.load, ld.unload):
        pass
    with m.use(spec("vlm", 3000), ld.load, ld.unload):      # evicts the idle large LLM
        pass
    assert "llm-gpu" in ld.unloaded
    assert sum(1 for r in m.resident() if r.startswith(("llm", "vlm"))) == 1


def test_lru_eviction_frees_the_least_recently_used():
    m, ld = mgr(vram=4096), Loader()
    for name in ("a", "b"):
        with m.use(spec(name, 1500), ld.load, ld.unload):
            pass
    with m.use(spec("a", 1500), ld.load, ld.unload):        # touch a: b is now LRU
        pass
    with m.use(spec("c", 1500), ld.load, ld.unload):
        pass
    assert ld.unloaded == ["b-gpu"]


def test_pinning_prevents_thrash_and_defers_instead_of_oversubscribing():
    m, ld = mgr(vram=4096), Loader()
    with m.use(spec("hot", 3000), ld.load, ld.unload, pin=True):
        pass
    with pytest.raises(AdmissionDeferred):                 # pinned: cannot evict, device busy
        with m.use(spec("other", 2000), ld.load, ld.unload):
            pass
    assert "hot-gpu" not in ld.unloaded
    m.unpin("hot")
    with m.use(spec("other", 2000), ld.load, ld.unload):
        pass
    assert "hot-gpu" in ld.unloaded


def test_in_use_models_are_never_evicted():
    m, ld = mgr(vram=4096), Loader()
    with m.use(spec("busy", 3000), ld.load, ld.unload):
        with pytest.raises(AdmissionDeferred):
            with m.use(spec("x", 2000), ld.load, ld.unload):
                pass


def test_a_model_that_never_fits_falls_back_visibly_or_is_rejected():
    m, ld = mgr(), Loader()
    with m.use(spec("huge", 20000), ld.load, ld.unload) as p:
        assert p.device == "cpu" and p.degraded and p.degraded_from == "huge-gpu"
        assert p.derived_by_fields()["degraded_from"] == "huge-gpu"      # shown in derived_by
    with pytest.raises(AdmissionRejected):
        with m.use(spec("huge", 20000, "required"), ld.load, ld.unload):
            pass


def test_cpu_only_profile_rejects_required_and_annotates_preferred():
    m, ld = mgr(vram=None, profile="cpu-only"), Loader()
    with pytest.raises(AdmissionRejected, match="requires an accelerator"):
        with m.use(spec("x", 100, "required"), ld.load, ld.unload):
            pass
    with m.use(spec("y", 100), ld.load, ld.unload) as p:
        assert p.degraded and p.backend == "y-cpu"
    cpu_only = ModelSpec("z", "cap", (CPU("z-cpu"),), "cpu_only")
    with m.use(cpu_only, ld.load, ld.unload) as p:
        assert not p.degraded


def test_measured_footprint_replaces_the_optimistic_declared_one():
    used = {"v": 0}

    def measure():
        return used["v"]

    ld = Loader()

    def load(b, d):
        used["v"] += 3500                                   # really costs 3500, declared 1000
        return ld.load(b, d)

    fp = FootprintCache()
    m = ResidencyManager(PROFILES["small-gpu"], 8192, footprints=fp, measure=measure,
                         wait_timeout=0.2)
    with m.use(spec("liar", 1000), load, ld.unload):
        pass
    assert fp.get("liar", "liar-gpu", "cuda")[0] == 3500
    assert m.used_mb() == 3500                              # accounting uses the measurement


def test_concurrent_large_models_cannot_both_conclude_they_fit():
    m, ld = mgr(vram=8192, wait=2), Loader()
    inside, peak, lock = [0], [0], threading.Lock()

    def worker(name):
        with m.use(spec(name, 5000), ld.load, ld.unload):
            with lock:
                inside[0] += 1
                peak[0] = max(peak[0], inside[0])
            time.sleep(0.15)
            with lock:
                inside[0] -= 1

    ts = [threading.Thread(target=worker, args=(n,)) for n in ("m1", "m2", "m3")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] == 1                                     # one large model at a time


# ---------------------------------------------------------- profiles/probe
def host(gpus=(), ram=32768):
    return HostReport("t", "cpu", 16, ram, 10**6, list(gpus))


def test_profile_selection_is_explained():
    assert select_profile(host())[0].name == "cpu-only"
    p, why = select_profile(host([Gpu(0, "8 GB consumer GPU", 8192, 8000, "1")]))
    assert p.name == "small-gpu" and "8192" in why[0]
    assert select_profile(host([Gpu(0, "big", 24576, 24000, "1")]))[0].name == "workstation"
    assert select_profile(host([Gpu(i, "h", 81920, 80000, "1") for i in range(2)]))[0].name == "server"


def test_preflight_reports_degradation_honestly():
    rpt = run_preflight(host=host(), deployment_commercial=False)
    text = rpt.render()
    assert rpt.profile.name == "cpu-only"
    assert any("REQUIRED" in u for u in rpt.unavailable)
    assert "Profile  cpu-only" in text and "Container runtime present" in text


# ---------------------------------------------------------------- licences
def test_registry_defaults_are_licence_clean_for_commercial_use():
    reg = load_registry()
    defaults = [m.id for m in reg.values() if m.default]
    blocking = [f for f in licence_preflight(defaults, deployment_commercial=True, registry=reg)
                if f.level == "block-commercial"]
    assert blocking == []                 # no default is a known non-commercial weight


def test_non_commercial_weights_are_flagged_for_commercial_deployments():
    f = licence_preflight(["insightface-buffalo_l", "qwen2.5-vl-3b-instruct", "nllb-200"],
                          deployment_commercial=True)
    assert {x.model_id for x in f if x.level == "block-commercial"} == {
        "insightface-buffalo_l", "qwen2.5-vl-3b-instruct", "nllb-200"}
    # not flagged when the deployment has not declared itself commercial
    assert not [x for x in licence_preflight(["insightface-buffalo_l"], deployment_commercial=False)
                if x.level == "block-commercial"]


def test_unknown_licence_is_never_treated_as_permission():
    f = licence_preflight(["not-in-registry", "pyannote-community-1"], deployment_commercial=True)
    msgs = " ".join(x.message for x in f)
    assert "licence unknown" in msgs and "unconfirmed" in msgs


def test_the_face_defaults_are_the_licence_clean_pair_with_open_caveats():
    assert {m.id for m in defaults_for("face.embed")} == {"sface"}
    sface = load_registry()["sface"]
    assert "UNRESOLVED" in sface.notes            # honest about upstream training-data terms

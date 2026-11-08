#!/usr/bin/env python3
"""Time TeXres against TeX Live on the 100-document corpus.

    python3 scripts/bench100.py --texres /path/to/texres                # everything
    python3 scripts/bench100.py --texres BIN --docs pdf_letter,arxiv_bert --runs 5
    python3 scripts/bench100.py --texres BIN --jobs 12 --resume          # parallel, pinned cores
    python3 scripts/bench100.py --from-json scripts/bench/results/bench100.json   # re-render .md

Corpus:
  * generated: scripts/bench/corpus100/<name>/ (committed, gen_corpus100.py)
  * public:    scripts/bench/corpus_public/<name>/ (fetch_public.py; sources are
               pinned by arXiv version / GitHub commit and sha256, not committed)
Both are listed with engine and main file in their manifests.

Per document: a verification build with both tools (TL's also warms its
$HOME-side caches); outputs must have the same page count and the same
`pdftotext -layout` text modulo whitespace. A document that TeX Live cannot
build, that TeXres cannot build, or whose output differs is NOT timed; it is
kept in the results with status and repro information. Then, for each scenario
(runs alternate between the tools and flip order every run; every run starts
from an identical restored state; --runs >= 5, median reported):
  a  cold full build   TeXres with an empty cache root (except the machine-wide
                       font database below texmf-var, see Tool.fresh_cache) vs
                       latexmk in a clean dir
  b  no-change rebuild same command again after a converged build
  c  one-line edit     BENCH-A -> BENCH-B (generated docs), or one plain-text
                       line of a body file gets " BENCH-B" appended (public docs;
                       the line is chosen automatically and recorded)
  d  single engine pass (optional, --scenarios abcd): TeXres pdflatex/xelatex/
                       lualatex personality vs the TL engine over TL's aux files

Ratio everywhere is TeXres wall time / TeX Live wall time: below 1 means TeXres
is faster. The summary counts documents faster/slower per scenario.

--jobs N runs N documents concurrently, each pinned (taskset) to its own
--cpus-per-job logical CPUs (whole physical cores, SMT siblings included); both
tools of a document share the same CPU set. Verify with --jobs 1 on a few
documents before trusting parallel numbers (see PERFORMANCE.md).
"""

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bench_tl as B  # noqa: E402

BENCH = HERE / "bench"
GEN_DIR = BENCH / "corpus100"
PUB_DIR = BENCH / "corpus_public"
PUB_MANIFEST = BENCH / "public_manifest.json"
SCENARIOS = {"a": "cold build", "b": "no-change rebuild", "c": "one-line edit rebuild", "d": "single engine pass"}
ENGINE_EXE = {"pdf": "pdflatex", "xe": "xelatex", "lua": "lualatex"}
FLAGS = {"pdf": "-pdf", "xe": "-xelatex", "lua": "-lualatex"}
PRINT = threading.Lock()


def log(*a):
    with PRINT:
        print(*a, flush=True)


# ------------------------------------------------------------------- corpus


def load_corpus(need_public=True):
    docs = {}
    gm = json.loads((GEN_DIR / "manifest.json").read_text())
    for n, d in gm.items():
        docs[n] = dict(name=n, engine=d["engine"], src=GEN_DIR / n, main=d["main"], source="generated",
                       tags=d.get("tags", []), note=d.get("note", ""))
    if PUB_MANIFEST.exists():
        for d in json.loads(PUB_MANIFEST.read_text())["docs"]:
            docs[d["name"]] = dict(name=d["name"], engine=d["engine"], src=PUB_DIR / d["name"], main=d["main"],
                                   source=d["kind"], tags=[d.get("field", "")], note="", ref=d.get("id") or f"{d['repo']}@{d['commit'][:10]}")
    return docs


def ensure_public(docs, selected, cache):
    missing = [n for n in selected if docs[n]["source"] != "generated" and not (docs[n]["src"] / docs[n]["main"]).is_file()]
    if missing:
        log(f"fetching {len(missing)} public documents")
        r = subprocess.run([sys.executable, str(BENCH / "fetch_public.py"), "--cache", cache, "--only", ",".join(missing)])
        if r.returncode:
            sys.exit("fetch_public.py failed")


# --------------------------------------------------------------------- CPUs


def cpu_slots(jobs, per_job, allow_unpinned=False):
    """Disjoint CPU lists, whole physical cores (SMT siblings together)."""
    seen, cores = set(), []
    base = Path("/sys/devices/system/cpu")
    for c in sorted(int(p.name[3:]) for p in base.glob("cpu[0-9]*")):
        if c in seen:
            continue
        sib_file = base / f"cpu{c}/topology/thread_siblings_list"
        if not sib_file.exists():
            continue
        lst = []
        for part in sib_file.read_text().strip().split(","):
            a, _, b = part.partition("-")
            lst += list(range(int(a), int(b or a) + 1))
        seen.update(lst)
        cores.append(sorted(lst))
    if not cores:
        return [None] * jobs
    threads = len(cores[0])
    ncores = max(1, per_job // threads)
    if jobs * ncores > len(cores):
        if allow_unpinned:
            return [None] * jobs
        sys.exit(f"--jobs {jobs} x {per_job} CPUs does not fit on {len(cores)} physical cores")
    return [",".join(str(c) for core in cores[k * ncores : (k + 1) * ncores] for c in core) for k in range(jobs)]


# ---------------------------------------------------------------- one tool


def ensure_tl_hyphenation(root: Path) -> dict:
    """If host TeX Live's language.dat lacks non-English hyphenation patterns,
    extract tex/generic/ from the bundled archive and dump xelatex.fmt so TL
    polyglossia/babel runs use the same hyphenation patterns as TeX Live 2026."""
    kpse = B.TL_BIN / "kpsewhich"
    if kpse.exists():
        r = B.sh([str(kpse), "language.dat"])
        ldat = Path(r.stdout.strip()) if r.stdout.strip() else None
        if ldat and ldat.is_file() and any(l.strip().startswith("french") for l in ldat.read_text(errors="ignore").splitlines()):
            return {}
    import glob
    import io
    import tarfile
    assets = HERE.parent / "crates" / "tex-kpse" / "assets"
    parts = sorted(glob.glob(str(assets / "packages.tar.zst.*")))
    supp_path = assets / "packages-supplement.tar.zst"
    if not parts or not supp_path.is_file():
        return {}
    tl_texmf = root / "tl_texmf"
    web2c = tl_texmf / "web2c"
    if not (web2c / "xelatex.fmt").is_file():
        cat = subprocess.Popen(["cat", *parts], stdout=subprocess.PIPE)
        zst = subprocess.Popen(["zstd", "-dc"], stdin=cat.stdout, stdout=subprocess.PIPE)
        with tarfile.open(fileobj=zst.stdout, mode="r|*") as tar:
            for m in tar:
                if m.isfile() and m.name.startswith("tex/generic/"):
                    dst = tl_texmf / m.name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(tar.extractfile(m).read())
        supp = subprocess.run(["zstd", "-dc", str(supp_path)], stdout=subprocess.PIPE, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(supp), mode="r:") as tar:
            for m in tar.getmembers():
                if m.isfile() and m.name.startswith("tex/generic/"):
                    dst = tl_texmf / m.name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(tar.extractfile(m).read())
        web2c.mkdir(parents=True, exist_ok=True)
        env = {**B.make_env(), "TEXINPUTS": f".:{tl_texmf / 'tex'}//:", "TEXFORMATS": f"{web2c}:"}
        xetex = B.TL_BIN / "xetex"
        if xetex.is_file():
            subprocess.run([str(xetex), "-ini", "-interaction=nonstopmode", "-etex", "xelatex.ini"],
                           cwd=web2c, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return {
        "TEXINPUTS": f".:{tl_texmf / 'tex'}//:",
        "LUAINPUTS": f".:{tl_texmf / 'tex'}//:",
        "TEXFORMATS": f"{web2c}:",
    }


class Tool:
    def __init__(self, kind, d, root, texres, bindir, cpus, tl_extra_env=None):
        self.kind, self.d = kind, d
        self.flag = FLAGS[d["engine"]]
        self.engine = ENGINE_EXE[d["engine"]]
        self.base = root / d["name"] / kind
        self.work = self.base / "work"
        self.cwd = self.work / os.path.dirname(d["main"])
        self.main = os.path.basename(d["main"])
        self.cache = self.base / "cache"
        self.snap_work = self.base / "snap_work"
        self.snap_cache = self.base / "snap_cache"
        self.texres, self.bindir, self.cpus = texres, bindir, cpus
        self.env = B.make_env()
        if shutil.which("biber", path=self.env["PATH"]) is None:
            self.env["PATH"] = f"{self.env['PATH']}:{self.bindir}"
        if kind == "texres":
            self.env["TEX_RS_CACHE_DIR"] = str(self.cache)
        elif tl_extra_env:
            self.env.update(tl_extra_env)
        self.env["TMPDIR"] = os.environ.get("TMPDIR", "/tmp")

    def pin(self, cmd):
        return ["taskset", "-c", self.cpus] + cmd if self.cpus else cmd

    def build_cmd(self, pin=True):
        if self.kind == "texres":
            cmd = [str(self.texres), self.flag, self.main]
        else:
            cmd = [str(B.TL_BIN / "latexmk"), self.flag, "-interaction=nonstopmode", self.main]
        return self.pin(cmd) if pin else cmd

    def pass_cmd(self):
        exe = (self.bindir if self.kind == "texres" else B.TL_BIN) / self.engine
        return self.pin([str(exe), "-interaction=nonstopmode", "-halt-on-error", self.main])

    def fresh_cache(self):
        """Empty TeXres's cache root, except `texmf-var`: like TeX Live's
        $HOME/.texliveYYYY/texmf-var (which latexmk runs never clear), it holds
        the machine-wide luaotfload font database and font caches, not state
        of the document. The first build of each document creates it."""
        if self.kind == "texres":
            keep = self.base / "texmf-var.keep"
            shutil.rmtree(keep, ignore_errors=True)
            if (self.cache / "texmf-var").is_dir():
                (self.cache / "texmf-var").rename(keep)
            shutil.rmtree(self.cache, ignore_errors=True)
            self.cache.mkdir(parents=True)
            if keep.is_dir():
                keep.rename(self.cache / "texmf-var")

    def restore_src(self):
        B.restore(self.d["src"], self.work)

    def run(self, cmd, timeout):
        wall, cpu, rc, tail = B.timed(cmd, self.cwd, self.env, timeout)
        return wall, cpu, rc, tail

    def pdf(self):
        return self.cwd / (Path(self.main).stem + ".pdf")


# --------------------------------------------------------- edit selection

SAFE_CMD = re.compile(r"\\(?:cite[a-z]*|ref|eqref|cref|emph|textit|textbf|textsc|texttt)\*?(?:\[[^\]]*\])?\{[^{}]*\}")
QUAL = re.compile(r"^[A-Za-z(][^%&#\\]{59,}[A-Za-z.,)]$")


def plain_line(l):
    """A prose line that can safely get ' BENCH-B' appended."""
    l = l.strip()
    flat = SAFE_CMD.sub("x", l)
    return bool(QUAL.match(flat)) and l.count("$") % 2 == 0 and l.count("{") == l.count("}") and "\\" not in flat
SKIP_ENV = re.compile(r"\\begin\{(verbatim\*?|lstlisting|comment|minted|Verbatim|filecontents\*?|tikzpicture|tabular\*?|equation|align)\}")


def edit_candidates(work: Path, main_rel: str):
    """Plain-text lines of the body files, nearest-the-middle first."""
    files = [work / main_rel]
    others = sorted((p for p in work.rglob("*.tex") if p != work / main_rel), key=lambda p: -p.stat().st_size)
    for grp in (files, others):
        cands = []
        for f in grp:
            try:
                lines = f.read_text(encoding="utf-8").split("\n")
            except (UnicodeDecodeError, OSError):
                continue
            started = f.name != Path(main_rel).name or not any("\\begin{document}" in l for l in lines)
            skip_depth = 0
            for i, l in enumerate(lines):
                if "\\begin{document}" in l:
                    started = True
                if SKIP_ENV.search(l):
                    skip_depth += 1
                if re.search(r"\\end\{(verbatim\*?|lstlisting|comment|minted|Verbatim|filecontents\*?|tikzpicture|tabular\*?|equation|align)\}", l):
                    skip_depth = max(0, skip_depth - 1)
                if started and not skip_depth and plain_line(l):
                    cands.append((f, i, len(lines)))
        if cands:
            mid = len(cands) // 2
            order = sorted(range(len(cands)), key=lambda k: abs(k - mid))
            return [(cands[k][0].relative_to(work).as_posix(), cands[k][1], cands[k][0].read_text(encoding="utf-8").split("\n")[cands[k][1]]) for k in order[:6]]
    return []


def apply_edit(work: Path, edit):
    """edit = {"file", "line", "old", "new"}  (line is 0-based; old must match)."""
    f = work / edit["file"]
    src = f.read_text(encoding="utf-8")
    if edit["line"] is None:
        assert src.count(edit["old"]) == 1, f"{edit['file']}: marker missing"
        f.write_text(src.replace(edit["old"], edit["new"]), encoding="utf-8")
        return
    lines = src.split("\n")
    assert lines[edit["line"]] == edit["old"], f"{edit['file']}:{edit['line'] + 1} changed"
    lines[edit["line"]] = edit["new"]
    f.write_text("\n".join(lines), encoding="utf-8")


def text_has(pdf: Path, needle: str):
    _, t = B.pdf_summary(pdf)
    return needle in re.sub(r"\s+", "", t)


# -------------------------------------------------------------- per document


def first_errors(tail, t):
    """First TeX errors (with context) from the transcript a failed build mentions."""
    m = re.search(r"transcript retained at (\S+)", tail)
    cands = [Path(m.group(1))] if m else []
    cands += [t.pdf().with_suffix(".log")]
    for f in cands:
        if f.is_file():
            lines = f.read_text(errors="replace").split("\n")
            out = []
            for i, l in enumerate(lines):
                if l.startswith("!") or "Fatal" in l[:20]:
                    out.append(" / ".join(x.strip() for x in lines[i : i + 4] if x.strip())[:300])
                if len(out) >= 3:
                    break
            if out:
                return out
    return []


def measure(d, tools, sc, runs, prep_d, edit, timeout, tag):
    out = {k: {"wall": [], "cpu": []} for k in tools}
    kinds = list(tools)
    for i in range(runs):
        for kind in (kinds if i % 2 == 0 else kinds[::-1]):
            t = tools[kind]
            if sc == "a":
                t.restore_src()
                t.fresh_cache()
                cmd = t.build_cmd()
            elif sc in "bc":
                B.restore(t.snap_work, t.work)
                if kind == "texres":
                    B.restore(t.snap_cache, t.cache)
                if sc == "c":
                    apply_edit(t.work, edit)
                cmd = t.build_cmd()
            else:
                B.restore(prep_d, t.work)
                t.fresh_cache()
                cmd = t.pass_cmd()
            wall, cpu, rc, tail = t.run(cmd, timeout)
            if rc != 0:
                raise B.Failed(f"{kind} {d['name']} scenario {sc} run {i + 1}: exit {rc}\n{tail[-800:]}")
            if sc == "c" and i == 0 and not text_has(t.pdf(), "BENCH-B"):
                raise B.Failed(f"{kind} {d['name']}: edit rebuild did not show the edit")
            out[kind]["wall"].append(wall)
            out[kind]["cpu"].append(cpu)
            log(f"    {tag} [{sc}] run {i + 1}/{runs} {kind:6s} wall {wall:8.3f}s cpu {cpu:8.3f}s")
    return out


def do_doc(d, root, texres, bindir, args, cpus, tl_extra_env=None):
    name = d["name"]
    tag = f"{name:30s}"
    entry = {"engine": d["engine"], "source": d["source"], "tags": d["tags"], "note": d["note"], "main": d["main"],
             "ref": d.get("ref", "")}
    tools = {k: Tool(k, d, root, texres, bindir, cpus, tl_extra_env=tl_extra_env) for k in ("texres", "tl")}
    try:
        verify = {}
        # --- TeX Live first: a document TL cannot build is not a TeXres problem
        for kind in ("tl", "texres"):
            t = tools[kind]
            t.restore_src()
            t.fresh_cache()
            wall, cpu, rc, tail = t.run(t.build_cmd(), args.timeout)
            verify[kind + "_wall"] = wall
            log(f"  {tag} verification {kind:6s} {wall:7.2f}s rc={rc}")
            if rc != 0 or not t.pdf().exists():
                entry["status"] = "tl_failed" if kind == "tl" else "texres_failed"
                entry["error_tail"] = tail[-1800:]
                entry["error_log"] = first_errors(tail, t)
                entry["verify"] = verify
                return entry
            B.restore(t.work, t.snap_work)
            if kind == "texres":
                B.restore(t.cache, t.snap_cache)
        vf = B.compare_pdfs(tools["tl"].pdf(), tools["texres"].pdf(), visual_dpi=args.visual_dpi)
        vf.update(verify)
        text_ok = vf["text_equal"] if args.min_text_similarity >= 1.0 else vf["text_similarity"] >= args.min_text_similarity
        vis_ok = True if args.visual_dpi <= 0 else (
            vf.get("visual_iou", 1.0) >= args.min_visual_iou
            and vf.get("similarity", 1.0) >= args.min_similarity
        )
        vf["equivalent"] = vf["pages_equal"] and text_ok and vis_ok
        entry["verify"] = vf
        entry["pages"] = vf["pages_tl"]
        if not vf["equivalent"]:
            entry["status"] = "differs"
            log(
                f"  {tag} DIFFERS: pages TL {vf['pages_tl']} TeXres {vf['pages_texres']} "
                f"text_sim={vf.get('text_similarity', 0):.4f} vis_iou={vf.get('visual_iou', 1.0):.4f}; "
                f"{json.dumps(vf.get('first_difference'))[:200]}"
            )
            return entry
        # --- choose the edit (generated docs: the marker; public docs: a plain-text line TL shows)
        edit = None
        if "c" in args.scenarios:
            main_text = (tools["tl"].snap_work / d["main"]).read_text(encoding="utf-8", errors="replace")
            if "BENCH-A" in main_text:
                edit = {"file": d["main"], "line": None, "old": "BENCH-A", "new": "BENCH-B"}
            else:
                for f, ln, old in edit_candidates(tools["tl"].snap_work, d["main"]):
                    cand = {"file": f, "line": ln, "old": old, "new": old + " BENCH-B"}
                    probe = root / name / "probe"
                    B.restore(tools["tl"].snap_work, probe)
                    apply_edit(probe, cand)
                    t = tools["tl"]
                    _, _, rc, _ = B.timed(t.build_cmd(pin=False), probe / os.path.dirname(d["main"]), t.env, args.timeout)
                    pdf = probe / os.path.dirname(d["main"]) / (Path(d["main"]).stem + ".pdf")
                    ok = rc == 0 and pdf.exists() and text_has(pdf, "BENCH-B")
                    shutil.rmtree(probe, ignore_errors=True)
                    if ok:
                        edit = cand
                        break
            if edit is None:
                entry["status"] = "no_edit_point"
                return entry
            entry["edit"] = {k: edit[k] for k in ("file", "line")} if edit["line"] is not None else {"file": edit["file"], "marker": "BENCH-A"}
        prep_d = None
        if "d" in args.scenarios:
            prep_d = root / name / "prep_d"
            B.restore(tools["tl"].snap_work, prep_d)
            for f in list(prep_d.rglob("*")):
                if f.is_file() and B.PRODUCT_RE.search(f.name):
                    f.unlink()
        entry["timings"] = {}
        for sc in args.scenarios:
            log(f"  {tag} scenario {sc}: {SCENARIOS[sc]}")
            raw = measure(d, tools, sc, args.runs, prep_d, edit, args.timeout, tag)
            entry["timings"][sc] = {
                k: {"wall": B.summarize(v["wall"]), "cpu": B.summarize(v["cpu"]), "wall_runs": v["wall"], "cpu_runs": v["cpu"]}
                for k, v in raw.items()
            }
            entry["timings"][sc]["loadavg_1m"] = os.getloadavg()[0]
        entry["status"] = "timed"
    except B.Failed as e:
        entry["status"] = "run_failed"
        entry["error_tail"] = str(e)[-1800:]
    finally:
        if not args.keep:
            shutil.rmtree(root / name, ignore_errors=True)
    return entry


# -------------------------------------------------------------------- output


def ratio(e, sc):
    t = e.get("timings", {}).get(sc)
    return t["texres"]["wall"]["median"] / t["tl"]["wall"]["median"] if t else None


def render_md(res):
    env = res["environment"]
    docs = res["documents"]
    scs = [s for s in SCENARIOS if s in res["scenarios_run"]]
    timed = {n: e for n, e in docs.items() if e["status"] == "timed"}
    L = ["# TeXres vs TeX Live: 100-document benchmark\n"]
    L.append(f"Run {env['date']}; {res['runs']} timed runs per cell (median), tools alternating; "
             f"jobs={res.get('jobs', 1)}, CPUs/job={res.get('cpus_per_job', 'unpinned')}.\n")
    L.append("Ratio = TeXres wall time / TeX Live wall time (below 1: TeXres faster).\n")
    L.append("## Environment\n")
    L.append("| | |\n| --- | --- |")
    L.append(f"| CPU | {env['cpu_model']} ({env['logical_cpus']} logical) |")
    L.append(f"| RAM / OS | {env['ram_gib']} GiB; {env['os']}, kernel {env['kernel']}; governor `{env['cpu_governor']}` |")
    L.append(f"| TeXres | {env['texres_version']} (sha256 `{env['texres_sha256'][:16]}…`){'; ' + res['texres_note'] if res.get('texres_note') else ''} |")
    L.append(f"| TeX Live | {env['pdflatex']}; {env['xelatex']}; {env['lualatex']}; {env['latexmk']}; {env['biber']} |")
    L.append(f"| Load average | start {env['loadavg_at_start'][0]:.1f}, end {env['loadavg_at_end'][0]:.1f} |")
    L.append("")
    # composition
    L.append("## Corpus composition\n")
    L.append("| Group | Documents |\n| --- | ---: |")
    comp = {}
    for n, e in docs.items():
        comp[(e["source"], e["engine"])] = comp.get((e["source"], e["engine"]), 0) + 1
    for (s, g), c in sorted(comp.items()):
        L.append(f"| {s}, {g} | {c} |")
    L.append(f"| **total** | {len(docs)} |\n")
    # status
    stc = {}
    for e in docs.values():
        stc[e["status"]] = stc.get(e["status"], 0) + 1
    L.append("## Status\n")
    L.append("| Status | Documents |\n| --- | ---: |")
    for k, v in sorted(stc.items()):
        L.append(f"| {k} | {v} |")
    L.append("")
    # parity & similarity summary
    verified = [e["verify"] for e in docs.values() if "verify" in e and "pages_equal" in e["verify"]]
    if verified:
        pages_eq = sum(1 for v in verified if v.get("pages_equal"))
        text_eq = sum(1 for v in verified if v.get("text_equal"))
        text_sims = [v.get("text_similarity", 1.0 if v.get("text_equal") else 0.0) for v in verified]
        sims = [v.get("similarity", v.get("text_similarity", 1.0 if v.get("text_equal") else 0.0)) for v in verified]
        vis_ious = [v["visual_iou"] for v in verified if "visual_iou" in v]
        L.append("## TeX Live Parity & Similarity\n")
        L.append("| Metric | Value |\n| --- | ---: |")
        L.append(f"| Page-count parity | {pages_eq} / {len(docs)} |")
        L.append(f"| Exact text parity (`pdftotext -layout`) | {text_eq} / {len(docs)} |")
        L.append(f"| Text similarity (min / median / mean) | {min(text_sims):.4f} / {statistics.median(text_sims):.4f} / {statistics.mean(text_sims):.4f} |")
        if vis_ious:
            L.append(f"| Visual ink IoU (min / median / mean) | {min(vis_ious):.4f} / {statistics.median(vis_ious):.4f} / {statistics.mean(vis_ious):.4f} |")
        L.append(f"| Composite similarity (min / median / mean) | {min(sims):.4f} / {statistics.median(sims):.4f} / {statistics.mean(sims):.4f} |")
        if "gate" in res:
            g = res["gate"]
            L.append(f"| Release gate | **{'PASS' if g['passed'] else 'FAIL'}** (min_similarity >= {g['min_similarity']:.3f}, min_text_similarity >= {g['min_text_similarity']:.3f}, max_median_wall_ratio <= {g['max_median_wall_ratio']:.2f}) |")
        L.append("")
    # summary
    L.append("## Summary: documents where TeXres is faster / slower\n")
    L.append("| Scenario | TeXres faster | TeXres slower | Median ratio | Worst ratio |\n| --- | ---: | ---: | ---: | ---: |")
    for s in scs:
        rs = [(ratio(e, s), n) for n, e in timed.items() if ratio(e, s) is not None]
        if not rs:
            continue
        faster = sum(1 for r, _ in rs if r < 1)
        L.append(f"| ({s}) {SCENARIOS[s]} | {faster} | {len(rs) - faster} | {statistics.median(r for r, _ in rs):.2f} | {max(rs)[0]:.2f} ({max(rs)[1]}) |")
    L.append("")
    # table slowest-first by the worst scenario ratio
    def worst(e):
        return max(((ratio(e, s) or 0) for s in scs), default=0)
    L.append("## All timed documents, slowest first (by worst ratio over scenarios)\n")
    head = "| Document | Engine | Pages | " + " | ".join(f"({s}) TeXres / TL s | ratio" for s in scs) + " |"
    L.append(head)
    L.append("| --- | --- | ---: | " + " | ".join("---: | ---:" for _ in scs) + " |")
    for n, e in sorted(timed.items(), key=lambda kv: -worst(kv[1])):
        cells = []
        for s in scs:
            t = e["timings"][s]
            a, b = t["texres"]["wall"]["median"], t["tl"]["wall"]["median"]
            cells.append(f"{B.fmt_t(a)} / {B.fmt_t(b)} | {a / b:.2f}")
        L.append(f"| {n} | {e['engine']} | {e['pages']} | " + " | ".join(cells) + " |")
    L.append("")
    # problems
    prob = [(n, e) for n, e in docs.items() if e["status"] != "timed"]
    slower = [(n, e) for n, e in sorted(timed.items(), key=lambda kv: -worst(kv[1])) if worst(e) >= 1]
    L.append("## Documents where TeXres is slower in at least one scenario\n")
    if slower:
        L.append("| Document | " + " | ".join(f"({s})" for s in scs) + " |\n| --- | " + " | ".join("---:" for _ in scs) + " |")
        for n, e in slower:
            L.append(f"| {n} | " + " | ".join(f"{ratio(e, s):.2f}" for s in scs) + " |")
    else:
        L.append("None.")
    L.append("")
    L.append("## Documents that failed, differ, or could not be timed\n")
    if prob:
        L.append("| Document | Engine | Status | Detail |\n| --- | --- | --- | --- |")
        for n, e in sorted(prob):
            if e["status"] == "differs":
                v = e["verify"]
                det = f"pages TL {v['pages_tl']} / TeXres {v['pages_texres']}; differing pages {v.get('differing_pages', [])[:8]}"
                if "first_difference" in v:
                    det += f"; TL `…{v['first_difference']['tl']}…` vs TeXres `…{v['first_difference']['texres']}…`"
            else:
                if e.get("error_log"):
                    det = " ⏎ ".join(e["error_log"])
                else:
                    det = (e.get("error_tail", "") or "").strip().splitlines()[-3:]
                    det = " ⏎ ".join(x.strip() for x in det)
            L.append(f"| {n} | {e['engine']} | {e['status']} | {det.replace('|', '\\|')[:420]} |")
    else:
        L.append("None.")
    L.append("")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------- main


def evaluate_gate(res, args) -> dict:
    docs = res["documents"]
    total = len(docs)
    timed = {n: e for n, e in docs.items() if e["status"] == "timed"}
    pass_rate = (len(timed) / total) if total else 0.0
    verified = [e["verify"] for e in docs.values() if "verify" in e and "pages_equal" in e["verify"]]
    min_text_sim = min((v.get("text_similarity", 0.0) for v in verified), default=0.0) if len(verified) == total else 0.0
    min_sim = min((v.get("similarity", 0.0) for v in verified), default=0.0) if len(verified) == total else 0.0
    vis_ious = [v["visual_iou"] for v in verified if "visual_iou" in v]
    min_vis_iou = min(vis_ious) if vis_ious else 1.0
    reasons = []
    if pass_rate < args.min_pass_rate:
        failed_names = sorted(n for n, e in docs.items() if e["status"] != "timed")
        reasons.append(f"pass rate {len(timed)}/{total} ({pass_rate:.3f}) < {args.min_pass_rate:.3f} (failed: {', '.join(failed_names[:10])})")
    if min_text_sim < args.min_text_similarity:
        reasons.append(f"min text_similarity {min_text_sim:.4f} < {args.min_text_similarity:.4f}")
    if min_sim < args.min_similarity:
        reasons.append(f"min similarity {min_sim:.4f} < {args.min_similarity:.4f}")
    if args.visual_dpi > 0 and min_vis_iou < args.min_visual_iou:
        reasons.append(f"min visual_iou {min_vis_iou:.4f} < {args.min_visual_iou:.4f}")
    scenario_medians = {}
    for s in res["scenarios_run"]:
        rs = [ratio(e, s) for e in timed.values() if ratio(e, s) is not None]
        if rs:
            med = statistics.median(rs)
            scenario_medians[s] = round(med, 4)
            if med > args.max_median_wall_ratio:
                reasons.append(f"scenario ({s}) median wall ratio {med:.3f} > {args.max_median_wall_ratio:.3f}")
    return {
        "passed": len(reasons) == 0,
        "total_docs": total,
        "passed_docs": len(timed),
        "pass_rate": round(pass_rate, 4),
        "min_text_similarity": args.min_text_similarity,
        "observed_min_text_similarity": round(min_text_sim, 6),
        "min_similarity": args.min_similarity,
        "observed_min_similarity": round(min_sim, 6),
        "min_visual_iou": args.min_visual_iou,
        "observed_min_visual_iou": round(min_vis_iou, 4),
        "max_median_wall_ratio": args.max_median_wall_ratio,
        "scenario_median_ratios": scenario_medians,
        "reasons": reasons,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--texres", help="texres executable to measure (copy it somewhere stable first)")
    ap.add_argument("--texres-note", default="", help="free text recorded in the results (e.g. commit)")
    ap.add_argument("--from-json")
    ap.add_argument("--runs", type=int, default=None)
    ap.add_argument("--docs", default="all", help="comma-separated names, or tag=<tag>, engine=pdf|xe|lua, source=generated|arxiv|github")
    ap.add_argument("--scenarios", default=None)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--cpus-per-job", type=int, default=4)
    ap.add_argument("--no-pin", action="store_true", help="do not pin workers with taskset")
    ap.add_argument("--timeout", type=int, default=900, help="per command, seconds")
    ap.add_argument("--out-dir", default=str(BENCH / "results"))
    ap.add_argument("--name", default=None, help="result file stem")
    ap.add_argument("--workdir")
    ap.add_argument("--cache", default=os.environ.get("BENCH_CACHE", str(Path.home() / ".cache" / "texres-bench")))
    ap.add_argument("--resume", action="store_true", help="reuse per-document results for the same binary in <out-dir>/<name>_docs/")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--gate", action="store_true", help="gate build/release on TeX Live parity, similarity threshold, and wall-time ratio")
    ap.add_argument("--visual-dpi", type=int, default=None, help="render DPI for visual page similarity (default: 100 in --gate, 0 otherwise)")
    ap.add_argument("--min-similarity", type=float, default=0.980, help="minimum composite similarity per document (default: 0.980)")
    ap.add_argument("--min-text-similarity", type=float, default=None, help="minimum text similarity per document (default: 0.995 in --gate, 1.0 otherwise)")
    ap.add_argument("--min-visual-iou", type=float, default=0.70, help="minimum rendered ink IoU per document when --visual-dpi > 0 (default: 0.70)")
    ap.add_argument("--min-pass-rate", type=float, default=1.0, help="minimum fraction of documents passing parity + timing (default: 1.0)")
    ap.add_argument("--max-median-wall-ratio", type=float, default=1.0, help="maximum median TeXres/TeX Live wall-time ratio per scenario (default: 1.0)")
    args = ap.parse_args()
    if args.from_json:
        jp = Path(args.from_json)
        jp.with_suffix(".md").write_text(render_md(json.loads(jp.read_text())))
        return 0
    if args.runs is None:
        args.runs = 1 if args.gate else 5
    if args.scenarios is None:
        args.scenarios = "ab" if args.gate else "abc"
    if args.visual_dpi is None:
        args.visual_dpi = 100 if args.gate else 0
    if args.min_text_similarity is None:
        args.min_text_similarity = 0.995 if args.gate else 1.0
    if args.name is None:
        args.name = "parity100" if args.gate else "bench100"
    if not args.texres:
        ap.error("--texres is required")
    if args.runs < 1 or (not args.gate and args.runs < 5):
        ap.error("--runs must be at least 5 (or >= 1 with --gate)")
    texres = Path(args.texres).resolve()
    if not texres.is_file():
        ap.error(f"{texres} not found")
    docs = load_corpus()
    if args.docs == "all":
        sel = sorted(docs)
    else:
        sel = []
        for tok in args.docs.split(","):
            if "=" in tok:
                k, v = tok.split("=", 1)
                sel += [n for n, d in docs.items() if (k == "engine" and d["engine"] == v) or (k == "source" and d["source"] == v) or (k == "tag" and v in d["tags"])]
            elif tok in docs:
                sel.append(tok)
            else:
                ap.error(f"unknown document {tok}")
        sel = sorted(set(sel))
    ensure_public(docs, sel, args.cache)
    for tool in ("pdflatex", "xelatex", "lualatex", "latexmk", "bibtex"):
        if not (B.TL_BIN / tool).exists():
            ap.error(f"{B.TL_BIN / tool} missing")
    root = Path(args.workdir or tempfile.mkdtemp(prefix="texres-bench100-", dir=os.environ.get("TMPDIR"))).resolve()
    root.mkdir(parents=True, exist_ok=True)
    bindir = root / "bin"
    bindir.mkdir(exist_ok=True)
    for name in ("pdflatex", "xelatex", "lualatex", "biber"):
        link = bindir / name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(texres)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    per_doc = out / f"{args.name}_docs"
    per_doc.mkdir(exist_ok=True)
    sha = hashlib.sha256(texres.read_bytes()).hexdigest()
    slots = ([None] * args.jobs) if (args.jobs <= 1 or args.no_pin) else cpu_slots(args.jobs, args.cpus_per_job, allow_unpinned=args.gate)

    tl_extra_env = ensure_tl_hyphenation(root)
    # warm TL's $HOME-side caches (luaotfload database, fontconfig) once, serially
    warm = root / "warm"
    warm.mkdir(exist_ok=True)
    for eng in ("pdf", "xe", "lua"):
        (warm / "w.tex").write_text("\\documentclass{article}\\usepackage{fontspec}\\begin{document}warm\\end{document}\n" if eng != "pdf" else "\\documentclass{article}\\begin{document}warm\\end{document}\n")
        B.timed([str(B.TL_BIN / ENGINE_EXE[eng]), "-interaction=nonstopmode", "w.tex"], warm, {**B.make_env(), **tl_extra_env}, 600)

    res = {"environment": B.environment(texres), "runs": args.runs, "scenarios_run": list(args.scenarios), "documents": {},
           "jobs": args.jobs, "cpus_per_job": args.cpus_per_job if (args.jobs > 1 and any(slots)) else None, "texres_note": args.texres_note}
    todo = []
    for n in sel:
        pj = per_doc / f"{n}.json"
        if args.resume and pj.exists():
            old = json.loads(pj.read_text())
            if (
                old.get("texres_sha256") == sha
                and old.get("scenarios") == args.scenarios
                and old.get("runs", 0) >= args.runs
                and old.get("visual_dpi", 0) == args.visual_dpi
            ):
                res["documents"][n] = old["entry"]
                log(f"resume {n}: {old['entry']['status']}")
                continue
        todo.append(n)
    free = list(range(len(slots)))
    lock = threading.Lock()

    def worker(n):
        with lock:
            slot = free.pop()
        try:
            t0 = time.time()
            e = do_doc(docs[n], root, texres, bindir, args, slots[slot], tl_extra_env=tl_extra_env)
            log(f"== {n}: {e['status']} ({time.time() - t0:.0f}s)")
            (per_doc / f"{n}.json").write_text(json.dumps({
                "texres_sha256": sha,
                "scenarios": args.scenarios,
                "runs": args.runs,
                "visual_dpi": args.visual_dpi,
                "entry": e,
            }, indent=1))
            return n, e
        finally:
            with lock:
                free.append(slot)

    with cf.ThreadPoolExecutor(args.jobs) as ex:
        for n, e in ex.map(worker, todo):
            res["documents"][n] = e
    res["documents"] = {n: res["documents"][n] for n in sel}
    res["environment"]["loadavg_at_end"] = os.getloadavg()
    gate = evaluate_gate(res, args)
    res["gate"] = gate
    (out / f"{args.name}.json").write_text(json.dumps(res, indent=1) + "\n")
    (out / f"{args.name}.md").write_text(render_md(res))
    log(f"wrote {out / (args.name + '.json')} and .md")
    if args.gate:
        status_str = "PASS" if gate["passed"] else "FAIL"
        log(
            f"[GATE {status_str}] {gate['passed_docs']}/{gate['total_docs']} docs passed; "
            f"min_text_similarity={gate['observed_min_text_similarity']:.4f} (>= {gate['min_text_similarity']:.4f}); "
            f"min_visual_iou={gate['observed_min_visual_iou']:.4f} (>= {gate['min_visual_iou']:.4f}); "
            f"min_similarity={gate['observed_min_similarity']:.4f} (>= {gate['min_similarity']:.4f}); "
            f"median_wall_ratios={gate['scenario_median_ratios']} (<= {gate['max_median_wall_ratio']:.2f})"
        )
        for reason in gate["reasons"]:
            log(f"  ! {reason}")
    if not args.keep and not args.workdir:
        shutil.rmtree(root, ignore_errors=True)
    return 0 if (not args.gate or gate["passed"]) else 1

if __name__ == "__main__":
    sys.exit(main())

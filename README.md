# SafeDSE experiments

Scripts, inputs and raw results of the experiments in

> F. Bahrami and S.-H. Attarzadeh-Niaki, "SafeDSE: Joint Exploration of Safety
> Architecture Patterns, Mapping, and Scheduling for Dataflow Applications With
> Safety Arguments," manuscript, 2026,

and in its extended technical report. The tool itself is
[SafeDSE](https://github.com/SBU-CPS-Lab/SafeDSE), included here as the git
submodule `safedse/` at the version the results were checked with.

You can inspect the raw results without running anything (`results/`), turn
them into the macros, tables and plot data of the paper (`generated/`) in
about a minute, or re-run any experiment.

## Repository layout

| Path | Content |
|---|---|
| `safedse/` | SafeDSE (submodule) |
| `paths.py` | locations shared by the scripts; `SAFEDSE` overrides the submodule path |
| `harness.py` | RQ1, RQ2 (joint vs. decoupled flows and all their variants), RQ3, RQ4 |
| `gen_synthetic.py` | RQ3 synthetic instances from SDF3's random graph generator |
| `gsn_stats.py` | RQ4 argument statistics; keeps solution, check log and argument in `results/gsn/` |
| `breadth.py` | price sweep, communication variants, three-application instance, core-bounded pattern instances, tool timing, flattening cost, other solvers, exact cross-checks of the three-application rows |
| `certify_pf.py` | solver-independent cycle bound for every infeasible pattern-first row |
| `exact_period.py` | least period of a returned design (design fixed, period minimised) |
| `exact_model.py`, `exact/`, `patches/` | the exact-period model variant B: `exact/` is SafeDSE's model with `patches/exact_period_variant_b.patch` ([patches/README.md](patches/README.md)) |
| `exact_check.py` | re-solves every RQ2/RQ4 row with variant B |
| `mutation.py`, `verifier_v1/`, `verifier_ext/`, `wrappers/fix_all.mzn` | mutation analysis of the verifier and the argument generator |
| `modelext_build.py`, `modelext/`, `modelext.py` | evaluation of three model extensions (pattern-dependent doer SIL, priced development multipliers, verdict tokens as variables) in a model copy |
| `desyde_cmp.py`, `desyde/` | RQ1 head-to-head with DeSyDe (build script, build patch, the TODAES inputs) |
| `wrappers/` | wrapper models (`include "dse.mzn";` plus constraints) for the baselines and the pin step |
| `variants/`, `multi/`, `synthetic/` | generated input variants, the three-application instance, the RQ3 graphs and instances |
| `make_macros.py`, `make_catalogue_table.py`, `make_gsn_figure.py`, `make_report.py` | `results/` to `generated/` |
| `PARAMETERS.md` | the source of every input parameter |
| `results/` | raw results (see below) |
| `generated/paper/`, `generated/report/` | macros, plot data, tables and GSN figure sources of the paper and the report, generated from `results/` |

## Setup

```bash
git clone --recurse-submodules https://github.com/SBU-CPS-Lab/SafeDSE-experiments.git
cd SafeDSE-experiments
pip install pyyaml
```

You need MiniZinc 2.10 with OR-Tools CP-SAT (see
[safedse/docs/usage.md](https://github.com/SBU-CPS-Lab/SafeDSE/blob/main/docs/usage.md);
`safedse/bootstrap_minizinc.sh` installs the bundle) and Python 3.9 or later.
The results were produced with MiniZinc 2.10.1, CP-SAT 9.15 and Python 3.14
([results/env.txt](results/env.txt)). Build the SafeDSE instances once:

```bash
safedse/tools/build_all.sh
python3 safedse/tests/run_tests.py gsn patterns     # optional: a quick check of the tool
```

Optional, only for the experiments that need them:

- **DeSyDe** (RQ1 head-to-head): `desyde/build.sh` clones DeSyDe at tag
  `v0.2.1-todaes`, applies `desyde/desyde-build.patch` (compile fixes for a
  current compiler and boost, and the initialization of `Mapping::sysConstr`)
  and builds it against Gecode 4.4.0 with Gist in `/usr/local`; see the
  comments in the script.
- **SDF3** (new RQ3 graphs only): the stored graphs in `synthetic/graphs/` are
  reused, so the generator is needed only for new sweep points. Set
  `SDF3_BIN` to `sdf3generate-sdf` of an SDF3 build.

## Regenerate the paper inputs from the stored results

No solver runs; about a minute:

```bash
python3 make_macros.py               # generated/paper/results-macros.tex, data/*.csv, tables/tab-results.tex
python3 make_catalogue_table.py      # generated/paper/tables/tab-catalogue.tex
python3 make_gsn_figure.py           # generated/paper/figures/fig-gsn.tex
python3 make_gsn_figure.py --actor compJah --out generated/report/figures/fig-gsn-compjah.tex
python3 make_report.py               # generated/report/tables/*.tex, data/*.csv
```

`make_macros.py` prints a WARNING for every row it leaves out (timeouts,
unverified rows, disagreeing repetitions). Every number of the paper is a
macro in `results-macros.tex`; no number is typed by hand.

## Re-run the experiments

Every script writes one CSV row per finished run (append and flush) and skips
rows already present, so it can be interrupted and resumed; delete a CSV, or
its rows, to re-run them. Long runs are best started detached
(`nohup python3 harness.py rq3 > rq3.log 2>&1 &`). Scratch files go to
`results/tmp/` (not tracked). Times are rough wall-clock figures on the
machine of `results/env.txt`.

| Experiment | Command | Output | Paper | Time |
|---|---|---|---|---|
| RQ1: Rosvall's benchmark, published bounds | `python3 harness.py rq1` | `e1_parity.csv` | RQ1, report | minutes |
| RQ1: head-to-head with DeSyDe | `python3 desyde_cmp.py safedse desyde` | `e7_safedse.csv`, `e7_desyde.csv`, `results/desyde/` | RQ1 | up to 1 h per scenario and tool |
| RQ2: joint vs. decoupled flows, no bound | `python3 harness.py rq2` | `e2_joint.csv` | RQ2, Table III | minutes |
| RQ2: period bounds 1.00-2.00 x mu* | `python3 harness.py rq2tight` | `e2_tight.csv` | RQ2, Fig. 5 | hours |
| RQ2: pattern-first choices, least period per flow, input variants, repetitions | `python3 harness.py pftable rq2thr rq2var rq2reps` | `e2_pfchoice.csv`, `e2_thr.csv`, `e2_var.csv`, `e2_reps.csv` | RQ2, Table III | hours |
| RQ2: certificates of pattern-first infeasibility | `python3 certify_pf.py` | `e2_cert.csv` | RQ2 | seconds |
| RQ2/RQ4: exact cross-check (variant B) | `python3 exact_check.py` | `e2_exact.csv`, `e2_exact_sl.csv` | RQ2 | about 12 h of solver time |
| RQ3: scalability (actor count, catalogue size) | `python3 harness.py rq3` | `e3_scal.csv`, `synthetic/` | RQ3, Fig. 6 | about 4.5 h |
| RQ4: cost profiles, fault models, communication, NVP | `python3 harness.py rq4` | `e4_sens.csv` | RQ4, Table III | minutes |
| RQ4: argument statistics | `python3 gsn_stats.py` (`--regen` re-argues the kept solutions) | `e4_gsn.csv`, `results/gsn/` | RQ4, Fig. 4 | minutes |
| RQ4 and breadth: price sweep, communication variants, three applications, core-bounded patterns, tool timing, flattening, other solvers | `python3 breadth.py price comm multi patcons pipe flat xsolver` | `e4_price.csv`, `e4_comm.csv`, `e5_multi.csv`, `e4_patcons.csv`, `e4_pipe.csv`, `e3_flat.csv`, `e4_xsolver.csv` | RQ3, RQ4, Fig. 7, Table III | about 3 h |
| exact cross-check of the bounded three-application rows | `python3 breadth.py multiexact multiexactlong` | `e5_multi_exact.csv`, `e5_multi_exact_long.csv` | RQ2 | about 1.5 h, plus up to 1 h per long row |
| mutation analysis | `python3 mutation.py base solution model generator oracle` | `e6_mut.csv`, `e6_oracle.csv`, `results/mut/` | RQ4 | hours |
| mutation analysis, current verifier | `python3 mutation.py solution model_reverify generator --verifiers artifact` | `e6_mut.csv` | RQ4 | minutes |
| false alarms of the verifier extension | `python3 mutation.py recheck` | `e6_recheck.csv` | report | hours |
| model extensions | `python3 modelext_build.py; python3 modelext.py validate; python3 modelext.py run` | `e8_modelext.csv` | report | about 3 h |

Notes:

- **Verification.** Every solution passes SafeDSE's independent verifier
  (`tools/verify.py`); a row without `verify_ok=True` is recorded but never
  becomes a number. Cost solutions are first pinned to their least period
  (`exact_period.py`), since with 4 CP-SAT workers the reported period of a
  cost optimum can lie anywhere between the design's least period and the
  bound.
- **Nondeterminism.** CP-SAT with 4 workers is a parallel portfolio:
  objective values and UNSAT verdicts reproduce, run times vary, and among
  equal-cost optima the returned design (and its period) can differ between
  runs. The SL baseline therefore uses a canonical step-1 optimum
  (`harness.sl_step1`).
- **Order.** `certify_pf.py` and `exact_check.py` read the RQ2 CSVs; `make_*`
  read all of them. `mutation.py recheck` re-verifies the solutions that the
  other runs left in `results/tmp/`, which is not part of this repository.
- **Model copies.** `exact/` and `modelext/` are generated from the SafeDSE
  submodule by `exact_model.py` and `modelext_build.py`; SafeDSE itself is
  never changed. `verifier_v1/` and `verifier_ext/` are earlier and extended
  versions of SafeDSE's verifier, kept so that the mutation-analysis rows of
  each verifier stay reproducible (tags in `mutation.py`).

## Results

`results/` holds the raw data behind every number, table and figure:

| File | Content |
|---|---|
| `e1_*.csv`, `e7_*.csv`, `desyde/` | RQ1 |
| `e2_*.csv` | RQ2: one row per instance, flow (joint, SL, PF-min, PF-max, PF-light, PF-cheap, NP), bound, variant or repetition |
| `e3_scal.csv`, `e3_flat.csv` | RQ3 |
| `e4_*.csv`, `e5_*.csv`, `gsn/` | RQ4 and breadth; `gsn/` has the solution, check log and argument of each argued instance |
| `e6_*.csv`, `mut/` | mutation analysis; `mut/base/` the verified base optima, `mut/model/` the solutions of the model mutants |
| `e8_modelext.csv` | model extensions |
| `env.txt` | machine, MiniZinc, solvers, Python, SafeDSE and DeSyDe versions |

Columns are named after the solver output (`status`, `seconds`,
`total_cost`, `mu_max`, `nprocs`, `hw_cost`, `dev_cost`, `promotion_cost`,
`verify_ok`, ...); each script's field list (`*_FIELDS`) documents its CSV.
`e2_exact_sl.csv` uses the cause `period-pessimism` for a difference that
comes from the model's conservative period
([SafeDSE docs](https://github.com/SBU-CPS-Lab/SafeDSE/blob/main/docs/design.md#known-limitations)).

## License

BSD 3-Clause, see [LICENSE](LICENSE). The DeSyDe inputs in `desyde/todaes/`
are distributed under DeSyDe's BSD 2-Clause license; see [NOTICE](NOTICE).

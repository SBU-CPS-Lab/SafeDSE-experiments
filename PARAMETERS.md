# Input parameters of the evaluation

Where every number that enters an instance comes from. The paper states the
short form in its evaluation setup. Paths are relative to the SafeDSE
repository (`safedse/`) unless they start with `experiments/`, which means
this repository. Checked against SafeDSE commit `e9ae785`.

## Time

| Parameter | Value | Source |
|---|---|---|
| WCET of an application actor, Rosvall's platform (`pc_sobel`, RQ1) | as published | `data/rosvall/WCETs*.xml`: the DeSyDe TODAES benchmark files (Rosvall and Sander) |
| WCET of an application actor, card platforms (all other pattern instances, `m_3app`) | nominal execution time of the SDF3 file x the mode's cycle factor, rounded | `tools/mkwcets.py` (file header: "Scaffolding only"); nominal times from `data/apps/*.xml`, `data/rosvall/*.hsdf.xml` |
| WCET of a checker, voter, or any other component whose `wcet_type` is not `<owner>` | 0.3 x the owner's nominal time, then x the cycle factor (`--checker-scale 0.3`) | `tools/mkwcets.py` l.20, 41-47; the same for NVP's voter (`voter_FrontEnd` = 42 = checker) |
| WCET of a full replica (`<owner>`) | the owner's WCET | catalog `wcet_type: <owner>` |
| Mode cycle factors | cortexR 1.0 / 1.3 (eco); cortexM 1.6 / 1.2 (performance); ppcE200 (P) 1.15; sparcLEON (S) 1.25 | `data/platform/mixed*.xml` |
| Verdict / comparison edges back to the owner | 1 token (fault reaction within one period) | `data/patterns.yaml`; variant with 2 tokens: `experiments/variants/patterns_tok2.yaml` |
| RQ3 WCETs | per processor type, from `sdf3generate-sdf`'s own `executionTime` entries; a type it leaves out is a forbidden binding | `experiments/gen_synthetic.py` |
| TDMA bus | 16 slots, at most 8 per core, flit 32 (card platforms); 9 slots (Rosvall) | platform files |

All time values are integers in the benchmark's unit; nothing is rounded by
the model (the period is the least integer at or above the MCR).

## Cost (unitless cost units; one unit system for hardware and development)

| Parameter | Value | Source |
|---|---|---|
| Card price (FCR template) | R-card 120, M-card 25, P-card 140, S-card 150 | `data/platform/mixed*.xml`, illustrative |
| Core price per mode | cortexR 40, cortexM 8 / 11 (performance), P 45, S 48 | same |
| Costly platform (Fig. 7, `x_*`) | every price x 4 | `data/platform/mixed_costly.xml`; price sweep x 1, 2, 4, 8, 16: `experiments/variants/platform_noiso_hw*.xml` |
| Certified partitioning | 15 per partitioned core (R-card in `mixed.xml` only) | `data/platform/mixed.xml` |
| Development base cost per actor | 100 (every task type) | `data/cost_model.xml` `<baseline default="100"/>` |
| k(s), development multiplier x100 per SIL 0-4 | myklebust2015 (default): 100 / 113 / 225 / 518 / 906; klosterman: 100 / 113 / 123 / 170 / 235; do178b: 100 / 200 / 300 / 500 / 900; variant klosterman_low: 100 / 113 / 123 / 140 / 159 | `data/cost_model.xml` (SIL 0 and 4 extrapolated; comments there); Myklebust et al. 2015, Table 1 and text; `experiments/variants/cost_model_klosterman_low.xml` |
| Cost of a node | base x k(s) / 100 at its implemented SIL, for every active node (application actor and component) | `lib/safety.mzn` |
| Recurring cost units of a pattern | none 0; same-CPU D/C 1; low-SIL D/C, 2-of-2, isolated checker, 1-of-2 failover, low-SIL diverse D/C 2; mixed-SIL D/C 3; dual 2-of-2 4; NVP 4 | `data/patterns.yaml` (after Armoush's relative costs) |
| `dev_cost_multiplier` (e.g. 2.0 for diverse development) | read, not priced (evaluated as an extension: `experiments/modelext.py`) | `data/patterns.yaml` |
| RQ3 prices | card and core price 10 x (type index + 1) | `experiments/gen_synthetic.py` l.271 |

Total cost = hardware + development (incl. promotion) + partitioning +
recurring pattern units (`model/dse.mzn` l.177).

## Safety

| Parameter | Value | Source |
|---|---|---|
| Required SILs, running example | frontEnd 3, compJah 3, backEnd 2, others 1 | `data/safety_rasta.xml` (illustrative) |
| `d_*` instances | SIL 3 lowered to 2 | `tools/build_all.sh` |
| Sobel | get_pixel 3, others 1 | `data/safety_sobel3.xml` |
| `m_3app` | RASTA as above; Sobel 2; SUSAN 0 | `experiments/multi/safety_3app.xml` |
| RQ3 | 25 % SIL 3, 25 % SIL 2, 50 % SIL 0-1, fault model H | `experiments/gen_synthetic.py` |
| Core-type SIL ceilings | cortexR 4, ppcE200 (P) 4, sparcLEON (S) 4, cortexM 2 | `data/platform/*.xml` |
| Pattern SIL ranges, placement, covered fault classes | catalog data | `data/patterns.yaml`, Table II |

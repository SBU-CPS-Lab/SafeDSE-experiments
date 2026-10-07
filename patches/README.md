# Patches to the SafeDSE model (experiments only)

These patches are applied to copies of SafeDSE's `model/` and `lib/` under
this repository. SafeDSE itself is never changed.

## Exact period: `exact_period_variant_b.patch`

The SafeDSE model posts every pattern channel from `tok` unconditionally. An
inactive pattern slot has zero WCET but keeps its place in its core's static
order, so a channel of an unselected pattern into the slot, followed by an
order edge out of it, is a path that the deployed design does not have. The
least period of a fixed design can then exceed the maximum cycle ratio (MCR)
of the deployed graph, never fall below it (SafeDSE
`docs/design.md#known-limitations`). Two RQ3 time-limited incumbents first
showed it (`actorsweep_n32_i1`: 146 vs 139; `_i2`: 109 vs 105).

Two exact formulations were written and validated on both designs (pinned,
then `VERIFY OK` at 139 and 105):

| Variant | Idea |
|---|---|
| A | pattern channels only under `pe_on` (`tok` keeps no `pe_*` pair; the verifier builds pattern edges of selected patterns only) |
| B | a second "order potential" `qpot` for static-order and wrap edges, equal to `pot` on active nodes; only channels whose guard differs from their endpoints' activity stay under `pe_on` |

Both cost solve time, so the SafeDSE model keeps the conservative
formulation. Measured with 4 workers on `actorsweep_n16_i0`: 71.5 s (model),
303.6 s (A), 251.4 s (B); on `actorsweep_n16_i3` A timed out at 600 s where
the model took 233 s; with one thread on `f_sw3` (300 s) both variants stopped
at cost 6568 where the model proves 3365 in 180 s.

Variant B is used as an exact cross-check: `exact_model.py` creates `exact/`
by applying this patch to the SafeDSE submodule, and `exact_check.py`
(RQ2/RQ4) and `breadth.py multiexact` re-solve stored rows with it. Its
`lib/activation.mzn` comment on `mcm_pattern_edges` still describes variant A,
the first one tried. Variant A is not used by any experiment.

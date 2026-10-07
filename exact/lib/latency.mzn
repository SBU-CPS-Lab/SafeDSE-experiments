% ===========================================================================
% latency.mzn -- end-to-end latency (docs/design.md#latency-estimate)
%
% In the periodic regime of a self-timed schedule the k-th firing of actor i
% starts at approximately pot[i] + k*mu, where pot is the same potential vector
% the throughput constraint already introduces.  So for a source/sink pair:
%
%     latency(s,d) ~= pot[d] - pot[s] + rho(s,d) * mu + T[d]
%
% where rho(s,d) is the minimum number of initial tokens on any path from s to
% d -- the number of iterations of pipeline delay between them.  rho is a
% property of the APPLICATION graph alone, so the front-end computes it once by
% shortest-path over token counts and passes it as a parameter; the schedule
% never reduces it, because serialisation edges carry no tokens and so cannot
% create a shorter token-path.
%
% COST: nothing.  No new variables, one linear expression per constrained pair.
% That is the whole reason for preferring this over the unfolding formulation:
% latency becomes free to constrain, and expensive only to verify.
%
% WHAT IT IS NOT.  This is the steady-state latency, and it ignores the
% transient phase before the schedule becomes periodic.  For a graph whose
% transient is long relative to its period the true worst-case latency of the
% first few iterations can exceed this.  Treat the constraint as a filter --
% it prunes the search cheaply and correctly in the periodic regime -- and
% verify the returned solution exactly with tools/verify.py, which
% simulates the transient.  Reporting these numbers as worst-case latency
% without that verification step would be wrong.
% ===========================================================================

int: nLatCon;                        % number of constrained source/sink pairs
set of int: LC = 1..nLatCon;
array[LC] of int: lat_src;           % actor index
array[LC] of int: lat_dst;
array[LC] of int: lat_rho;           % min initial tokens on any src->dst path
array[LC] of int: lat_ub;            % required bound

array[LC] of var int: latency;

predicate latency_periodic(
    array[int] of var int: pot,
    array[int] of var int: T,
    array[int] of var int: mu_of
) =
    forall(c in LC)(
        latency[c] = pot[lat_dst[c]] - pot[lat_src[c]]
                   + lat_rho[c] * mu_of[lat_src[c]]
                   + T[lat_dst[c]]
    );

constraint forall(c in LC)( latency[c] <= lat_ub[c] );

var int: max_latency;
constraint max_latency = max([0] ++ [latency[c] | c in LC]);

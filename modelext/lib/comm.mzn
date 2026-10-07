% ===========================================================================
% comm.mzn -- TDMA bus communication (docs/design.md#communication)
%
% Communication refinement uses the SAME mechanism as safety patterns: a channel
% whose endpoints land on different processors gets three extra MSAG actors --
% block, send, receive -- activated by the guard `proc[src] != proc[dst]`. The
% front-end instantiates all three per channel statically; the model decides
% which are real. Conditional structural expansion of the dataflow graph,
% guarded by a decision variable, is one operation, and communication and
% safety patterns are two instances of it (docs/design.md#guarded-superposition).
%
% The WCCT model follows DeSyDe's src/cp_model/wcct.constraints:
%
%   receiving costs no time on a TDMA bus -- the buffer sits in the receiving
%   node's local memory, so `rec` has WCET 0 but still exists as an MSAG actor
%   because the receive BUFFER creates a cycle;
%
%   blocking is the worst-case wait for your slot to come round,
%       wcct_b = (tdma_slots - (alloc[p] - 1)) * cycle_length
%
%   sending is the transfer itself, in whole TDMA rounds,
%       wcct_s = (rounds(alloc[p]) * tdma_slots + tdma_slots) * cycle_length
%   where rounds() is a precomputed table indexed by the slot allocation:
%       rounds[0] = 0, rounds[1] = slots_needed - 1,
%       rounds[s] = floor(slots_needed / s), minus 1 when s divides it exactly.
%
% MSAG CYCLES.  Four kinds, matching RAPIDO'17 Fig. 3b:
%   src -> block -> send -> rec -> dst      the forward path
%   block -> src   carrying sendbuf tokens  the send-side buffer
%   rec -> send    carrying recbuf tokens   the receive-side FIFO
%   the send/receive orderings on each node, as for computation
%
% BUFFER SIZES ARE VARIABLES, so the back-edge terms are tok * mu with BOTH
% factors variable, a non-linear term. CP-SAT has a native int_times and
% handles it directly (docs/design.md#throughput-by-node-potentials).
% ===========================================================================
include "globals.mzn";

int: nCh;  set of int: CH = 1..nCh;
array[int] of int: ch_src;         % HSDF node index of the producing actor
array[int] of int: ch_dst;
array[int] of int: ch_msgsize;
array[int] of int: ch_slots_needed;
array[int] of int: ch_direct_tok;  % initial tokens on the original channel   % ceil(msgsize / data_per_slot)
array[int] of int: ch_app;

% Declared over `int` rather than CH because the front-end always emits at least
% one padding row (nCh controls the index set), so an instance built with
% --comm ideal still type-checks.
% the three communication actors per channel, as HSDF node indices
array[int] of int: ch_block;
array[int] of int: ch_send;
array[int] of int: ch_rec;

int: tdma_slots;
int: tdma_cycle_len;
int: data_per_slot;
% rounds[s+1] = full TDMA rounds needed when s slots are allocated
array[int, int] of int: ch_rounds;

array[PR] of var 0..tdma_slots: tdma_alloc;
constraint sum(tdma_alloc) <= tdma_slots;
constraint forall(p in PR)( not used[p] -> tdma_alloc[p] = 0 );

% ---- is this channel remote? ---------------------------------------------
array[1..max(nCh,1)] of var bool: ch_remote;
constraint forall(c in CH)(
    ch_remote[c] <-> ( active[ch_src[c]] /\ active[ch_dst[c]]
                       /\ proc[ch_src[c]] != proc[ch_dst[c]] )
);

% A processor needs slots exactly when something is sent from it.
array[PR] of var bool: p_sends;
constraint forall(p in PR)(
    p_sends[p] <-> exists(c in CH)( ch_remote[c] /\ proc[ch_src[c]] = p )
);
constraint forall(p in PR)( p_sends[p] <-> tdma_alloc[p] > 0 );

% NOTE. Tying `active` for communication actors to ch_remote was tried and
% measured WORSE (a_sobel TDMA-only went 3.1s -> 15.1s): it adds a reified
% equivalence per actor and buys nothing, because the WCCT model already forces
% a local channel's times to zero. The real cost was elsewhere -- see the
% processor-order exclusion below.

% ---- WCCT -----------------------------------------------------------------
int: max_wcct = (tdma_slots + 1) * tdma_cycle_len
              * (max([0] ++ [ch_rounds[c, s] | c in CH, s in 0..tdma_slots]) + 2);
array[1..max(nCh,1)] of var 0..max_wcct: wcct_b;
array[1..max(nCh,1)] of var 0..max_wcct: wcct_s;

% Local channels cost nothing and consume no bus resource.
constraint forall(c in CH)( not ch_remote[c] -> wcct_b[c] = 0 /\ wcct_s[c] = 0 );

constraint forall(c in CH, p in PR)(
    (ch_remote[c] /\ proc[ch_src[c]] = p) -> (
        wcct_b[c] = (tdma_slots - (tdma_alloc[p] - 1)) * tdma_cycle_len
        /\
        wcct_s[c] = (ch_rounds[c, tdma_alloc[p]] * tdma_slots + tdma_slots)
                    * tdma_cycle_len
    )
);

% Allocating more slots than the largest message from a processor needs is pure
% waste and only enlarges the search space. DeSyDe posts the same restriction.
constraint forall(p in PR)(
    tdma_alloc[p] > 0 ->
        (tdma_alloc[p] - 1) * data_per_slot
        < max([0] ++ [ch_msgsize[c] * bool2int(ch_remote[c]
                                               /\ proc[ch_src[c]] = p)
                      | c in CH])
);

% ---- communication actors' WCETs -----------------------------------------
% Receiving takes no time on a TDMA bus: the token lands in the receiving node's
% local memory. `rec` still exists as an MSAG actor because the receive FIFO
% creates a cycle whose length bounds the period.
predicate comm_actor_times(array[int] of var int: T) =
    forall(c in CH)(
        T[ch_block[c]] = wcct_b[c] /\
        T[ch_send[c]]  = wcct_s[c] /\
        T[ch_rec[c]]   = 0
    );

% ---- buffers --------------------------------------------------------------
int: max_buf;
array[1..max(nCh,1)] of var 0..max_buf: sendbuf;
array[1..max(nCh,1)] of var 1..max_buf: recbuf;
constraint forall(c in CH)( not ch_remote[c] -> sendbuf[c] = 0 /\ recbuf[c] = 1 );
constraint forall(c in CH)( ch_remote[c] -> sendbuf[c] >= 1 /\ recbuf[c] >= 1 );

% Send-side buffers live in the network interface, receive-side in local memory.
int: ni_capacity;
constraint forall(p in PR)(
    sum(c in CH)( bool2int(ch_remote[c] /\ proc[ch_src[c]] = p)
                  * sendbuf[c] * ch_msgsize[c] ) <= ni_capacity
);

% ---- MSAG edges for the communication path -------------------------------
% Guarded by ch_remote: a local channel keeps its direct src -> dst edge (posted
% by the base model) and adds nothing.
predicate comm_msag_edges(
    array[int] of var int: T,
    array[int] of var int: pot,
    array[int] of var int: mu_of
) =
    % A LOCAL channel keeps its original direct edge; a REMOTE one is replaced
    % by the block/send/rec path below. The front-end removes these edges from
    % the token matrix so exactly one of the two forms is ever posted.
    forall(c in CH)(
        not ch_remote[c] -> pot[ch_dst[c]] >= pot[ch_src[c]] + T[ch_src[c]]
                                            - ch_direct_tok[c] * mu_of[ch_src[c]]
    )
    /\
    forall(c in CH)(
        (ch_remote[c] -> pot[ch_block[c]] >= pot[ch_src[c]] + T[ch_src[c]]
                                           - ch_direct_tok[c] * mu_of[ch_src[c]])
        /\
        (ch_remote[c] -> pot[ch_send[c]] >= pot[ch_block[c]] + T[ch_block[c]])
        /\
        (ch_remote[c] -> pot[ch_rec[c]] >= pot[ch_send[c]] + T[ch_send[c]])
        /\
        (ch_remote[c] -> pot[ch_dst[c]] >= pot[ch_rec[c]] + T[ch_rec[c]])
        /\
        % the path carries the original channel's initial tokens, placed on its
        % first edge so the pipeline depth is preserved
        % send-side buffer: cycle {src, block} carrying sendbuf tokens
        (ch_remote[c] -> pot[ch_src[c]] >= pot[ch_block[c]] + T[ch_block[c]]
                                         - sendbuf[c] * mu_of[ch_src[c]])
        /\
        % receive-side FIFO: cycle {send, rec} carrying recbuf tokens
        (ch_remote[c] -> pot[ch_send[c]] >= pot[ch_rec[c]] + T[ch_rec[c]]
                                          - recbuf[c] * mu_of[ch_src[c]])
    );

% ---- safety-aware channel separation (docs/design.md#channel-separation) -
% Replicated channels must not share a failure region. On a TDMA bus the shared
% resource is the bus itself; what can be separated is the SLOT, so two channels
% carrying replica traffic are required to originate from processors with
% disjoint slot allocations -- which, given `sum(tdma_alloc) <= tdma_slots`,
% follows from their sources being different processors.
%
% This is weaker than genuine communication redundancy: a single-bus platform
% has a single point of failure no slot discipline can remove. The honest
% statement is that the model enforces what the platform can offer and records
% what it cannot. A replicated bus or a NoC with disjoint routes is what the
% argument actually needs; this model does not provide one.
int: nSep;  set of int: SEP = 1..nSep;
array[int] of int: sep_a;      % channel indices that must stay separated
array[int] of int: sep_b;      % (padded to one row; nSep controls the index set)

constraint forall(k in SEP)(
    (ch_remote[sep_a[k]] /\ ch_remote[sep_b[k]]) ->
        proc[ch_src[sep_a[k]]] != proc[ch_src[sep_b[k]]]
);

% ---- does bus traffic count against its core's SIL? -----------------------
% (docs/design.md#communication-sil)
% A designer setting, because the answer depends on the platform, not on the
% model. If the transfer is DMA driven by dedicated hardware, the block/send
% actors are not application software and Koopman rule 2 does not reach them.
% If the bus driver is software running on that core, it is subject to the rule
% and a SIL-3 core's driver must be developed to SIL 3 -- which makes remote
% communication from a high-SIL core materially more expensive.
%
%   comm_sil_mode = 0  exempt: transfers carry no integrity requirement (DMA)
%   comm_sil_mode = 1  inherit: a transfer inherits the SIL of the data it
%                      carries, i.e. of its sending actor
%   comm_sil_mode = 2  core: a transfer is developed to the SIL of the core it
%                      runs on, exactly like any other software there
int: comm_sil_mode;

constraint comm_sil_mode = 1 -> forall(c in CH)(
    ch_remote[c] -> ( sil_impl[ch_block[c]] >= sil_impl[ch_src[c]]
                   /\ sil_impl[ch_send[c]]  >= sil_impl[ch_src[c]]
                   /\ sil_impl[ch_rec[c]]   >= sil_impl[ch_src[c]] )
);
% mode 2 is enforced by lib/safety.mzn's no-silent-mixing rule once the
% communication actors are no longer exempted from it; see comm_sil_exempt.
bool: comm_sil_exempt = (comm_sil_mode = 0);

var int: n_remote;
constraint n_remote = sum(c in CH)( bool2int(ch_remote[c]) );
var int: total_slots_used;
constraint total_slots_used = sum(tdma_alloc);

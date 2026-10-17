# LineSettle evidence

## Product claim

LineSettle settles a fully funded purchase order by calculating supplier
payouts from buyer-accepted per-line receipt quantities using immutable stored
unit prices.

## Reviewed source and artifact

The hosted proof described here was produced from source revision
[`a1463f3d8f5fea7303567d4233e6e88fcfafbb6a`](https://github.com/starboytiimz/linesettle/commit/a1463f3d8f5fea7303567d4233e6e88fcfafbb6a).
The contract artifact was `linesettle.wasm`, 24,387 bytes, SHA-256
`13f3858854c028c9b7eaf31ef3fb172dd6b830aa30e29980f610ad298fb66524`.
The source revision remains the producer identity for this historical run;
later documentation-only revisions do not change its attribution.

Toolchain: Rust 1.95.0, Cargo 1.95.0, `soroban-sdk` 28.0.0, Stellar CLI
28.1.0, and `wasm32v1-none`. The hosted runner was Ubuntu 24.04 and the local
standalone network used protocol 28.

## Hosted flagship execution

The proof deployed the artifact above to a fresh local standalone network and
used a locally issued classic Stellar asset wrapped as a Stellar Asset
Contract (SAC). These are local test identities and a test-only asset.

| PO lines | Receipt | Independently derived value | Actually settled |
|---|---:|---:|---:|
| A: 10 x 6; B: 8 x 5; total 100 | A+6 / B+4 | 6 x 6 + 4 x 5 = 56 | 56 |
| Same PO | A+4 / B+4 | 4 x 6 + 4 x 5 = 44 | 44 |

After the final receipt, the PO was `Closed`, funded value was 100, paid value
was 100, refunded value was 0, the supplier had received 100 settlement
tokens, and the isolated proof contract held zero remaining settlement tokens.
Raw PO, line, receipt, and token-balance CLI queries are preserved in
[`evidence/stage6/hosted-flagship-proof.txt`](../evidence/stage6/hosted-flagship-proof.txt).

### Deliberate quantity change

With the same stored terms (A 10 x 6 and B 8 x 5), receipt A+5 / B+4 has
independent value 5 x 6 + 4 x 5 = 50. The hosted execution settled 50. This
shows that the payout changed with accepted quantities while the PO prices
stayed fixed.

### Expected stale-receipt rejection

After A+6 / B+4 had settled, a previously submitted A+5 / B+1 receipt was
attempted. The actual CLI invocation exited 1 with
`HostError: Error(Contract, #14)`, corresponding to
`QuantityExceedsRemaining`. Before and after the rejected attempt, the PO
header, both accepted line quantities, stale receipt status (`Submitted`), and
buyer, supplier, and contract settlement-asset balances were equal. The failed
attempt did not settle or mutate this economic state.

### Repeatability and hosted provenance

Three isolated hosted control executions reproduced first payout 56, final
payout 44, `Closed`, paid 100, and refunded 0. Contract IDs, PO IDs, and
transaction hashes are run-specific and are not expected to match.

Historical GitHub Actions run
[`38977916518`](https://github.com/starboytiimz/linesettle/actions/runs/38977916518)
ran on the exact producer revision above. Its `verify` job was
[`117232750103`](https://github.com/starboytiimz/linesettle/actions/runs/38977916518/job/117232750103)
and its `flagship` job was
[`117232748876`](https://github.com/starboytiimz/linesettle/actions/runs/38977916518/job/117232748876); both succeeded. The workflow requested
`stellar/quickstart:latest`; that run recorded resolved image digest
`sha256:4f8dfd693deba57fddef7f0bc722e4782c2e606c8675e3adc5438cebae249295`.
The tag is floating; the recorded digest identifies the image used in that
run, not a permanent pin in the workflow.

The preserved log is a sanitized copy of the uploaded raw proof log. Runner
absolute paths were replaced with stable placeholders. Checksums identify the
preserved bytes; a checksum alone does not establish that an experiment was
correct. The artifact manifest and checksums are in
[`evidence/stage6/manifest.txt`](../evidence/stage6/manifest.txt) and
[`evidence/stage6/SHA256SUMS`](../evidence/stage6/SHA256SUMS).

### Validation on the evidence revision

This evidence revision was then checked by hosted run
[`38981786768`](https://github.com/starboytiimz/linesettle/actions/runs/38981786768)
at source SHA
`e17841902cb8e9cb18afa97a081ce9da90891b34`. Both the `verify` job
([`117244085611`](https://github.com/starboytiimz/linesettle/actions/runs/38981786768/job/117244085611))
and `flagship` job
([`117244085839`](https://github.com/starboytiimz/linesettle/actions/runs/38981786768/job/117244085839))
succeeded on that exact revision. The new flagship artifact records the same
WASM identity, 56/44 closeout, changed-quantity payout 50, stale rejection
with exit 1 and error #14, unchanged post-rejection state and balances, and
three repeat executions. Its sanitized raw CLI log and artifact manifest are
preserved in [`evidence/stage9/`](../evidence/stage9/).

Earlier runs for this revision encountered Quickstart Friendbot HTTP 502 and
a Stellar CLI Friendbot response parse error before any LineSettle invocation.
The workflow now retries local funding up to five times and requires Horizon
to confirm each generated account before continuing. The successful run
records the first issuer request returning 502, then HTTP 200 on retry, with
buyer and supplier returning HTTP 200. Persistent funding or Horizon failure
still fails the job before the proof runs.

## Reproduce

Prerequisites: Rust/Cargo 1.95.0 with `wasm32v1-none`, Stellar CLI 28.1.0,
Python 3, and Docker. From the repository root:

```sh
cargo +1.95.0 fmt --all -- --check
cargo +1.95.0 test --workspace --locked
stellar contract build --locked --package linesettle
```

For the full local ledger proof, use the `flagship` job in
[`ci.yml`](../.github/workflows/ci.yml) as the supported setup and invocation
reference. It starts a fresh protocol-28 standalone network with
`stellar container start local`, waits for network health, creates local test
identities and an SAC, then runs
[`scripts/local-flagship-proof.py`](../scripts/local-flagship-proof.py). The
script independently derives expected receipt values and asserts the actual
state and token-balance results, including the intended stale failure. Do not
use Testnet or Mainnet for this proof.

## Scope and limitations

- No independent external-project compatibility proof has been completed.
  Stage 7 found no legitimate target; Stage 8 is blocked and was not executed.
- There is no physical-delivery proof: receipt quantities are submitted by the
  supplier and accepted by the buyer.
- No LineSettle Testnet or Mainnet contract proof has been completed.
- Cancellation is covered by host tests but has not been demonstrated through
  the CLI local-ledger proof.
- Broad compatibility with arbitrary custom SEP-41 tokens is not claimed; the
  real execution used a classic asset wrapped as a SAC.
- v0.1 uses integer quantities, has no dispute arbitration, and does not
  support PO amendments.
- No project `LICENSE` has been selected. The source is publicly available,
  but no formal open-source license is asserted.

## TTL lifecycle repair

The historical evidence above describes the source and WASM tested by those
runs. The predecessor revision did not explicitly renew Soroban storage TTLs.
The TTL repair adds bounded renewal for the contract instance/code and each
live business-state key needed by an active PO lifecycle. It does not make
storage permanent. Sufficient inactivity can still archive persistent entries;
those entries must be restored through normal Soroban transaction restoration
before contract execution can use them. This repair does not provide an
off-contract restoration service.

The implementation source is commit
[`40b3b12b422d2264c4509de3461c1b7efe972514`](https://github.com/starboytiimz/linesettle/commit/40b3b12b422d2264c4509de3461c1b7efe972514),
tree `0b3363f39a380b475a7bcc61166fdb3b74d03545`. Its `linesettle.wasm` is
25,478 bytes with SHA-256
`e27caede18f9b6c6275d7076b9a671c4964a27b815329ebf399ade0e3770e339`.
Two independent builds from that clean source revision with Rust/Cargo 1.95.0
and Stellar CLI 28.1.0 produced byte-identical artifacts.

The policy uses a 2,073,601-ledger renewal threshold and extends live entries
to 2,592,000 ledgers (approximately 120 and 150 days respectively at a nominal
five-second ledger cadence). These are ledger counts, not wall-clock
guarantees. Public getters extend the contract instance/code but intentionally
do not renew business records; successful state-changing lifecycle operations
renew the header and every PO line, and receipt operations renew the receipt
and each indexed receipt line they use. An actual committed invocation can
therefore incur instance/code TTL writes even when it calls a getter; a
simulation-only read does not commit that extension.

The TTL-enabled artifact was also exercised on a fresh protocol-28 standalone
network with Stellar CLI 28.1.0 and the test-only classic-asset SAC. The local
run reproduced 56 then 44, Closed / paid 100 / refunded 0, the changed-quantity
50 payout, and stale-receipt exit 1 with `QuantityExceedsRemaining`; all
compared economic state and settlement-asset balances were unchanged after the
stale failure. Three independent control deployments reproduced the 56/44
terminal result. The hosted TTL repair run is linked from the pull request once
available; the historical proof identities above remain attributed to their
original source revisions.

This policy does not make storage permanent. Sufficient inactivity can still
archive persistent entries; those entries must be restored through normal
Soroban transaction restoration before contract execution can use them. The
repair does not provide an off-contract restoration service.

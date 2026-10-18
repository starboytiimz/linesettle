# ProcureSettle evidence

> **Project identity:** The product was renamed from LineSettle to ProcureSettle
> after the historical evidence below was produced. Pre-rename proof retains the
> LineSettle name and artifact identity; proof produced after the rename uses
> ProcureSettle. Historical source SHAs and hashes are preserved as recorded.

## Product claim

ProcureSettle settles a fully funded purchase order by calculating supplier
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
stellar contract build --locked --package procuresettle
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

The separate Testnet harness is [`scripts/testnet-flagship-proof.py`](../scripts/testnet-flagship-proof.py).
It requires Stellar CLI 28.1.0, three funded Testnet identities in a private
CLI config directory, and real Testnet transaction fees in XLM. Review its
`--help` and the evidence manifest before running; it creates a classic test
asset, deploys contracts, and submits transactions on public Testnet. The
captured Testnet run is evidence, not a recurring CI gate.

## Scope and limitations

- No independent external-project compatibility proof has been completed.
  An earlier exact-match Stage 7 search found no legitimate external target,
  so Stage 8 was not executed at that time. Subsequent demand research
  identified credible adjacent Stellar-native procurement/commerce workflows
  that may serve as compatibility targets. No ProcureSettle external-project
  compatibility proof, adoption, endorsement, or upstream integration is
  claimed yet.
- There is no physical-delivery proof: receipt quantities are submitted by the
  supplier and accepted by the buyer.
- A public ProcureSettle Stellar Testnet flagship proof is recorded below. No
  Mainnet deployment or proof is claimed.
- Cancellation is covered by host tests but has not been demonstrated through
  the CLI local-ledger proof.
- Broad compatibility with arbitrary custom SEP-41 tokens is not claimed; the
  real execution used a classic asset wrapped as a SAC.
- v0.1 uses integer quantities, has no dispute arbitration, and does not
  support PO amendments.
- The project is dual licensed under MIT OR Apache-2.0; see `LICENSE-MIT` and
  `LICENSE-APACHE`.

## Public Stellar Testnet flagship proof

The following proof used the ProcureSettle artifact built from source
[`1e114630cda64260235dd9f4b61456b73c56f9f9`](https://github.com/starboytiimz/procuresettle/commit/1e114630cda64260235dd9f4b61456b73c56f9f9),
tree `c333da06cdf60169f0b03f323da09a6f983ef521`. The deployed contract is
`CB2N2TJDMINCTXWPGMDZI7LNTMQYHC5ZYTT24YJKIPRQLP5UOAUSICEG`. The Stellar CLI
deployment flow submitted separate transactions for code upload and contract
instance creation: WASM code upload/install transaction
[`5135b907fb2c2d23212b99fb0a29b7e8606ba18f70c636f0b06937480032886e`](https://stellar.expert/explorer/testnet/tx/5135b907fb2c2d23212b99fb0a29b7e8606ba18f70c636f0b06937480032886e),
then contract instance creation/deployment transaction
[`cbe56d1f20de2f97c8af9641ccd4dced2e356d9a1b6a347c9829e0a96274e8d6`](https://stellar.expert/explorer/testnet/tx/cbe56d1f20de2f97c8af9641ccd4dced2e356d9a1b6a347c9829e0a96274e8d6),
which created the contract ID above and invoked `__constructor` against the
same WASM hash. The artifact was `procuresettle.wasm`, 25,484 bytes, SHA-256
`78b5c6dec22ad1cea497223ff411af544dcd1e9fd613b85514f2c5ae03873a7c`.

The run used Stellar CLI 28.1.0, Rust/Cargo 1.95.0, `soroban-sdk` 28.0.0,
and the public Stellar Testnet at protocol 29. Settlement used the classic
asset `PSET:GADNRQAUDP3AWNV5A5Q26N4667LK3Z3GGYPZLER6VFONODXPZ5OVM25W`, wrapped
as SAC `CBTMM72FRA3O6UBHGOXQ6PS2M77HHJSKGCWVNHMM7XAEUPCU54WO3SUS`.

For the control PO (A 10 x 6; B 8 x 5; total 100), the independently
calculated A+6/B+4 receipt settled 56, and the A+4/B+4 receipt settled 44.
The PO closed with funded value 100, paid value 100, refunded value 0, and no
remaining control-PO balance in the contract. With the same terms, A+5/B+4
settled 50. These were actual Testnet token transfers and contract state
queries, not host-only results.

On a third funded PO, the previously valid stale A+5/B+1 receipt was attempted
after A+6/B+4 had settled. The CLI exited 1 with `QuantityExceedsRemaining`
(PoError #14). The stale receipt remained `Submitted`; PO and line state and
buyer/supplier/contract PSET balances compared equal before and after the
failed attempt. Final isolated proof balances were buyer 700, supplier 206,
and contract 94 base units, independently accounting for the three funded
POs and their successful settlements.

The deployment, setup, and successful lifecycle transaction hashes, plus
fetched Testnet transaction results/meta/events, are preserved in
[`evidence/testnet/transactions.json`](../evidence/testnet/transactions.json).
The captured CLI transcript and manifest/checksums are in the same directory.
The failed stale attempt was rejected during simulation and therefore has no
submitted transaction hash; its nonzero CLI output and before/after state are
preserved in the transcript and manifest.

This is a Testnet result only. Testnet assets have no monetary value and the
network is periodically reset; the public ledger records may eventually become
unavailable. The committed fetched transaction data preserves the observed
results for later inspection. No Mainnet, production-readiness, audit, physical
delivery, broad arbitrary SEP-41 compatibility, Movix compatibility, adoption,
or endorsement claim is made.

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
terminal result. The hosted PR run
[`39027872531`](https://github.com/starboytiimz/linesettle/actions/runs/39027872531)
checked out source `42197f30a30979f7b2976cb04b8af2d1155d75e8`; its `verify`
job [`117382327373`](https://github.com/starboytiimz/linesettle/actions/runs/39027872531/job/117382327373)
and `flagship` job
[`117382327538`](https://github.com/starboytiimz/linesettle/actions/runs/39027872531/job/117382327538)
both succeeded. That hosted execution produced the same WASM identity and
reproduced the local semantic results above. The historical proof identities
above remain attributed to their original source revisions.

This policy does not make storage permanent. Sufficient inactivity can still
archive persistent entries; those entries must be restored through normal
Soroban transaction restoration before contract execution can use them. The
repair does not provide an off-contract restoration service.

## Current ProcureSettle artifact

The product was renamed from LineSettle without changing settlement behavior.
The first ProcureSettle release build was produced from source revision
[`2e91c98440b20d26165bfe21186ded607072234c`](https://github.com/starboytiimz/linesettle/commit/2e91c98440b20d26165bfe21186ded607072234c),
tree `d6b008c68a4a3ad82d2af8d96ff9d007c5f62fe6`. The package is `procuresettle`
and the artifact is `target/wasm32v1-none/release/procuresettle.wasm`, 25,484
bytes, SHA-256
`78b5c6dec22ad1cea497223ff411af544dcd1e9fd613b85514f2c5ae03873a7c`.
Rust/Cargo 1.95.0, `soroban-sdk` 28.0.0, Stellar CLI 28.1.0, and
`wasm32v1-none` were used. The workspace has 64 passing host tests, including
the TTL ledger-advance and rollback tests.

A real local protocol-28 standalone execution using a locally issued classic
asset wrapped as a SAC re-proved the renamed artifact. It settled 56 then 44
for the canonical A10x6 / B8x5 PO, closed with paid 100/refunded 0, settled 50
for changed receipt quantities A5/B4, and rejected the stale A5/B1 receipt
with process exit 1 / `QuantityExceedsRemaining` (#14), with no compared
economic state or balance mutation. Three repeat controls also reproduced
56/44 and the same terminal accounting. Full raw CLI output, source/tool/image
provenance, deployment and SAC IDs, and checksums are preserved in
[`evidence/procuresettle/`](../evidence/procuresettle/). The fresh network used
`stellar/quickstart:latest`, resolved for this run to
`sha256:4f8dfd693deba57fddef7f0bc722e4782c2e606c8675e3adc5438cebae249295`;
the tag remains floating.

The earlier 24,387-byte `linesettle.wasm` and SHA-256
`13f3858854c028c9b7eaf31ef3fb172dd6b830aa30e29980f610ad298fb66524` remain
the historical LineSettle artifact identity. Pre-rename proof logs and
manifests are unchanged. The package rename changes the generated artifact
identity while preserving the contract's settlement semantics.

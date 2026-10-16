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

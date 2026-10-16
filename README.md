# LineSettle

Quantity-derived purchase-order settlement on Stellar.

LineSettle is a Soroban purchase-order receipt settlement contract. Settlement
amounts are computed from buyer-accepted quantities on stored PO lines at fixed
stored unit prices, rather than from predefined milestone amounts.

Generic milestone escrow releases predetermined amounts. LineSettle stores
ordered quantities and unit prices, tracks cumulative accepted quantities, and
calculates each settlement from newly accepted receipt deltas.

## Implemented flow

Buyer creates a PO → supplier accepts → buyer funds the full PO → supplier
submits receipt quantities → buyer accepts and settles them → the contract
rechecks current quantity bounds and transfers the exact quantity-derived
SEP-41 amount to the supplier.

## Current evidence

- 54 local Soroban host tests passed on the current baseline.
- Rust 1.95.0, `soroban-sdk = 28.0.0`, Stellar CLI 28.1.0, and
  `wasm32v1-none` are pinned.
- `Cargo.lock` is committed; the contract has a locked WASM build.
- The GitHub Actions workflow checks formatting, tests, and the locked Stellar
  CLI contract build.

Run the checks from the repository root:

```sh
cargo fmt --all -- --check
cargo test --workspace --locked
stellar contract build --locked --package linesettle
```

## Limits

- Evidence is local host testing; there is no Testnet or Mainnet proof yet.
- The contract records supplier delivery assertions and buyer quantity
  acceptance; it does not automatically verify physical delivery.
- There is no dispute arbitration, invoice financing, factoring, or PO
  amendment in v0.1.
- Each PO is limited to eight lines.
- Compatibility has been tested with the pinned SDK host and its Stellar Asset
  Contract fixture, not broadly with custom SEP-41 tokens.
- Full-fulfillment closeout and mutual cancellation/refund are not implemented.
- No `LICENSE` has been selected yet.

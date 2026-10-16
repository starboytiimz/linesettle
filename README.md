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
SEP-41 amount to the supplier. When every ordered quantity is accepted, the PO
closes automatically. Buyer and supplier may also cancel an Accepted or Funded
PO together; a Funded PO refunds only its remaining unpaid value to the buyer.

## Evidence

The project has 62 host tests and a hosted local-ledger proof of quantity-based
settlement, closeout, changed-quantity payout, and stale-receipt rejection. See
the [evidence record](docs/EVIDENCE.md) for the exact reviewed source and WASM
identities, raw hosted proof log, limitations, and reproduction path.

Run the checks from the repository root:

```sh
cargo fmt --all -- --check
cargo test --workspace --locked
stellar contract build --locked --package linesettle
```

## Limits

- There is no LineSettle Testnet or Mainnet contract proof.
- The contract records supplier delivery assertions and buyer quantity
  acceptance; it does not automatically verify physical delivery.
- There is no dispute arbitration, invoice financing, factoring, or PO
  amendment in v0.1.
- Each PO is limited to eight lines.
- Compatibility has been tested with the pinned SDK host and its Stellar Asset
  Contract fixture, not broadly with custom SEP-41 tokens.
- Cancellation is covered by local host tests but has not been demonstrated in
  the CLI local-ledger proof.
- No `LICENSE` has been selected yet.

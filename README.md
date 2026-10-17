# ProcureSettle

Accepted-quantity procurement settlement on Stellar.

ProcureSettle (formerly LineSettle) is a Soroban purchase-order settlement
contract. Supplier payouts are calculated from buyer-accepted per-line receipt
quantities at stored unit prices rather than from fixed milestone amounts.

Generic milestone escrow releases predetermined amounts. ProcureSettle stores
ordered quantities and unit prices, tracks cumulative accepted quantities, and
calculates each settlement from newly accepted receipt deltas.

## Implemented flow

Buyer creates a PO → supplier accepts → buyer funds the full PO → supplier
submits receipt quantities → buyer accepts and settles them → the contract
rechecks current quantity bounds and transfers the exact quantity-derived
settlement amount to the supplier. When every ordered quantity is accepted,
the PO closes automatically. Buyer and supplier may also cancel an Accepted or
Funded PO together; a Funded PO refunds only its remaining unpaid value to the
buyer.

## Evidence

The project has 64 host tests and a hosted local-ledger proof of quantity-based
settlement, closeout, changed-quantity payout, and stale-receipt rejection. See
the [evidence record](docs/EVIDENCE.md) for historical LineSettle and current
ProcureSettle source/WASM identities, raw proof logs, limitations, and the
reproduction path.

Run the checks from the repository root:

```sh
cargo fmt --all -- --check
cargo test --workspace --locked
stellar contract build --locked --package procuresettle
```

## License

ProcureSettle is dual licensed under [MIT](LICENSE-MIT) or
[Apache-2.0](LICENSE-APACHE).

## Limits

- There is no ProcureSettle Testnet or Mainnet contract proof.
- The contract records supplier delivery assertions and buyer quantity
  acceptance; it does not automatically verify physical delivery.
- There is no dispute arbitration, invoice financing, factoring, or PO
  amendment in v0.1.
- Each PO is limited to eight lines.
- Compatibility has been tested with the pinned SDK host and its Stellar Asset
  Contract fixture, not broadly with custom SEP-41 tokens.
- Cancellation is covered by local host tests but has not been demonstrated in
  the CLI local-ledger proof.
- Active contract calls renew the contract instance/code and the relevant live
  PO, line, receipt, and receipt-line entries to a bounded TTL window. This is
  not permanent storage: after sufficiently long inactivity, Soroban may archive
  an entry and an invocation will require normal ledger restoration first.

Evidence produced before the product rename retains the historical LineSettle
name and artifact identity. Evidence produced after the rename uses
ProcureSettle.

#!/usr/bin/env python3
"""Verify the pinned, read-only Movix line-item source model for F4.

This helper does not import or copy Movix code. It checks the immutable source
revision and blob identities, extracts the one public fixture, independently
recomputes its line values and total, and verifies the documented workflow
boundary before any settlement proof is run.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

PINNED_COMMIT = "05f2ba1dbe72a8f46e546b4509281e54933708e4"
EXPECTED_BLOBS = {
    "packages/domain/src/fixtures.ts": "97177a96ed7da93f5163aef90354a070e432edaa",
    "packages/domain/src/domain.test.ts": "ec680c2d2253e0501855acdd085619aa681fb9ec",
    "packages/domain/src/orders.ts": "76be7f589386bfe3a65ea43ed646692ecfe3c0d4",
    "docs/Movix-ASEAN-Agricultural-Trade-Pivot.md": "3fe9a4a4202d00614c1d2a3e83c08ecfd26e1b57",
    "docs/Movix-Sprint-Plan.md": "e70877877392c6d052322eca1d6e1fdb4e1167a7",
    "docs/contracts/escrow-v1/abi.md": "386d9b2a1ff48509b31ae2a18054df37026c2c20",
    "contracts/escrow/src/lib.rs": "a2890723a1983ad8ea9196880def33ced4b626b1",
    "apps/web/features/orders/escrow-fulfillment-panel.tsx": "54be7049bb2dea660c97e745f08f64ced95fdbe2",
    "docs/Movix-Sprint-08-Fulfillment-Release-Detailed.md": None,
}


def git(checkout: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *args],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {message}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--movix-checkout", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="Optional normalized JSON output")
    args = parser.parse_args()
    checkout = args.movix_checkout.resolve()

    head = git(checkout, "rev-parse", "HEAD")
    require(head == PINNED_COMMIT, f"Movix HEAD is {head}, expected {PINNED_COMMIT}")
    tree = git(checkout, "rev-parse", "HEAD^{tree}")
    for path, expected_blob in EXPECTED_BLOBS.items():
        blob = git(checkout, "rev-parse", f"HEAD:{path}")
        if expected_blob is not None:
            require(blob == expected_blob, f"unexpected Git blob for {path}: {blob}")

    fixture_path = checkout / "packages/domain/src/fixtures.ts"
    fixture = fixture_path.read_text(encoding="utf-8")
    draft = re.search(r"export const draftOrder\s*=\s*\{(?P<body>.*?)\n\}\s*as const;", fixture, re.S)
    require(draft is not None, "draftOrder fixture block not found")
    body = draft.group("body")
    po_match = re.search(r'purchaseOrderNumber:\s*"([^"]+)"', body)
    total_match = re.search(r"totalBaseUnits:\s*([\d_]+)n", body)
    line_matches = re.findall(
        r"\{\s*lineNumber:\s*(\d+)\s*,\s*quantity:\s*([\d_]+)n\s*,\s*unitPriceBaseUnits:\s*([\d_]+)n\s*\}",
        body,
    )
    require(po_match is not None and po_match.group(1) == "MOVIX-PO-0001", "expected PO fixture not found")
    require(total_match is not None, "draftOrder.totalBaseUnits not found")
    lines = [
        {
            "line_number": int(line_no),
            "quantity": int(quantity.replace("_", "")),
            "unit_price_base_units": int(price.replace("_", "")),
        }
        for line_no, quantity, price in line_matches
    ]
    expected_lines = [
        {"line_number": 1, "quantity": 2, "unit_price_base_units": 125_000_000},
        {"line_number": 2, "quantity": 3, "unit_price_base_units": 72_500_000},
    ]
    require(lines == expected_lines, f"fixture line tuples differ: {lines!r}")
    total = int(total_match.group(1).replace("_", ""))
    derived_values = [line["quantity"] * line["unit_price_base_units"] for line in lines]
    recomputed_total = sum(derived_values)
    require(total == 467_500_000, f"fixture total is {total}, expected 467500000")
    require(recomputed_total == total, f"independent line calculation gives {recomputed_total}, fixture says {total}")

    domain_test = (checkout / "packages/domain/src/domain.test.ts").read_text(encoding="utf-8")
    require(
        "line.quantity * line.unitPriceBaseUnits" in domain_test
        and "expect(calculated).toBe(draftOrder.totalBaseUnits)" in domain_test,
        "Movix domain test does not independently recompute the fixture total",
    )
    pivot = (checkout / "docs/Movix-ASEAN-Agricultural-Trade-Pivot.md").read_text(encoding="utf-8")
    sprint8 = (checkout / "docs/Movix-Sprint-08-Fulfillment-Release-Detailed.md").read_text(encoding="utf-8")
    abi = (checkout / "docs/contracts/escrow-v1/abi.md").read_text(encoding="utf-8")
    escrow = (checkout / "contracts/escrow/src/lib.rs").read_text(encoding="utf-8")
    panel = (checkout / "apps/web/features/orders/escrow-fulfillment-panel.tsx").read_text(encoding="utf-8")
    for phrase in ("split shipments, multiple lots, partial delivery, and partial release", "AGR-03 — Final quantity"):
        require(phrase in pivot, f"current product authority is missing: {phrase}")
    require("Partial releases, split shipments, or multi-tranche payments" in sprint8, "Sprint 8 scope boundary missing")
    require("quantity changes payable value" in pivot and "parties amend or enter the exception/refund path" in pivot, "AGR-03 boundary missing")
    require("confirm_delivery" in escrow and "token_client.transfer(&contract, &escrow.supplier, &net_amount)" in escrow, "whole-order settlement source path changed")
    require("release the full escrow" in panel, "full-release UI statement missing")
    require("no dispute resolver, partial settlement" in abi, "escrow ABI limitation missing")

    result = {
        "verdict": "PASS",
        "repository": "https://github.com/chrissstellee/Movix",
        "commit": head,
        "tree": tree,
        "fixture_blob": EXPECTED_BLOBS["packages/domain/src/fixtures.ts"],
        "purchase_order_number": po_match.group(1),
        "lines": [
            {**line, "line_value_base_units": value}
            for line, value in zip(lines, derived_values, strict=True)
        ],
        "fixture_total_base_units": total,
        "independently_recomputed_total_base_units": recomputed_total,
        "domain_test_independently_recomputes_total": True,
        "deferred_partial_delivery_and_release_scope_verified": True,
        "agr_03_amendment_or_exception_refund_boundary_verified": True,
        "whole_order_release_after_delivery_confirmation_verified": True,
        "verified_blob_ids": {path: git(checkout, "rev-parse", f"HEAD:{path}") for path in EXPECTED_BLOBS},
    }
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

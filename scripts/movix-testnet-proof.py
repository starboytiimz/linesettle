#!/usr/bin/env python3
"""Execute the bounded ProcureSettle/Movix source-model proof on Stellar Testnet.

The script reads a separately pinned Movix checkout, creates new test-only
identities and MVXP asset, deploys the caller-supplied ProcureSettle WASM, and
checks real contract state and SAC balances. Identity material is kept only in
the private Stellar config directory and is never printed or copied to reports.
Testnet is resettable; this script is intentionally not part of routine CI.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

EXPECTED_WASM_SIZE = 25_484
EXPECTED_WASM_SHA = "78b5c6dec22ad1cea497223ff411af544dcd1e9fd613b85514f2c5ae03873a7c"
MOVIX_COMMIT = "05f2ba1dbe72a8f46e546b4509281e54933708e4"
TESTNET_RPC = "https://soroban-testnet.stellar.org"
HORIZON = "https://horizon-testnet.stellar.org"
HASH_RE = re.compile(r"Signing transaction:\s*([0-9a-fA-F]{64})")
G_RE = re.compile(r"\bG[A-Z2-7]{55}\b")
C_RE = re.compile(r"\bC[A-Z2-7]{55}\b")
STALE_ERROR = "HostError: Error(Contract, #14)"
LINES = [
    {"line_id": 1, "ordered_quantity": 2, "unit_price": "125000000"},
    {"line_id": 2, "ordered_quantity": 3, "unit_price": "72500000"},
]


class Proof:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.config = args.config_dir.resolve()
        self.wasm = args.wasm.resolve()
        self.repo = Path(__file__).resolve().parents[1]
        self.report = args.report_dir.resolve()
        self.report.mkdir(parents=True, exist_ok=True)
        self.config.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.config, 0o700)
        self.log_path = self.report / "movix-testnet-proof.log"
        self.log = self.log_path.open("w", encoding="utf-8")
        self.cli = ["stellar", "--config-dir", str(self.config)]
        self.transactions: dict[str, dict[str, str]] = {}
        if args.prior_transactions:
            self.transactions = json.loads(args.prior_transactions.read_text(encoding="utf-8"))
        self.last_hashes: list[str] = []
        self.summary: dict[str, Any] = {
            "transactions": sorted(self.transactions),
            "submitted_operations": [],
            "verified_cli_post_submit_errors": [],
        }

    def close(self) -> None:
        self.log.close()

    def run(
        self,
        args: list[str],
        *,
        expected_exit: int = 0,
        read_only: bool = False,
        secret_output: bool = False,
    ) -> tuple[int, str, str]:
        cmd = self.cli + args
        display = shlex.join(cmd).replace(str(self.config), "$PRIVATE_F4_CONFIG")
        self.last_hashes = []
        attempts = 4 if read_only else 1
        proc: subprocess.CompletedProcess[str] | None = None
        for attempt in range(attempts):
            proc = subprocess.run(cmd, text=True, capture_output=True, check=False, timeout=180)
            if secret_output:
                self.log.write(f"\n$ {display}\nEXIT={proc.returncode}\n[identity command output suppressed]\n")
            else:
                self.log.write(
                    f"\n$ {display}\nEXIT={proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}\n"
                )
            self.log.flush()
            combined = proc.stdout + "\n" + proc.stderr
            transient_read_errors = (
                "could not query captive core",
                "client error (Connect)",
                "Request timeout",
            )
            if not (
                read_only
                and proc.returncode != 0
                and any(error in combined for error in transient_read_errors)
                and attempt + 1 < attempts
            ):
                break
            self.log.write("Retrying read-only query after transient RPC/captive-core error.\n")
            self.log.flush()
            time.sleep(1)
        assert proc is not None
        if not secret_output:
            for tx_hash in HASH_RE.findall(proc.stdout + "\n" + proc.stderr):
                tx_hash = tx_hash.lower()
                self.last_hashes.append(tx_hash)
                if tx_hash not in self.transactions:
                    self.fetch_transaction(tx_hash)
            if self.last_hashes:
                self.summary["submitted_operations"].append(
                    {"command": display, "transaction_hashes": list(self.last_hashes)}
                )
        if expected_exit >= 0 and proc.returncode != expected_exit:
            combined = proc.stdout + "\n" + proc.stderr
            confirmed_errors = ("Read-only file system", "Request timeout")
            observed_post_submit_error = next((error for error in confirmed_errors if error in combined), None)
            post_submit_success = (
                proc.returncode != 0
                and observed_post_submit_error is not None
                and bool(self.last_hashes)
                and all('"tx_success"' in self.transactions[tx_hash].get("result", "")
                        for tx_hash in self.last_hashes)
            )
            if post_submit_success:
                self.summary["verified_cli_post_submit_errors"].append({
                    "command": display,
                    "process_exit": proc.returncode,
                    "post_submit_error": observed_post_submit_error,
                    "ledger_results": {tx_hash: "tx_success" for tx_hash in self.last_hashes},
                    "action": "continued only after independent tx fetch confirmed success; no transaction was retried",
                })
                self.log.write(
                    "NOTE: CLI exited nonzero after submission because its local filesystem write failed. "
                    "Fetched transaction result confirms tx_success; no transaction was retried.\n"
                )
                self.log.flush()
            else:
                raise RuntimeError(f"{display}: exit {proc.returncode}, expected {expected_exit}; see {self.log_path}")
        return proc.returncode, proc.stdout, proc.stderr

    def fetch_transaction(self, tx_hash: str) -> None:
        fetched: dict[str, str] = {}
        for kind in ("result", "meta", "events"):
            proc = None
            for attempt in range(4):
                proc = subprocess.run(
                    self.cli + ["tx", "fetch", kind, "--hash", tx_hash, "--network", "testnet"],
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=180,
                )
                if proc.returncode == 0 or attempt == 3:
                    break
                self.log.write(f"Fetch retry {attempt + 1}/3 for {kind} of {tx_hash}: read-only query failed.\n")
                self.log.flush()
                time.sleep(2)
            assert proc is not None
            joined = proc.stdout + (("\nSTDERR:\n" + proc.stderr) if proc.stderr else "")
            self.log.write(f"\n$ stellar tx fetch {kind} --hash {tx_hash} --network testnet\nEXIT={proc.returncode}\n{joined}\n")
            self.log.flush()
            if proc.returncode != 0 and kind in ("result", "meta", "events"):
                raise RuntimeError(f"failed to fetch {kind} for submitted transaction {tx_hash}: {joined}")
            fetched[kind] = joined
        self.transactions[tx_hash] = fetched
        self.summary["transactions"] = sorted(self.transactions)
        (self.report / "transactions.json").write_text(
            json.dumps(self.transactions, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def address(self, identity: str) -> str:
        _, out, err = self.run(["keys", "public-key", identity], read_only=True)
        matches = G_RE.findall(out + "\n" + err)
        if not matches:
            raise RuntimeError(f"could not resolve public address for {identity}")
        return matches[-1]

    def query(self, contract: str, method: str, *args: str) -> Any:
        _, out, err = self.run(
            ["contract", "invoke", "--id", contract, "--source", self.args.buyer_identity,
             "--network", "testnet", "--", method, *args],
            read_only=True,
        )
        for stream in (out, err):
            for line in reversed(stream.splitlines()):
                try:
                    return json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
        raise RuntimeError(f"no JSON query result for {method}: {out!r} {err!r}")

    def invoke(self, contract: str, identity: str, method: str, *args: str) -> tuple[int, str, str]:
        return self.run(
            ["contract", "invoke", "--id", contract, "--source", identity,
             "--network", "testnet", "--", method, *args]
        )

    def balance(self, account: str, sac: str) -> int:
        _, out, err = self.run(
            ["token", "balance", "--id", sac, "--account", account, "--network", "testnet"],
            read_only=True,
        )
        for line in reversed((out + "\n" + err).splitlines()):
            try:
                return int(line.strip())
            except ValueError:
                pass
        raise RuntimeError(f"raw smallest-unit balance not found: {out!r} {err!r}")

    def horizon_account_creation(self, address: str) -> dict[str, Any]:
        url = f"{HORIZON}/accounts/{address}/operations?limit=20&order=asc"
        with urllib.request.urlopen(url, timeout=30) as response:
            data = json.load(response)
        records = data.get("_embedded", {}).get("records", [])
        row = next((x for x in records if x.get("type") == "create_account" and x.get("account") == address), None)
        if row is None:
            raise RuntimeError(f"Friendbot-funded account creation not visible on Horizon: {address}")
        tx_hash = str(row.get("transaction_hash", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", tx_hash):
            raise RuntimeError(f"invalid account-funding transaction hash for {address}")
        if tx_hash not in self.transactions:
            self.fetch_transaction(tx_hash)
        return {"address": address, "transaction_hash": tx_hash, "horizon_operation_id": row.get("id")}

    def create_identities(self) -> dict[str, str]:
        identities = {
            "issuer": self.args.issuer_identity,
            "buyer": self.args.buyer_identity,
            "supplier": self.args.supplier_identity,
        }
        identity_dir = self.config / "identity"
        for role, name in identities.items():
            exists = (identity_dir / f"{name}.toml").exists()
            if exists and not self.args.reuse_identities:
                raise RuntimeError(f"identity already exists in isolated config; refusing reuse: {name}")
            if not exists and self.args.reuse_identities:
                raise RuntimeError(f"cannot resume; expected identity is missing: {name}")
            if not exists:
                self.run(["keys", "generate", name, "--network", "testnet", "--fund"], secret_output=True)
        addresses = {role: self.address(name) for role, name in identities.items()}
        self.summary["account_funding"] = [self.horizon_account_creation(address) for address in addresses.values()]
        self.summary["identities"] = addresses
        return addresses

    def setup_asset(self, addresses: dict[str, str]) -> str:
        asset = f"MVXP:{addresses['issuer']}"
        self.summary["asset"] = asset
        for role in ("buyer", "supplier"):
            self.run([
                "tx", "new", "change-trust", "--source", self.args_identity(role),
                "--line", asset, "--network", "testnet",
            ])
            self.summary.setdefault("trustline_hashes", {})[role] = self.last_hashes[-1]
        _, out, err = self.run([
            "contract", "asset", "deploy", "--asset", asset,
            "--source", self.args.issuer_identity, "--network", "testnet",
        ])
        sac_matches = C_RE.findall(out + "\n" + err)
        if not sac_matches:
            raise RuntimeError("Stellar CLI did not return MVXP SAC contract ID")
        sac = sac_matches[-1]
        self.summary["sac_id"] = sac
        self.summary["sac_deploy_hashes"] = list(self.last_hashes)
        self.run([
            "tx", "new", "payment", "--source", self.args.issuer_identity,
            "--destination", addresses["buyer"], "--asset", asset,
            "--amount", "467500000", "--network", "testnet",
        ])
        self.summary["issuance_hash"] = self.last_hashes[-1]
        _, dec_out, dec_err = self.run(["token", "decimals", "--id", sac, "--network", "testnet"], read_only=True)
        decimals = (dec_out or dec_err).strip().splitlines()[-1].strip().strip('"')
        if decimals != "7":
            raise RuntimeError(f"unexpected MVXP SAC decimals: {decimals!r}")
        initial = {role: self.balance(addresses[role], sac) for role in ("buyer", "supplier")}
        initial["sac_contract"] = self.balance(sac, sac)
        if initial != {"buyer": 467_500_000, "supplier": 0, "sac_contract": 0}:
            raise RuntimeError(f"unexpected initial smallest-unit balances: {initial}")
        self.summary["initial_asset_balances"] = initial
        return sac

    def args_identity(self, role: str) -> str:
        return {"issuer": self.args.issuer_identity, "buyer": self.args.buyer_identity,
                "supplier": self.args.supplier_identity}[role]

    def deploy(self) -> str:
        _, out, err = self.run([
            "contract", "deploy", "--wasm", str(self.wasm),
            "--source", self.args.issuer_identity, "--network", "testnet",
        ])
        ids = C_RE.findall(out + "\n" + err)
        if not ids:
            raise RuntimeError("contract deploy returned no contract ID")
        contract = ids[-1]
        self.summary["contract_id"] = contract
        self.summary["deploy_transaction_hashes"] = list(self.last_hashes)
        self.verify_deployed_hash(contract)
        _, interface_out, interface_err = self.run([
            "contract", "info", "interface", "--id", contract, "--network", "testnet",
        ], read_only=True)
        self.summary["deployed_interface"] = (interface_out or interface_err).strip()
        return contract

    def verify_deployed_hash(self, contract: str) -> None:
        _, hash_out, hash_err = self.run([
            "contract", "info", "hash", "--id", contract, "--network", "testnet",
        ], read_only=True)
        deployed_hash = (hash_out or hash_err).strip().splitlines()[-1].strip().lower()
        if EXPECTED_WASM_SHA not in deployed_hash:
            raise RuntimeError(f"on-network contract WASM hash mismatch: {deployed_hash}")
        self.summary["deployed_wasm_hash_output"] = deployed_hash

    def create_po(self, contract: str, po_id: int, sac: str, buyer: str, supplier: str) -> None:
        self.invoke(contract, self.args.buyer_identity, "create_po",
                    "--po_id", str(po_id), "--buyer", buyer, "--supplier", supplier,
                    "--token", sac, "--lines", json.dumps(LINES, separators=(",", ":")))
        po = self.query(contract, "get_po", "--po_id", str(po_id))
        if po["status"] != "Draft" or int(po["total_value"]) != 467_500_000:
            raise RuntimeError(f"unexpected newly created PO: {po}")

    def fund(self, contract: str, po_id: int, sac: str, buyer: str, supplier: str) -> dict[str, Any]:
        buyer_before = self.balance(buyer, sac)
        contract_before = self.balance(contract, sac)
        self.invoke(contract, self.args.supplier_identity, "accept_po", "--po_id", str(po_id))
        self.invoke(contract, self.args.buyer_identity, "fund_po", "--po_id", str(po_id))
        po = self.query(contract, "get_po", "--po_id", str(po_id))
        after_buyer = self.balance(buyer, sac)
        after_contract = self.balance(contract, sac)
        if po["status"] != "Funded" or int(po["funded_value"]) != 467_500_000:
            raise RuntimeError(f"unexpected funded state: {po}")
        if buyer_before - after_buyer != 467_500_000 or after_contract - contract_before != 467_500_000:
            raise RuntimeError("funding token deltas did not equal the exact Movix fixture total")
        return {"po": po, "buyer_delta": after_buyer - buyer_before,
                "contract_delta": after_contract - contract_before}

    def submit(self, contract: str, po_id: int, receipt_id: int, quantities: list[dict[str, int]]) -> None:
        self.invoke(contract, self.args.supplier_identity, "submit_receipt",
                    "--po_id", str(po_id), "--receipt_id", str(receipt_id),
                    "--quantities", json.dumps(quantities, separators=(",", ":")))

    def receipt_value_from_raw(self, contract: str, po_id: int, receipt_id: int) -> tuple[int, dict[str, Any]]:
        receipt = self.query(contract, "get_receipt", "--po_id", str(po_id), "--receipt_id", str(receipt_id))
        raw = []
        total = 0
        for line_id in receipt["line_ids"]:
            rline = self.query(contract, "get_receipt_line", "--po_id", str(po_id),
                               "--receipt_id", str(receipt_id), "--line_id", str(line_id))
            line = self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", str(line_id))
            line_value = int(rline["quantity_delta"]) * int(line["unit_price"])
            total += line_value
            raw.append({"receipt_line": rline, "po_line": line, "independent_line_value": line_value})
        return total, {"receipt": receipt, "lines": raw}

    def settle(self, contract: str, po_id: int, receipt_id: int, expected: int,
               sac: str, buyer: str, supplier: str) -> dict[str, Any]:
        buyer_before = self.balance(buyer, sac)
        supplier_before = self.balance(supplier, sac)
        contract_before = self.balance(contract, sac)
        _, out, err = self.invoke(contract, self.args.buyer_identity, "accept_receipt_and_settle",
                                  "--po_id", str(po_id), "--receipt_id", str(receipt_id))
        tx_hashes = list(self.last_hashes)
        try:
            result = json.loads(out.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            raise RuntimeError(f"settlement returned no JSON amount: {out!r} {err!r}") from exc
        after = {
            "buyer": self.balance(buyer, sac),
            "supplier": self.balance(supplier, sac),
            "contract": self.balance(contract, sac),
        }
        if int(result) != expected:
            raise RuntimeError(f"settlement returned {result}, independently expected {expected}")
        if after["buyer"] != buyer_before or after["supplier"] - supplier_before != expected or contract_before - after["contract"] != expected:
            raise RuntimeError(f"settlement token balance deltas mismatch: {after}")
        if not tx_hashes:
            raise RuntimeError("settlement produced no captured submitted transaction hash")
        for tx_hash in tx_hashes:
            events = self.transactions[tx_hash]["events"]
            amount_event = re.search(rf'"i128"\s*:\s*"{expected}"', events)
            if "transfer" not in events or amount_event is None:
                raise RuntimeError(f"fetched SAC transfer event missing amount {expected} in {tx_hash}")
        return {"return_value": int(result), "expected": expected, "transaction_hashes": tx_hashes,
                "balances_before": {"buyer": buyer_before, "supplier": supplier_before, "contract": contract_before},
                "balances_after": after}

    def audit_completed_proof(self, contract: str, po_id: int, sac: str,
                              buyer: str, supplier: str, info: dict[str, Any],
                              extraction: dict[str, Any], wasm_sha: str, wasm_size: int) -> dict[str, Any]:
        """Independently reconcile the completed Testnet PO and preserved stale-attempt transcript."""
        po = self.query(contract, "get_po", "--po_id", str(po_id))
        lines = {str(i): self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", str(i)) for i in (1, 2)}
        receipts = {str(i): self.query(contract, "get_receipt", "--po_id", str(po_id), "--receipt_id", str(i)) for i in (1, 2, 3)}
        balances = {role: self.balance(address, sac) for role, address in
                    (("buyer", buyer), ("supplier", supplier), ("contract", contract))}
        if (po["status"] != "Closed" or int(po["funded_value"]) != 467_500_000
                or int(po["paid_value"]) != 467_500_000 or int(po["refunded_value"]) != 0
                or [int(lines[str(i)]["accepted_quantity"]) for i in (1, 2)] != [2, 3]
                or [receipts[str(i)]["status"] for i in (1, 2, 3)] != ["Settled", "Submitted", "Settled"]
                or balances != {"buyer": 0, "supplier": 467_500_000, "contract": 0}):
            raise RuntimeError(f"completed on-chain state mismatch: po={po}, lines={lines}, receipts={receipts}, balances={balances}")

        r1_value, r1_raw = self.receipt_value_from_raw(contract, po_id, 1)
        stale_value, stale_raw = self.receipt_value_from_raw(contract, po_id, 2)
        r3_value, r3_raw = self.receipt_value_from_raw(contract, po_id, 3)
        if (r1_value, stale_value, r3_value) != (197_500_000, 322_500_000, 270_000_000):
            raise RuntimeError(f"raw stored terms did not independently reproduce receipt values: {r1_value}, {stale_value}, {r3_value}")

        stale_before, stale_after, stale_rc, stale_error = self.read_stale_transcript(
            po_id, contract, sac, buyer, supplier
        )
        stale_equal = stale_before == stale_after
        if not stale_equal:
            raise RuntimeError(f"preserved stale pre/post economic snapshots differ: {stale_before} != {stale_after}")

        settlement_hashes = {}
        for amount in (197_500_000, 270_000_000):
            matches = []
            for tx_hash, fetched in self.transactions.items():
                events = fetched.get("events", "")
                if ('"symbol":"transfer"' in events and contract in events and supplier in events
                        and f'"i128":"{amount}"' in events):
                    matches.append(tx_hash)
            if len(matches) != 1:
                raise RuntimeError(f"expected exactly one fetched SAC transfer of {amount}, found {matches}")
            tx_hash = matches[0]
            fetched = self.transactions[tx_hash]
            if not all(k in fetched for k in ("result", "meta", "events")) or '"tx_success"' not in fetched["result"]:
                raise RuntimeError(f"settlement transaction not fully fetched/successful: {tx_hash}")
            settlement_hashes[str(amount)] = tx_hash

        self.summary.update({
            "execution_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "procuresettle_source_sha": subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip(),
            "procuresettle_source_tree": subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip(),
            "wasm": {"filename": self.wasm.name, "size": wasm_size, "sha256": wasm_sha},
            "rustc": subprocess.run(["rustc", "+1.95.0", "--version"], text=True, capture_output=True, check=True).stdout.strip(),
            "cargo": subprocess.run(["cargo", "+1.95.0", "--version"], text=True, capture_output=True, check=True).stdout.strip(),
            "stellar_cli": subprocess.run(["stellar", "--version"], text=True, capture_output=True, check=True).stdout.strip(),
            "soroban_sdk": "28.0.0",
            "network": {"info": info, "rpc_endpoint": TESTNET_RPC, "rpc_hostname": "soroban-testnet.stellar.org"},
            "movix": extraction,
            "po_id": po_id, "asset": f"MVXP:{self.address(self.args.issuer_identity)}",
            "sac_id": sac, "contract_id": contract,
            "control": {"final_po": po, "final_lines": lines, "receipt_states": receipts,
                        "receipt_1_raw": r1_raw, "receipt_1_independent_value": r1_value,
                        "stale_receipt_raw": stale_raw, "stale_value_initially_valid": stale_value,
                        "stale_before": stale_before, "stale_after": stale_after,
                        "stale_process_exit": stale_rc, "stale_contract_error": "QuantityExceedsRemaining (PoError #14)",
                        "stale_failure_output": stale_error, "stale_equal": stale_equal,
                        "receipt_3_raw": r3_raw, "receipt_3_independent_value": r3_value,
                        "settlement_transaction_hashes": settlement_hashes,
                        "final_balances": balances},
            "whole_proof_balances": balances,
            "transaction_count_unique": len(self.transactions),
        })
        summary_path = self.report / "summary.json"
        summary_path.write_text(json.dumps(self.summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"verdict": "PASS", "po_id": po_id, "first": r1_value,
                          "stale_expected": stale_value, "stale_error": "QuantityExceedsRemaining (#14)",
                          "stale_exit": stale_rc, "stale_equal": stale_equal, "final": r3_value,
                          "status": po["status"], "paid": int(po["paid_value"]),
                          "balances": balances, "settlement_hashes": settlement_hashes,
                          "unique_fetched_transactions": len(self.transactions),
                          "summary": str(summary_path)}, indent=2))
        return self.summary

    def read_stale_transcript(self, po_id: int, contract: str, sac: str,
                              buyer: str, supplier: str) -> tuple[dict[str, Any], dict[str, Any], int, str]:
        transcript_path = self.args.stale_transcript or self.log_path
        text = transcript_path.read_text(encoding="utf-8")
        blocks = list(re.finditer(r"(?m)^\$ (.+)$", text))
        stale_index = next((i for i, m in enumerate(blocks)
                            if "accept_receipt_and_settle" in m.group(1)
                            and f"--po_id {po_id}" in m.group(1) and "--receipt_id 2" in m.group(1)), None)
        if stale_index is None:
            raise RuntimeError("raw transcript has no stale receipt invocation")
        stop = blocks[stale_index + 1].start() if stale_index + 1 < len(blocks) else len(text)
        start = blocks[stale_index].start()
        stale_block = text[start:stop]
        exit_match = re.search(r"(?m)^EXIT=(-?\d+)$", stale_block)
        if not exit_match or int(exit_match.group(1)) == 0 or STALE_ERROR not in stale_block or HASH_RE.search(stale_block):
            raise RuntimeError("raw stale attempt lacks a nonzero QuantityExceedsRemaining failure before submission")

        def extract_snapshot(region: str, before: bool) -> dict[str, Any]:
            region_blocks = list(re.finditer(r"(?m)^\$ (.+)$", region))
            signatures = [
                ("get_po", ""), ("get_line", "--line_id 1"), ("get_line", "--line_id 2"),
                ("get_receipt", "--receipt_id 2"),
                ("token balance", f"--account {buyer}"),
                ("token balance", f"--account {supplier}"),
                ("token balance", f"--account {contract}"),
            ]
            found: dict[str, Any] = {}
            selected: list[tuple[re.Match[str], str]] = []
            for name, suffix in signatures:
                candidates = [m for m in region_blocks if name in m.group(1)
                              and (f"--po_id {po_id}" in m.group(1) if name != "token balance" else True)
                              and (suffix in m.group(1) if suffix else True)
                              and (f"--id {sac}" in m.group(1) if name == "token balance" else True)]
                if not candidates:
                    raise RuntimeError(f"stale transcript missing {name} snapshot query {suffix}")
                selected.append((candidates[-1] if before else candidates[0], name + suffix))
            for match, key in selected:
                next_block = next((m for m in region_blocks if m.start() > match.start()), None)
                block_end = next_block.start() if next_block else len(region)
                block = region[match.start():block_end]
                out_match = re.search(r"STDOUT:\n(.*?)\nSTDERR:", block, re.S)
                if not out_match:
                    raise RuntimeError(f"stale transcript query has no stdout: {key}")
                raw = out_match.group(1).strip()
                try:
                    value: Any = json.loads(raw)
                except json.JSONDecodeError:
                    value = int(raw)
                if key.startswith("get_po"):
                    found["po"] = value
                elif key.startswith("get_line"):
                    line_id = "1" if "--line_id 1" in key else "2"
                    found.setdefault("lines", {})[line_id] = value
                elif key.startswith("get_receipt"):
                    found["receipt"] = value
                else:
                    account = key.split("--account ", 1)[1]
                    role = "buyer" if account == buyer else "supplier" if account == supplier else "contract"
                    found.setdefault("balances", {})[role] = value
            return found

        pre_region = text[:start]
        post_region = text[stop:]
        pre = extract_snapshot(pre_region, True)
        post = extract_snapshot(post_region, False)
        for snap in (pre, post):
            if (snap.get("po", {}).get("status") != "Funded"
                    or int(snap.get("po", {}).get("paid_value", -1)) != 197_500_000
                    or snap.get("receipt", {}).get("status") != "Submitted"
                    or snap.get("balances") != {"buyer": 0, "supplier": 197_500_000, "contract": 270_000_000}):
                raise RuntimeError(f"stale transcript snapshot does not match expected post-receipt-1 checkpoint: {snap}")
        return pre, post, int(exit_match.group(1)), stale_block

    def run_proof(self) -> dict[str, Any]:
        wasm_bytes = self.wasm.read_bytes()
        wasm_sha = hashlib.sha256(wasm_bytes).hexdigest()
        if len(wasm_bytes) != EXPECTED_WASM_SIZE or wasm_sha != EXPECTED_WASM_SHA:
            raise RuntimeError(f"unexpected WASM identity: size={len(wasm_bytes)} sha256={wasm_sha}")
        movix = subprocess.run(
            [sys.executable, str(self.repo / "scripts/movix-compatibility-proof.py"),
             "--movix-checkout", str(self.args.movix_checkout.resolve()),
             "--output", str(self.report / "movix-source-verification.json")],
            text=True, capture_output=True, check=False,
        )
        self.log.write("\n$ python scripts/movix-compatibility-proof.py --movix-checkout $PRIVATE_MOVIX_CHECKOUT\n")
        self.log.write(f"EXIT={movix.returncode}\nSTDOUT:\n{movix.stdout}\nSTDERR:\n{movix.stderr}\n")
        self.log.flush()
        if movix.returncode != 0:
            raise RuntimeError("pinned Movix source extraction did not pass; no Testnet actions started")
        extraction = json.loads(movix.stdout)

        _, net_out, net_err = self.run(["network", "info", "--network", "testnet", "--output", "json"], read_only=True)
        info = json.loads(net_out or net_err)
        _, health_out, health_err = self.run(["network", "health", "--network", "testnet", "--output", "json"], read_only=True)
        health = json.loads(health_out or health_err)
        if health.get("status") != "healthy" or int(info.get("protocol_version", 0)) != 29:
            raise RuntimeError(f"unexpected/unhealthy Testnet network: info={info}, health={health}")
        self.summary.update({
            "execution_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "procuresettle_source_sha": subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip(),
            "procuresettle_source_tree": subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip(),
            "wasm": {"filename": self.wasm.name, "size": len(wasm_bytes), "sha256": wasm_sha},
            "rustc": subprocess.run(["rustc", "+1.95.0", "--version"], text=True, capture_output=True, check=True).stdout.strip(),
            "cargo": subprocess.run(["cargo", "+1.95.0", "--version"], text=True, capture_output=True, check=True).stdout.strip(),
            "stellar_cli": subprocess.run(["stellar", "--version"], text=True, capture_output=True, check=True).stdout.strip(),
            "soroban_sdk": "28.0.0",
            "network": {"info": info, "health": health, "rpc_endpoint": TESTNET_RPC, "rpc_hostname": "soroban-testnet.stellar.org"},
            "movix": extraction,
        })

        addresses = self.create_identities()
        if self.args.resume_sac_id:
            sac = self.args.resume_sac_id
            initial = {role: self.balance(addresses[role], sac) for role in ("buyer", "supplier")}
            initial["contract"] = self.balance(
                self.args.resume_contract_id if (self.args.resume_po_id is not None or self.args.audit_completed_po_id is not None) else sac, sac
            )
            expected_resume = ({"buyer": 0, "supplier": 467_500_000, "contract": 0}
                               if self.args.audit_completed_po_id is not None else
                               {"buyer": 0, "supplier": 197_500_000, "contract": 270_000_000}
                               if self.args.resume_po_id is not None else
                               {"buyer": 467_500_000, "supplier": 0, "contract": 0})
            if initial != expected_resume:
                raise RuntimeError(f"resume asset balances differ from expected initial state: {initial}")
            self.summary["asset"] = f"MVXP:{addresses['issuer']}"
            self.summary["sac_id"] = sac
            self.summary["initial_asset_balances"] = initial
        else:
            sac = self.setup_asset(addresses)
        if self.args.resume_contract_id:
            contract = self.args.resume_contract_id
            self.summary["contract_id"] = contract
            self.summary["deploy_transaction_hashes"] = self.args.deployment_hash or []
            self.verify_deployed_hash(contract)
            _, interface_out, interface_err = self.run([
                "contract", "info", "interface", "--id", contract, "--network", "testnet",
            ], read_only=True)
            self.summary["deployed_interface"] = (interface_out or interface_err).strip()
        else:
            contract = self.deploy()
        if self.args.resume_po_id is None and self.args.audit_completed_po_id is None and self.balance(contract, sac) != 0:
            raise RuntimeError("fresh ProcureSettle contract did not begin with zero MVXP balance")
        for tx_hash in self.args.fetch_hash:
            if tx_hash not in self.transactions:
                self.fetch_transaction(tx_hash)
        if self.args.audit_completed_po_id is not None:
            return self.audit_completed_proof(
                contract, self.args.audit_completed_po_id, sac, addresses["buyer"], addresses["supplier"],
                info, extraction, wasm_sha, len(wasm_bytes),
            )
        if self.args.resume_po_id is not None:
            return self.resume_after_first_settlement(
                contract, self.args.resume_po_id, sac, addresses["buyer"], addresses["supplier"],
                info, extraction, wasm_sha, len(wasm_bytes),
            )
        po_id = time.time_ns() // 1_000_000 * 10
        self.summary["po_id"] = po_id
        self.create_po(contract, po_id, sac, addresses["buyer"], addresses["supplier"])
        funding = self.fund(contract, po_id, sac, addresses["buyer"], addresses["supplier"])
        if self.balance(contract, sac) != 467_500_000:
            raise RuntimeError("contract custody did not equal funded fixture value")

        self.submit(contract, po_id, 1, [{"line_id": 1, "quantity_delta": 1}, {"line_id": 2, "quantity_delta": 1}])
        r1_value, r1_raw = self.receipt_value_from_raw(contract, po_id, 1)
        if r1_value != 197_500_000:
            raise RuntimeError(f"independent receipt 1 calculation was {r1_value}")
        self.submit(contract, po_id, 2, [{"line_id": 1, "quantity_delta": 2}, {"line_id": 2, "quantity_delta": 1}])
        stale_value, stale_raw = self.receipt_value_from_raw(contract, po_id, 2)
        if stale_value != 322_500_000:
            raise RuntimeError(f"independent stale receipt calculation was {stale_value}")
        if any(int(row["receipt_line"]["quantity_delta"]) > int(row["po_line"]["ordered_quantity"]) for row in stale_raw["lines"]):
            raise RuntimeError("receipt 2 was not initially within original ordered quantities")

        first = self.settle(contract, po_id, 1, 197_500_000, sac, addresses["buyer"], addresses["supplier"])
        after_first_po = self.query(contract, "get_po", "--po_id", str(po_id))
        after_first_lines = {
            str(i): self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", str(i))
            for i in (1, 2)
        }
        r1_state = self.query(contract, "get_receipt", "--po_id", str(po_id), "--receipt_id", "1")
        if int(after_first_po["paid_value"]) != 197_500_000 or [after_first_lines[str(i)]["accepted_quantity"] for i in (1, 2)] != [1, 1]:
            raise RuntimeError("post-first-settlement PO state did not match accepted quantities/value")
        if r1_state["status"] != "Settled" or self.balance(contract, sac) != 270_000_000:
            raise RuntimeError("receipt 1 state or remaining contract balance is incorrect")

        stale_before = {
            "po": self.query(contract, "get_po", "--po_id", str(po_id)),
            "lines": {str(i): self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", str(i)) for i in (1, 2)},
            "receipt": self.query(contract, "get_receipt", "--po_id", str(po_id), "--receipt_id", "2"),
            "balances": {role: self.balance(address, sac) for role, address in
                         (("buyer", addresses["buyer"]), ("supplier", addresses["supplier"]), ("contract", contract))},
        }
        if stale_before["receipt"]["status"] != "Submitted" or stale_before["lines"]["1"]["ordered_quantity"] - stale_before["lines"]["1"]["accepted_quantity"] != 1:
            raise RuntimeError("stale receipt is not stale for the intended line 1 quantity reason")
        stale_rc, stale_out, stale_err = self.run([
            "contract", "invoke", "--id", contract, "--source", self.args.buyer_identity,
            "--network", "testnet", "--", "accept_receipt_and_settle",
            "--po_id", str(po_id), "--receipt_id", "2",
        ], expected_exit=-1)
        stale_error = stale_out + "\n" + stale_err
        if STALE_ERROR not in stale_error or self.last_hashes:
            raise RuntimeError(f"stale attempt did not fail in simulation with intended #14 before submission: {stale_error}")
        stale_after = {
            "po": self.query(contract, "get_po", "--po_id", str(po_id)),
            "lines": {str(i): self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", str(i)) for i in (1, 2)},
            "receipt": self.query(contract, "get_receipt", "--po_id", str(po_id), "--receipt_id", "2"),
            "balances": {role: self.balance(address, sac) for role, address in
                         (("buyer", addresses["buyer"]), ("supplier", addresses["supplier"]), ("contract", contract))},
        }
        stale_equal = stale_before == stale_after and stale_after["receipt"]["status"] == "Submitted"
        if not stale_equal:
            raise RuntimeError(f"stale failure mutated economic state: before={stale_before}; after={stale_after}")

        self.submit(contract, po_id, 3, [{"line_id": 1, "quantity_delta": 1}, {"line_id": 2, "quantity_delta": 2}])
        final_value, final_raw = self.receipt_value_from_raw(contract, po_id, 3)
        if final_value != 270_000_000:
            raise RuntimeError(f"independent final receipt calculation was {final_value}")
        final = self.settle(contract, po_id, 3, 270_000_000, sac, addresses["buyer"], addresses["supplier"])
        final_po = self.query(contract, "get_po", "--po_id", str(po_id))
        final_lines = {str(i): self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", str(i)) for i in (1, 2)}
        final_receipt = self.query(contract, "get_receipt", "--po_id", str(po_id), "--receipt_id", "3")
        final_balances = {role: self.balance(address, sac) for role, address in
                          (("buyer", addresses["buyer"]), ("supplier", addresses["supplier"]), ("contract", contract))}
        if final_po["status"] != "Closed" or int(final_po["funded_value"]) != 467_500_000 or int(final_po["paid_value"]) != 467_500_000 or int(final_po["refunded_value"]) != 0:
            raise RuntimeError(f"final accounting mismatch: {final_po}")
        if [final_lines[str(i)]["accepted_quantity"] for i in (1, 2)] != [2, 3] or final_receipt["status"] != "Settled":
            raise RuntimeError("final line or receipt state mismatch")
        if final_balances != {"buyer": 0, "supplier": 467_500_000, "contract": 0}:
            raise RuntimeError(f"final PSET-style isolated accounting mismatch: {final_balances}")

        self.summary.update({
            "sac_id": sac,
            "control": {"po_id": po_id, "funding": funding, "receipt_1_raw": r1_raw,
                        "receipt_1_independent_value": r1_value, "settlement_1": first,
                        "receipt_1_state": r1_state, "post_first_po": after_first_po,
                        "post_first_lines": after_first_lines, "stale_value_initially_valid": stale_value,
                        "stale_receipt_raw": stale_raw, "stale_before": stale_before,
                        "stale_after": stale_after, "stale_equal": stale_equal,
                        "stale_process_exit": stale_rc, "stale_contract_error": "QuantityExceedsRemaining (PoError #14)",
                        "stale_failure_output": stale_error,
                        "receipt_3_raw": final_raw, "receipt_3_independent_value": final_value,
                        "settlement_3": final, "final_po": final_po, "final_lines": final_lines,
                        "final_receipt": final_receipt, "final_balances": final_balances},
            "whole_proof_balances": final_balances,
            "transaction_count_unique": len(self.transactions),
        })
        summary_path = self.report / "summary.json"
        summary_path.write_text(json.dumps(self.summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print("PROCURESETTLE_MOVIX_TESTNET_PROOF")
        print(json.dumps({
            "verdict": "PASS",
            "source_sha": self.summary["procuresettle_source_sha"],
            "source_tree": self.summary["procuresettle_source_tree"],
            "wasm": self.summary["wasm"],
            "network_protocol": info["protocol_version"],
            "movix_commit": MOVIX_COMMIT,
            "fixture_total": 467_500_000,
            "contract_id": contract,
            "sac_id": sac,
            "receipt_1": 197_500_000,
            "stale_error": "QuantityExceedsRemaining (#14)",
            "stale_process_exit": stale_rc,
            "stale_equal": stale_equal,
            "receipt_3": 270_000_000,
            "final_status": final_po["status"],
            "paid": int(final_po["paid_value"]),
            "refunded": int(final_po["refunded_value"]),
            "final_balances": final_balances,
            "unique_fetched_transactions": len(self.transactions),
            "summary": str(summary_path),
            "raw_log": str(self.log_path),
        }, indent=2))
        return self.summary

    def resume_after_first_settlement(
        self, contract: str, po_id: int, sac: str, buyer: str, supplier: str,
        info: dict[str, Any], extraction: dict[str, Any], wasm_sha: str, wasm_size: int,
    ) -> dict[str, Any]:
        """Continue only from the independently checked F4 post-receipt-1 state."""
        po = self.query(contract, "get_po", "--po_id", str(po_id))
        lines = {str(i): self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", str(i)) for i in (1, 2)}
        receipt_1 = self.query(contract, "get_receipt", "--po_id", str(po_id), "--receipt_id", "1")
        receipt_2 = self.query(contract, "get_receipt", "--po_id", str(po_id), "--receipt_id", "2")
        balances = {role: self.balance(address, sac) for role, address in
                    (("buyer", buyer), ("supplier", supplier), ("contract", contract))}
        expected_checkpoint = {"buyer": 0, "supplier": 197_500_000, "contract": 270_000_000}
        if (po["status"] != "Funded" or int(po["funded_value"]) != 467_500_000
                or int(po["paid_value"]) != 197_500_000 or int(po["refunded_value"]) != 0
                or [int(lines[str(i)]["accepted_quantity"]) for i in (1, 2)] != [1, 1]
                or receipt_1["status"] != "Settled" or receipt_2["status"] != "Submitted"
                or balances != expected_checkpoint):
            raise RuntimeError(
                f"resume checkpoint mismatch; refusing to continue: po={po}, lines={lines}, "
                f"receipt1={receipt_1}, receipt2={receipt_2}, balances={balances}"
            )
        stale_before = {"po": po, "lines": lines, "receipt": receipt_2, "balances": balances}
        stale_rc, stale_out, stale_err = self.run([
            "contract", "invoke", "--id", contract, "--source", self.args.buyer_identity,
            "--network", "testnet", "--", "accept_receipt_and_settle",
            "--po_id", str(po_id), "--receipt_id", "2",
        ], expected_exit=-1)
        stale_error = stale_out + "\n" + stale_err
        if STALE_ERROR not in stale_error or self.last_hashes:
            raise RuntimeError(f"stale attempt did not fail in simulation with intended #14 before submission: {stale_error}")
        stale_after = {
            "po": self.query(contract, "get_po", "--po_id", str(po_id)),
            "lines": {str(i): self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", str(i)) for i in (1, 2)},
            "receipt": self.query(contract, "get_receipt", "--po_id", str(po_id), "--receipt_id", "2"),
            "balances": {role: self.balance(address, sac) for role, address in
                         (("buyer", buyer), ("supplier", supplier), ("contract", contract))},
        }
        stale_equal = stale_before == stale_after and stale_after["receipt"]["status"] == "Submitted"
        if not stale_equal:
            raise RuntimeError(f"stale failure mutated economic state: before={stale_before}; after={stale_after}")

        self.submit(contract, po_id, 3, [
            {"line_id": 1, "quantity_delta": 1}, {"line_id": 2, "quantity_delta": 2},
        ])
        final_value, final_raw = self.receipt_value_from_raw(contract, po_id, 3)
        if final_value != 270_000_000:
            raise RuntimeError(f"independent final receipt calculation was {final_value}")
        final = self.settle(contract, po_id, 3, 270_000_000, sac, buyer, supplier)
        final_po = self.query(contract, "get_po", "--po_id", str(po_id))
        final_lines = {str(i): self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", str(i)) for i in (1, 2)}
        final_receipt = self.query(contract, "get_receipt", "--po_id", str(po_id), "--receipt_id", "3")
        final_balances = {role: self.balance(address, sac) for role, address in
                          (("buyer", buyer), ("supplier", supplier), ("contract", contract))}
        if (final_po["status"] != "Closed" or int(final_po["funded_value"]) != 467_500_000
                or int(final_po["paid_value"]) != 467_500_000 or int(final_po["refunded_value"]) != 0
                or [int(final_lines[str(i)]["accepted_quantity"]) for i in (1, 2)] != [2, 3]
                or final_receipt["status"] != "Settled"
                or final_balances != {"buyer": 0, "supplier": 467_500_000, "contract": 0}):
            raise RuntimeError(f"final resumed proof state mismatch: po={final_po}, lines={final_lines}, balances={final_balances}")
        self.summary.update({
            "execution_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "procuresettle_source_sha": subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip(),
            "procuresettle_source_tree": subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip(),
            "wasm": {"filename": self.wasm.name, "size": wasm_size, "sha256": wasm_sha},
            "rustc": subprocess.run(["rustc", "+1.95.0", "--version"], text=True, capture_output=True, check=True).stdout.strip(),
            "cargo": subprocess.run(["cargo", "+1.95.0", "--version"], text=True, capture_output=True, check=True).stdout.strip(),
            "stellar_cli": subprocess.run(["stellar", "--version"], text=True, capture_output=True, check=True).stdout.strip(),
            "soroban_sdk": "28.0.0",
            "network": {"info": info, "rpc_endpoint": TESTNET_RPC, "rpc_hostname": "soroban-testnet.stellar.org"},
            "movix": extraction,
            "po_id": po_id,
            "asset": f"MVXP:{self.address(self.args.issuer_identity)}",
            "sac_id": sac,
            "contract_id": contract,
            "control": {"checkpoint_after_receipt_1": stale_before, "stale_before": stale_before,
                        "stale_after": stale_after, "stale_equal": stale_equal,
                        "stale_process_exit": stale_rc,
                        "stale_contract_error": "QuantityExceedsRemaining (PoError #14)",
                        "stale_failure_output": stale_error,
                        "receipt_3_raw": final_raw, "receipt_3_independent_value": final_value,
                        "settlement_3": final, "final_po": final_po, "final_lines": final_lines,
                        "final_receipt": final_receipt, "final_balances": final_balances},
            "whole_proof_balances": final_balances,
            "transaction_count_unique": len(self.transactions),
        })
        summary_path = self.report / "summary.json"
        summary_path.write_text(json.dumps(self.summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print("PROCURESETTLE_MOVIX_TESTNET_PROOF_RESUMED")
        print(json.dumps({"verdict": "PASS", "po_id": po_id, "receipt_3": 270_000_000,
                          "stale_error": "QuantityExceedsRemaining (#14)", "stale_exit": stale_rc,
                          "stale_equal": stale_equal, "final_status": final_po["status"],
                          "paid": int(final_po["paid_value"]), "refunded": int(final_po["refunded_value"]),
                          "final_balances": final_balances, "transactions": len(self.transactions),
                          "summary": str(summary_path)}, indent=2))
        return self.summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--movix-checkout", type=Path, required=True)
    parser.add_argument("--wasm", type=Path, default=Path("target/wasm32v1-none/release/procuresettle.wasm"))
    parser.add_argument("--report-dir", type=Path, default=Path(".codex/reports/movix-testnet"))
    parser.add_argument("--issuer-identity", default="procuresettle_f4_issuer")
    parser.add_argument("--buyer-identity", default="procuresettle_f4_buyer")
    parser.add_argument("--supplier-identity", default="procuresettle_f4_supplier")
    parser.add_argument("--reuse-identities", action="store_true", help="Resume only when all named identities already exist in the private config")
    parser.add_argument("--resume-sac-id", help="Use a SAC already established and independently balance-checked in this F4 config")
    parser.add_argument("--resume-contract-id", help="Continue with an already deployed F4 ProcureSettle instance")
    parser.add_argument("--resume-po-id", type=int, help="Continue only from the verified post-receipt-1 F4 checkpoint")
    parser.add_argument("--audit-completed-po-id", type=int, help="Audit a completed F4 PO and write its proof summary without chain writes")
    parser.add_argument("--stale-transcript", type=Path, help="Prior private raw CLI transcript containing the stale attempt and pre/post snapshots")
    parser.add_argument("--deployment-hash", action="append", default=[], help="Previously submitted deployment transaction hash; may be repeated")
    parser.add_argument("--fetch-hash", action="append", default=[], help="Fetch prior submitted transaction result/meta/events before continuing")
    parser.add_argument("--prior-transactions", type=Path, help="Existing private transaction fetch map to extend")
    args = parser.parse_args()
    proof = Proof(args)
    try:
        proof.run_proof()
        return 0
    except Exception as exc:
        print(f"F4_TESTNET_PROOF_FAILED: {exc}", file=sys.stderr)
        print(f"Private raw log: {proof.log_path}", file=sys.stderr)
        return 1
    finally:
        proof.close()


if __name__ == "__main__":
    raise SystemExit(main())

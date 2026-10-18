#!/usr/bin/env python3
"""Run ProcureSettle's public Stellar Testnet flagship proof.

Requires Stellar CLI 28.1.0 and funded issuer/buyer/supplier identities in a
private config directory. It creates a test-only classic PSET asset, deploys
its SAC and the supplied ProcureSettle WASM, and exercises three isolated POs.
Secret material is never read, logged, or printed. The script captures submitted
transaction hashes and fetches public result/meta/event data where supported.
Testnet is resettable; this is not a Testnet CI job.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
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
EXPECTED_STALE_ERROR = "HostError: Error(Contract, #14)"  # PoError::QuantityExceedsRemaining
HASH_RE = re.compile(r"Signing transaction:\s*([0-9a-fA-F]{64})")
TESTNET_RPC = "https://soroban-testnet.stellar.org"
HORIZON = "https://horizon-testnet.stellar.org"
LINES = [
    {"line_id": 1, "ordered_quantity": 10, "unit_price": "6"},
    {"line_id": 2, "ordered_quantity": 8, "unit_price": "5"},
]


class Proof:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.config = args.config_dir.resolve()
        self.wasm = args.wasm.resolve()
        self.report_dir = args.report_dir.resolve()
        self.repo_root = Path(__file__).resolve().parents[1]
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.report_dir / f"proof-script-{time.time_ns()}.log"
        self.log = self.log_path.open("w", encoding="utf-8")
        self.cli = ["stellar", "--config-dir", str(self.config)]
        self.summary: dict[str, Any] = {"transactions": [], "network": {}}
        self.tx_details: dict[str, dict[str, str]] = {}
        self.last_tx_hashes: list[str] = []
        self.summary["cli_post_submit_errors"] = []
        if args.prior_transactions:
            self.tx_details = json.loads(args.prior_transactions.read_text(encoding="utf-8"))
            self.summary["transactions"] = [
                {"hash": tx_hash, "fetched": list(details)}
                for tx_hash, details in sorted(self.tx_details.items())
            ]

    def close(self) -> None:
        self.log.close()

    def run(
        self, args: list[str], expected_exit: int = 0, read_only: bool = False
    ) -> tuple[int, str, str]:
        cmd = self.cli + args
        self.last_tx_hashes = []
        attempts = 4 if read_only else 1
        for attempt in range(attempts):
            display = shlex.join(cmd).replace(str(self.config), "$PRIVATE_CONFIG_DIR").replace(str(self.repo_root), "$REPO_ROOT")
            self.log.write(f"\n$ {display}\n")
            try:
                proc = subprocess.run(cmd, text=True, capture_output=True, check=False, timeout=180)
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
                self.log.write(f"TIMEOUT after 180s\nPARTIAL STDOUT:\n{stdout}\nPARTIAL STDERR:\n{stderr}\n")
                self.log.flush()
                raise RuntimeError(f"CLI command timed out after 180 seconds; inspect public ledger state before retrying: {display}") from exc
            self.log.write(f"EXIT={proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}\n")
            self.log.flush()
            failure = proc.stdout + "\n" + proc.stderr
            if not (
                read_only
                and proc.returncode != 0
                and 'could not query captive core' in failure
                and attempt + 1 < attempts
            ):
                break
            self.log.write("Retrying read-only query after transient captive-core error.\n")
            self.log.flush()
            time.sleep(1)
        for tx_hash in HASH_RE.findall(proc.stdout + "\n" + proc.stderr):
            tx_hash = tx_hash.lower()
            self.last_tx_hashes.append(tx_hash)
            if tx_hash not in self.tx_details:
                self.capture_transaction(tx_hash)
        if expected_exit >= 0 and proc.returncode != expected_exit:
            confirmed_post_submit_error = (
                proc.returncode != 0
                and "Read-only file system" in (proc.stdout + proc.stderr)
                and bool(self.last_tx_hashes)
                and all('"tx_success"' in self.tx_details[h].get("result", "") for h in self.last_tx_hashes)
            )
            if confirmed_post_submit_error:
                self.summary["cli_post_submit_errors"].append({
                    "command": display,
                    "process_exit": proc.returncode,
                    "ledger_result": "success confirmed by stellar tx fetch result",
                })
                self.log.write("NOTE: CLI returned nonzero after confirmed successful submission because its local filesystem write failed. This is preserved; no transaction was retried.\n")
            else:
                raise RuntimeError(
                    f"command exit {proc.returncode}, expected {expected_exit}; see {self.log_path}"
                )
        return proc.returncode, proc.stdout, proc.stderr

    def capture_transaction(self, tx_hash: str) -> None:
        fetched: dict[str, str] = {}
        for kind in ("result", "meta", "events"):
            cmd = self.cli + ["tx", "fetch", kind, "--hash", tx_hash, "--network", "testnet"]
            proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
            output = proc.stdout + ("\nSTDERR:\n" + proc.stderr if proc.stderr else "")
            fetched[kind] = output
            self.log.write(
                f"\n$ stellar tx fetch {kind} --hash {tx_hash} --network testnet\n"
                f"EXIT={proc.returncode}\n{output}\n"
            )
            if proc.returncode != 0 and kind in ("result", "meta"):
                self.log.flush()
                raise RuntimeError(f"could not fetch submitted transaction {tx_hash} {kind}: {output}")
            self.log.flush()
        self.tx_details[tx_hash] = fetched
        self.summary["transactions"].append({"hash": tx_hash, "fetched": list(fetched)})
        (self.report_dir / "transactions.json").write_text(
            json.dumps(self.tx_details, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def invoke(
        self, contract: str, source: str, method: str, *args: str, read_only: bool = False
    ) -> tuple[int, str, str]:
        command = [
                "contract", "invoke", "--contract-id", contract,
                "--source-account", source, "--network", "testnet", "--",
                method, *args,
            ]
        # Stellar CLI 28.1.0 sends successful mutating invocations by default;
        # read-only methods remain simulation-only. `--send` is a global CLI
        # option and is not accepted after the generated method arguments.
        return self.run(command, read_only=read_only)

    @staticmethod
    def json_value(stdout: str, stderr: str = "") -> Any:
        for text in (stdout, stderr):
            for line in reversed(text.splitlines()):
                candidate = line.strip()
                if not candidate:
                    continue
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    continue
        raise RuntimeError(f"no JSON result in CLI output: {stdout!r} {stderr!r}")

    def query(self, contract: str, method: str, *args: str) -> Any:
        _, out, err = self.invoke(
            contract, self.args.buyer_identity, method, *args, read_only=True
        )
        value = self.json_value(out, err)
        if not isinstance(value, (dict, list)):
            raise RuntimeError(f"expected structured result from {method}, got {value!r}")
        return value

    def balance(self, account: str) -> int:
        _, out, err = self.run(
            ["token", "balance", "--id", self.args.sac_id, "--account", account, "--network", "testnet"],
            read_only=True,
        )
        text = (out + "\n" + err).strip().splitlines()
        for line in reversed(text):
            try:
                return int(line.strip())
            except ValueError:
                continue
        raise RuntimeError(f"no raw token balance in CLI output: {out!r} {err!r}")

    def address(self, identity: str) -> str:
        _, out, err = self.run(["keys", "public-key", identity])
        value = (out or err).strip().splitlines()[-1].strip()
        if not re.fullmatch(r"G[A-Z2-7]{55}", value):
            raise RuntimeError(f"identity {identity} did not resolve to a public G-address")
        return value

    def horizon_account_creation(self, address: str) -> dict[str, Any]:
        url = f"{HORIZON}/accounts/{address}/operations?limit=20&order=asc"
        with urllib.request.urlopen(url, timeout=30) as response:
            raw = response.read().decode("utf-8")
        data = json.loads(raw)
        records = data.get("_embedded", {}).get("records", [])
        creation = next((row for row in records if row.get("type") == "create_account" and row.get("account") == address), None)
        if not creation:
            raise RuntimeError(f"Horizon did not show Testnet account creation for {address}")
        tx_hash = creation.get("transaction_hash")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(tx_hash)):
            raise RuntimeError(f"Horizon account creation record had no valid transaction hash for {address}")
        self.capture_transaction(tx_hash.lower())
        return {"address": address, "transaction_hash": tx_hash.lower(), "operation": creation, "source_url": url}

    def existing_trustline_tx(self, address: str, asset: str) -> str | None:
        code, issuer = asset.split(":", 1)
        url = f"{HORIZON}/accounts/{address}/operations?limit=50&order=desc"
        with urllib.request.urlopen(url, timeout=30) as response:
            data = json.load(response)
        for row in data.get("_embedded", {}).get("records", []):
            if (
                row.get("type") == "change_trust"
                and row.get("asset_code") == code
                and row.get("asset_issuer") == issuer
                and row.get("trustor") == address
                and int(row.get("limit", "0").replace(".", "")) > 0
            ):
                return str(row["transaction_hash"]).lower()
        return None

    def setup_asset(self) -> tuple[str, str]:
        issuer = self.address(self.args.issuer_identity)
        buyer = self.address(self.args.buyer_identity)
        supplier = self.address(self.args.supplier_identity)
        self.summary["accounts"] = {"issuer": issuer, "buyer": buyer, "supplier": supplier}
        funding = [
            self.horizon_account_creation(issuer),
            self.horizon_account_creation(buyer),
            self.horizon_account_creation(supplier),
        ]
        self.summary["account_funding"] = funding
        asset = f"PSET:{issuer}"
        self.summary["asset_identity"] = asset
        for identity, address in ((self.args.buyer_identity, buyer), (self.args.supplier_identity, supplier)):
            existing = self.existing_trustline_tx(address, asset)
            if existing:
                self.capture_transaction(existing)
                trust_hash = existing
                status = "existing trustline confirmed from Horizon"
            else:
                self.run([
                    "tx", "new", "change-trust", "--source-account", identity,
                    "--line", asset, "--network", "testnet",
                ])
                trust_hash = self.last_tx_hashes[-1] if self.last_tx_hashes else None
                status = "submitted by Stellar CLI"
            self.summary.setdefault("setup_transaction_hashes", []).append({
                "operation": f"change-trust {identity} {asset}",
                "hash": trust_hash,
                "status": status,
            })
        _, sac_out, sac_err = self.run([
            "contract", "asset", "deploy", "--asset", asset,
            "--source-account", self.args.issuer_identity, "--network", "testnet",
        ])
        sac_matches = re.findall(r"\bC[A-Z2-7]{55}\b", sac_out + "\n" + sac_err)
        if not sac_matches:
            raise RuntimeError("Stellar CLI returned no SAC contract ID")
        sac_id = sac_matches[-1]
        self.args.sac_id = sac_id
        self.summary["sac_id"] = sac_id
        self.summary["setup_transaction_hashes"].append({
            "operation": "deploy Stellar Asset Contract",
            "hash": self.last_tx_hashes[-1] if self.last_tx_hashes else None,
        })
        # Classic payment from the asset issuer creates exactly 1,000 base units
        # on the buyer trustline; XLM is kept separate for fees.
        self.run([
            "tx", "new", "payment", "--source-account", self.args.issuer_identity,
            "--destination", buyer, "--asset", asset, "--amount", "1000", "--network", "testnet",
        ])
        self.summary["setup_transaction_hashes"].append({
            "operation": "classic asset payment issuer to buyer 1000 base units",
            "hash": self.last_tx_hashes[-1] if self.last_tx_hashes else None,
        })
        self.summary["initial_asset_balances"] = {
            "buyer": self.balance(buyer),
            "supplier": self.balance(supplier),
            "contract": self.balance(sac_id),
        }
        assert self.summary["initial_asset_balances"] == {"buyer": 1000, "supplier": 0, "contract": 0}
        _, decimals_out, decimals_err = self.run([
            "token", "decimals", "--id", sac_id, "--network", "testnet",
        ], read_only=True)
        self.summary["asset_decimals"] = (decimals_out or decimals_err).strip()
        if self.summary["asset_decimals"] not in ("7", '"7"'):
            raise RuntimeError(f"unexpected SAC decimals: {self.summary['asset_decimals']!r}")
        return sac_id, buyer

    def deploy(self) -> str:
        _, out, err = self.run(
            [
                "contract", "deploy", "--wasm", str(self.wasm),
                "--source-account", self.args.issuer_identity, "--network", "testnet",
            ]
        )
        matches = re.findall(r"\bC[A-Z2-7]{55}\b", out + "\n" + err)
        if not matches:
            raise RuntimeError(f"contract deploy returned no contract ID; see {self.log_path}")
        return matches[-1]

    def create_po(self, contract: str, po_id: int) -> None:
        self.invoke(
            contract,
            self.args.buyer_identity,
            "create_po",
            "--po_id", str(po_id),
            "--buyer", self.args.buyer_address,
            "--supplier", self.args.supplier_address,
            "--token", self.args.sac_id,
            "--lines", json.dumps(LINES, separators=(",", ":")),
        )
        created = self.query(contract, "get_po", "--po_id", str(po_id))
        assert created["status"] == "Draft" and int(created["total_value"]) == 100

    def fund_po(self, contract: str, po_id: int) -> tuple[int, int, int]:
        buyer_before = self.balance(self.args.buyer_address)
        contract_before = self.balance(contract)
        self.invoke(contract, self.args.supplier_identity, "accept_po", "--po_id", str(po_id))
        self.invoke(contract, self.args.buyer_identity, "fund_po", "--po_id", str(po_id))
        buyer_after = self.balance(self.args.buyer_address)
        contract_after = self.balance(contract)
        assert buyer_before - buyer_after == 100
        assert contract_after - contract_before == 100
        po = self.query(contract, "get_po", "--po_id", str(po_id))
        assert po["status"] == "Funded" and int(po["funded_value"]) == 100
        return buyer_after, contract_after, contract_before

    def submit(self, contract: str, po_id: int, receipt_id: int, quantities: list[dict[str, int]]) -> None:
        self.invoke(
            contract,
            self.args.supplier_identity,
            "submit_receipt",
            "--po_id", str(po_id),
            "--receipt_id", str(receipt_id),
            "--quantities", json.dumps(quantities, separators=(",", ":")),
        )

    def raw_receipt_value(self, contract: str, po_id: int, receipt_id: int) -> tuple[int, dict[str, Any]]:
        receipt = self.query(
            contract, "get_receipt", "--po_id", str(po_id), "--receipt_id", str(receipt_id)
        )
        assert receipt["line_count"] == len(receipt["line_ids"])
        value = 0
        raw_lines = []
        for line_id in receipt["line_ids"]:
            delta = self.query(
                contract, "get_receipt_line", "--po_id", str(po_id),
                "--receipt_id", str(receipt_id), "--line_id", str(line_id),
            )
            line = self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", str(line_id))
            assert delta["line_id"] == line_id
            value += int(delta["quantity_delta"]) * int(line["unit_price"])
            raw_lines.append({"receipt_line": delta, "po_line": line})
        return value, {"receipt": receipt, "raw_lines": raw_lines}

    def settle(self, contract: str, po_id: int, receipt_id: int, expected: int) -> None:
        _, out, err = self.invoke(
            contract, self.args.buyer_identity, "accept_receipt_and_settle",
            "--po_id", str(po_id), "--receipt_id", str(receipt_id),
        )
        result = self.json_value(out, err)
        assert int(result) == expected, (result, expected)

    def run_control(self, contract: str, po_id: int) -> dict[str, Any]:
        self.create_po(contract, po_id)
        buyer_after_fund, contract_after_fund, contract_before_fund = self.fund_po(contract, po_id)
        supplier_before = self.balance(self.args.supplier_address)
        self.submit(contract, po_id, 1, [
            {"line_id": 1, "quantity_delta": 6},
            {"line_id": 2, "quantity_delta": 4},
        ])
        expected_r1, raw_r1 = self.raw_receipt_value(contract, po_id, 1)
        assert expected_r1 == 56
        raw_before_r1 = self.query(contract, "get_po", "--po_id", str(po_id))
        assert raw_before_r1["status"] == "Funded"
        self.settle(contract, po_id, 1, expected_r1)
        after_r1 = self.query(contract, "get_po", "--po_id", str(po_id))
        line_a = self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", "1")
        line_b = self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", "2")
        settled_r1 = self.query(contract, "get_receipt", "--po_id", str(po_id), "--receipt_id", "1")
        assert after_r1["status"] == "Funded"
        assert int(after_r1["paid_value"]) == 56 and int(after_r1["refunded_value"]) == 0
        assert line_a["accepted_quantity"] == 6 and line_b["accepted_quantity"] == 4
        assert settled_r1["status"] == "Settled"
        r1_lines = [line_a, line_b]
        assert self.balance(self.args.buyer_address) == buyer_after_fund
        assert self.balance(self.args.supplier_address) - supplier_before == 56
        assert contract_after_fund - self.balance(contract) == 56
        r1_supplier_after = self.balance(self.args.supplier_address)
        r1_contract_after = self.balance(contract)

        self.submit(contract, po_id, 2, [
            {"line_id": 1, "quantity_delta": 4},
            {"line_id": 2, "quantity_delta": 4},
        ])
        expected_final, raw_final = self.raw_receipt_value(contract, po_id, 2)
        assert expected_final == 44
        supplier_before_r2 = self.balance(self.args.supplier_address)
        contract_before_r2 = self.balance(contract)
        self.settle(contract, po_id, 2, expected_final)
        final_po = self.query(contract, "get_po", "--po_id", str(po_id))
        line_a = self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", "1")
        line_b = self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", "2")
        receipt_final = self.query(contract, "get_receipt", "--po_id", str(po_id), "--receipt_id", "2")
        assert final_po["status"] == "Closed"
        assert int(final_po["funded_value"]) == 100
        assert int(final_po["paid_value"]) == 100
        assert int(final_po["refunded_value"]) == 0
        assert line_a["accepted_quantity"] == 10 and line_b["accepted_quantity"] == 8
        assert receipt_final["status"] == "Settled"
        assert self.balance(self.args.buyer_address) == buyer_after_fund
        assert self.balance(self.args.supplier_address) - supplier_before == 100
        assert self.balance(contract) == contract_before_fund
        assert self.balance(self.args.supplier_address) - supplier_before_r2 == 44
        assert contract_before_r2 - self.balance(contract) == 44
        return {
            "contract_id": contract, "po_id": po_id, "r1_raw": raw_r1,
            "r1_expected_and_paid": expected_r1, "r1_po": after_r1,
            "r1_lines": r1_lines, "r2_raw": raw_final,
            "r2_expected_and_paid": expected_final, "final_po": final_po,
            "final_receipt": receipt_final,
            "token_deltas": {
                "buyer_during_settlement": 0,
                "supplier_for_full_po": self.balance(self.args.supplier_address) - supplier_before,
                "contract_net_change_from_pre_funding": self.balance(contract) - contract_before_fund,
                "contract_after_close": self.balance(contract),
                "r1_supplier_gain": r1_supplier_after - supplier_before,
                "r1_contract_delta": r1_contract_after - contract_after_fund,
                "r2_supplier_gain": self.balance(self.args.supplier_address) - supplier_before_r2,
                "r2_contract_delta": self.balance(contract) - contract_before_r2,
            },
        }

    def run_changed(self, contract: str, po_id: int, already_created: bool = False) -> dict[str, Any]:
        if already_created:
            existing = self.query(contract, "get_po", "--po_id", str(po_id))
            assert existing["status"] == "Draft" and int(existing["total_value"]) == 100
        else:
            self.create_po(contract, po_id)
        buyer_after_fund, contract_after_fund, _ = self.fund_po(contract, po_id)
        supplier_before = self.balance(self.args.supplier_address)
        self.submit(contract, po_id, 1, [
            {"line_id": 1, "quantity_delta": 5},
            {"line_id": 2, "quantity_delta": 4},
        ])
        expected, raw = self.raw_receipt_value(contract, po_id, 1)
        assert expected == 50
        self.settle(contract, po_id, 1, expected)
        po = self.query(contract, "get_po", "--po_id", str(po_id))
        line_a = self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", "1")
        line_b = self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", "2")
        assert int(po["total_value"]) == 100 and int(po["paid_value"]) == 50
        assert po["status"] == "Funded"
        assert line_a["accepted_quantity"] == 5 and line_b["accepted_quantity"] == 4
        assert self.balance(self.args.buyer_address) == buyer_after_fund
        assert self.balance(self.args.supplier_address) - supplier_before == 50
        assert contract_after_fund - self.balance(contract) == 50
        return {
            "contract_id": contract, "po_id": po_id, "raw": raw,
            "expected_and_paid": expected, "po": po,
            "token_deltas": {
                "buyer_during_settlement": 0,
                "supplier_for_receipt": self.balance(self.args.supplier_address) - supplier_before,
                "contract_for_receipt": self.balance(contract) - contract_after_fund,
                "contract_balance_after": self.balance(contract),
            },
        }

    def run_stale(self, contract: str, po_id: int) -> dict[str, Any]:
        self.create_po(contract, po_id)
        self.fund_po(contract, po_id)
        self.submit(contract, po_id, 1, [
            {"line_id": 1, "quantity_delta": 6},
            {"line_id": 2, "quantity_delta": 4},
        ])
        self.submit(contract, po_id, 2, [
            {"line_id": 1, "quantity_delta": 5},
            {"line_id": 2, "quantity_delta": 1},
        ])
        stale_value, stale_raw = self.raw_receipt_value(contract, po_id, 2)
        assert stale_value == 35
        assert 5 <= 10 and 1 <= 8  # valid against initial remaining quantities
        self.settle(contract, po_id, 1, 56)
        po_before = self.query(contract, "get_po", "--po_id", str(po_id))
        line_a_before = self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", "1")
        line_b_before = self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", "2")
        stale_before = self.query(contract, "get_receipt", "--po_id", str(po_id), "--receipt_id", "2")
        balances_before = {
            "buyer": self.balance(self.args.buyer_address),
            "supplier": self.balance(self.args.supplier_address),
            "contract": self.balance(contract),
        }
        assert line_a_before["ordered_quantity"] - line_a_before["accepted_quantity"] == 4
        assert stale_before["status"] == "Submitted"
        assert stale_raw["raw_lines"][0]["receipt_line"]["quantity_delta"] == 5

        # Run the expected failure without converting an infrastructure error into success.
        args = [
            "contract", "invoke", "--contract-id", contract,
            "--source-account", self.args.buyer_identity, "--network", "testnet", "--",
            "accept_receipt_and_settle", "--po_id", str(po_id), "--receipt_id", "2",
        ]
        rc, out, err = self.run(args, expected_exit=-1)
        failure = out + "\n" + err
        assert rc != 0 and EXPECTED_STALE_ERROR in failure, failure
        po_after = self.query(contract, "get_po", "--po_id", str(po_id))
        line_a_after = self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", "1")
        line_b_after = self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", "2")
        stale_after = self.query(contract, "get_receipt", "--po_id", str(po_id), "--receipt_id", "2")
        balances_after = {
            "buyer": self.balance(self.args.buyer_address),
            "supplier": self.balance(self.args.supplier_address),
            "contract": self.balance(contract),
        }
        assert po_after == po_before
        assert line_a_after == line_a_before and line_b_after == line_b_before
        assert stale_after == stale_before and stale_after["status"] == "Submitted"
        assert balances_after == balances_before
        return {
            "contract_id": contract, "po_id": po_id, "stale_value_from_raw": stale_value,
            "pre_post": {
                "po": {"before": po_before, "after": po_after},
                "line_a": {"before": line_a_before, "after": line_a_after},
                "line_b": {"before": line_b_before, "after": line_b_after},
                "receipt": {"before": stale_before, "after": stale_after},
                "balances": {"before": balances_before, "after": balances_after},
                "equal": True,
            },
            "cli_exit": rc,
            "expected_contract_error": "QuantityExceedsRemaining (PoError #14)",
        }

    def resume_control_after_receipt_submission(self, contract: str, po_id: int) -> dict[str, Any]:
        po_before = self.query(contract, "get_po", "--po_id", str(po_id))
        line_a_before = self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", "1")
        line_b_before = self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", "2")
        receipt1 = self.query(contract, "get_receipt", "--po_id", str(po_id), "--receipt_id", "1")
        receipt2 = self.query(contract, "get_receipt", "--po_id", str(po_id), "--receipt_id", "2")
        expected, raw = self.raw_receipt_value(contract, po_id, 2)
        assert expected == 44
        assert po_before["status"] == "Funded" and int(po_before["paid_value"]) == 56
        assert line_a_before["accepted_quantity"] == 6 and line_b_before["accepted_quantity"] == 4
        assert receipt1["status"] == "Settled" and receipt2["status"] == "Submitted"
        supplier_before = self.balance(self.args.supplier_address)
        contract_before = self.balance(contract)
        buyer_before = self.balance(self.args.buyer_address)
        _, out, err = self.invoke(
            contract, self.args.buyer_identity, "accept_receipt_and_settle",
            "--po_id", str(po_id), "--receipt_id", "2",
        )
        returned = self.json_value(out, err)
        assert int(returned) == expected
        final_po = self.query(contract, "get_po", "--po_id", str(po_id))
        final_a = self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", "1")
        final_b = self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", "2")
        final_receipt = self.query(contract, "get_receipt", "--po_id", str(po_id), "--receipt_id", "2")
        assert final_po["status"] == "Closed" and int(final_po["paid_value"]) == 100
        assert int(final_po["funded_value"]) == 100 and int(final_po["refunded_value"]) == 0
        assert final_a["accepted_quantity"] == 10 and final_b["accepted_quantity"] == 8
        assert final_receipt["status"] == "Settled"
        assert self.balance(self.args.supplier_address) - supplier_before == 44
        assert contract_before - self.balance(contract) == 44
        assert self.balance(self.args.buyer_address) == buyer_before
        return {
            "contract_id": contract, "po_id": po_id,
            "r1_expected_and_paid": 56, "r2_expected_and_paid": expected,
            "r1_receipt": receipt1, "r2_raw": raw, "r1_po": po_before,
            "final_po": final_po, "final_receipt": final_receipt,
            "token_deltas": {
                "supplier_for_full_po": 100,
                "r2_supplier_gain": self.balance(self.args.supplier_address) - supplier_before,
                "r2_contract_delta": self.balance(contract) - contract_before,
                "contract_after_close": self.balance(contract),
            },
        }

    def read_completed_control(self, contract: str, po_id: int) -> dict[str, Any]:
        final_po = self.query(contract, "get_po", "--po_id", str(po_id))
        line_a = self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", "1")
        line_b = self.query(contract, "get_line", "--po_id", str(po_id), "--line_id", "2")
        receipt1 = self.query(contract, "get_receipt", "--po_id", str(po_id), "--receipt_id", "1")
        receipt2 = self.query(contract, "get_receipt", "--po_id", str(po_id), "--receipt_id", "2")
        expected1, raw1 = self.raw_receipt_value(contract, po_id, 1)
        expected2, raw2 = self.raw_receipt_value(contract, po_id, 2)
        assert expected1 == 56 and expected2 == 44
        assert final_po["status"] == "Closed" and int(final_po["paid_value"]) == 100
        assert int(final_po["funded_value"]) == 100 and int(final_po["refunded_value"]) == 0
        assert line_a["accepted_quantity"] == 10 and line_b["accepted_quantity"] == 8
        assert receipt1["status"] == "Settled" and receipt2["status"] == "Settled"
        assert self.balance(self.args.supplier_address) == 100
        assert self.balance(contract) == 0
        return {
            "contract_id": contract, "po_id": po_id,
            "r1_expected_and_paid": 56, "r2_expected_and_paid": 44,
            "r1_raw": raw1, "r2_raw": raw2,
            "final_po": final_po, "final_receipt": receipt2,
            "token_deltas": {
                "supplier_for_full_po": 100,
                "contract_net_change_from_pre_funding": 0,
                "contract_after_close": 0,
            },
        }

    def run_all(self) -> None:
        size = self.wasm.stat().st_size
        sha = hashlib.sha256(self.wasm.read_bytes()).hexdigest()
        assert size == EXPECTED_WASM_SIZE and sha == EXPECTED_WASM_SHA
        source = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo_root,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        self.summary["source_sha"] = source
        self.summary["source_tree"] = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=self.repo_root,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        self.summary["execution_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.summary["rustc"] = subprocess.run(["rustc", "+1.95.0", "--version"], text=True, capture_output=True, check=True).stdout.strip()
        self.summary["cargo"] = subprocess.run(["cargo", "+1.95.0", "--version"], text=True, capture_output=True, check=True).stdout.strip()
        self.summary["stellar_cli"] = self.run(["--version"])[1].strip()
        self.summary["wasm"] = {"filename": self.wasm.name, "size": size, "sha256": sha}
        _, network_out, network_err = self.run(["network", "info", "--network", "testnet", "--output", "json"], read_only=True)
        network_info = json.loads(network_out or network_err)
        _, health_out, health_err = self.run(["network", "health", "--network", "testnet", "--output", "json"], read_only=True)
        health = json.loads(health_out or health_err)
        assert network_info.get("passphrase") == "Test SDF Network ; September 2015"
        assert int(network_info.get("protocol_version", 0)) >= 28
        assert health.get("status") == "healthy"
        self.summary["network"] = {"info": network_info, "health": health, "rpc_endpoint": TESTNET_RPC}
        if int(network_info.get("protocol_version", 0)) != 29:
            raise RuntimeError(f"current Testnet protocol changed; review compatibility: {network_info.get('protocol_version')}")
        if self.args.resume_contract:
            self.args.buyer_address = self.address(self.args.buyer_identity)
            self.args.supplier_address = self.address(self.args.supplier_identity)
            self.args.sac_id = self.args.sac_id
            sac_id = self.args.sac_id
            primary = self.args.resume_contract
            po_base = time.time_ns() // 1_000_000 * 10
            self.summary["accounts"] = {
                "issuer": self.address(self.args.issuer_identity),
                "buyer": self.args.buyer_address,
                "supplier": self.args.supplier_address,
            }
            self.summary["account_funding"] = [
                self.horizon_account_creation(self.summary["accounts"][role])
                for role in ("issuer", "buyer", "supplier")
            ]
            self.summary["asset_identity"] = f"PSET:{self.summary['accounts']['issuer']}"
            self.summary["sac_id"] = sac_id
            self.summary["primary_contract"] = primary
            self.summary["resumption_start_balances"] = {
                "buyer": self.balance(self.args.buyer_address),
                "supplier": self.balance(self.args.supplier_address),
                "contract": self.balance(primary),
            }
            if self.args.resume_receipt2_hash:
                self.capture_transaction(self.args.resume_receipt2_hash.lower())
            else:
                raise RuntimeError("--resume-receipt2-hash is required in resume mode")
            if not self.args.resume_changed_po or not self.args.resume_changed_create_hash:
                raise RuntimeError("resume mode requires an existing changed-case draft PO and its creation transaction hash")
            self.capture_transaction(self.args.resume_changed_create_hash.lower())
            self.summary["deployment_transaction_hash"] = self.args.deployment_hash
            if not self.summary["deployment_transaction_hash"]:
                raise RuntimeError("--deployment-hash is required in resume mode")
            _, deployed_hash_out, deployed_hash_err = self.run([
                "contract", "info", "hash", "--contract-id", primary, "--network", "testnet",
            ], read_only=True)
            deployed_hash = (deployed_hash_out or deployed_hash_err).strip().splitlines()[-1].strip().lower()
            if sha not in deployed_hash:
                raise RuntimeError(f"deployed WASM hash does not match local artifact: {deployed_hash}")
            self.summary["deployed_wasm_hash"] = deployed_hash
            _, iface_out, iface_err = self.run([
                "contract", "info", "interface", "--contract-id", primary, "--network", "testnet",
            ], read_only=True)
            self.summary["deployed_interface"] = (iface_out or iface_err).strip()
            control = (
                self.read_completed_control(primary, self.args.resume_control_po)
                if self.args.resume_control_complete
                else self.resume_control_after_receipt_submission(primary, self.args.resume_control_po)
            )
            self.summary["control"] = control
            self.summary["deliberate_change"] = self.run_changed(
                primary, self.args.resume_changed_po, already_created=True,
            )
            self.summary["stale"] = self.run_stale(primary, po_base + 1)
        else:
            sac_id, buyer_address = self.setup_asset()
            self.args.buyer_address = buyer_address
            self.args.supplier_address = self.summary["accounts"]["supplier"]
            self.args.sac_id = sac_id
            buyer_asset_before = self.balance(self.args.buyer_address)
            supplier_asset_before = self.balance(self.args.supplier_address)
            self.summary["asset_before"] = {
                "buyer": buyer_asset_before,
                "supplier": supplier_asset_before,
                "identity": self.args.sac_id,
            }
            po_base = time.time_ns() // 1_000_000 * 10
            primary = self.deploy()
            self.summary["primary_contract"] = primary
            self.summary["deployment_transaction_hash"] = self.last_tx_hashes[-1] if self.last_tx_hashes else None
            if not self.summary["deployment_transaction_hash"]:
                raise RuntimeError("contract deployment transaction hash was not captured from CLI output")
            _, deployed_hash_out, deployed_hash_err = self.run([
                "contract", "info", "hash", "--contract-id", primary, "--network", "testnet",
            ], read_only=True)
            deployed_hash = (deployed_hash_out or deployed_hash_err).strip().splitlines()[-1].strip().lower()
            if sha not in deployed_hash:
                raise RuntimeError(f"deployed WASM hash does not match local artifact: {deployed_hash}")
            self.summary["deployed_wasm_hash"] = deployed_hash
            _, iface_out, iface_err = self.run([
                "contract", "info", "interface", "--contract-id", primary, "--network", "testnet",
            ], read_only=True)
            self.summary["deployed_interface"] = (iface_out or iface_err).strip()
            self.summary["control"] = self.run_control(primary, po_base)
            self.summary["deliberate_change"] = self.run_changed(primary, po_base + 1)
            self.summary["stale"] = self.run_stale(primary, po_base + 2)
        assert self.summary["control"]["r1_expected_and_paid"] == 56
        assert self.summary["control"]["r2_expected_and_paid"] == 44
        assert self.summary["deliberate_change"]["expected_and_paid"] == 50
        self.summary["asset_after"] = {
            "buyer": self.balance(self.args.buyer_address),
            "supplier": self.balance(self.args.supplier_address),
            "contract": self.balance(primary),
        }
        assert self.summary["asset_after"] == {"buyer": 700, "supplier": 206, "contract": 94}, self.summary["asset_after"]
        control = self.summary["control"]
        changed = self.summary["deliberate_change"]
        stale = self.summary["stale"]
        final_summary = {
            "source_sha": source,
            "source_tree": self.summary["source_tree"],
            "execution_utc": self.summary["execution_utc"],
            "wasm_sha256": sha,
            "wasm_size": size,
            "stellar_cli": "28.1.0",
            "testnet_network": self.summary["network"],
            "procuresettle_deployment_id": self.summary["primary_contract"],
            "settlement_sac_id": self.args.sac_id,
            "asset_identity": self.summary["asset_identity"],
            "accounts": self.summary["accounts"],
            "control": {
                "first_payout": control["r1_expected_and_paid"],
                "final_payout": control["r2_expected_and_paid"],
                "status": control["final_po"]["status"],
                "paid": int(control["final_po"]["paid_value"]),
                "refunded": int(control["final_po"]["refunded_value"]),
                "token_deltas": control["token_deltas"],
            },
            "changed_quantity": {
                "payout": changed["expected_and_paid"],
                "token_deltas": changed["token_deltas"],
            },
            "stale_rejection": {
                "process_exit": stale["cli_exit"],
                "reason": stale["expected_contract_error"],
                "pre_post": stale["pre_post"],
            },
            "whole_proof_asset_accounting": self.summary["asset_after"],
            "transaction_hashes": [item["hash"] for item in self.summary["transactions"]],
            "deployment_transaction_hash": self.summary["deployment_transaction_hash"],
            "setup_transaction_hashes": self.summary.get("setup_transaction_hashes", []),
        }
        self.log.write("\nSEMANTIC_SUMMARY_JSON:\n" + json.dumps(self.summary, indent=2, sort_keys=True) + "\n")
        self.log.flush()
        (self.report_dir / "summary.json").write_text(json.dumps(self.summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print("PROCURESETTLE_TESTNET_FLAGSHIP_PROOF_SUMMARY")
        print(json.dumps({"verdict": "PASS", "report": str(self.log_path), **final_summary}, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--wasm", type=Path, default=Path("target/wasm32v1-none/release/procuresettle.wasm"))
    parser.add_argument("--report-dir", type=Path, default=Path(".codex/reports/testnet-live"))
    parser.add_argument("--issuer-identity", required=True)
    parser.add_argument("--buyer-identity", required=True)
    parser.add_argument("--supplier-identity", required=True)
    parser.add_argument("--sac-id")
    parser.add_argument("--resume-contract")
    parser.add_argument("--resume-control-po", type=int)
    parser.add_argument("--resume-receipt2-hash")
    parser.add_argument("--deployment-hash")
    parser.add_argument("--prior-transactions", type=Path)
    parser.add_argument("--resume-changed-po", type=int)
    parser.add_argument("--resume-changed-create-hash")
    parser.add_argument("--resume-control-complete", action="store_true")
    args = parser.parse_args()
    if args.resume_contract and not all((args.sac_id, args.resume_control_po, args.resume_receipt2_hash, args.deployment_hash, args.prior_transactions, args.resume_changed_po, args.resume_changed_create_hash)):
        parser.error("resume mode requires --sac-id, --resume-control-po, --resume-receipt2-hash, --deployment-hash, --prior-transactions, --resume-changed-po, and --resume-changed-create-hash")
    proof = Proof(args)
    try:
        proof.run_all()
    except Exception as exc:  # preserve failure reason and raw CLI log
        print(f"F3_TESTNET_PROOF_FAILED: {exc}", file=sys.stderr)
        print(f"Raw log: {proof.log_path}", file=sys.stderr)
        return 1
    finally:
        proof.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run LineSettle's local CLI proof using pre-funded test identities and an SAC.

Prerequisites: Stellar CLI 28.1.0; a healthy `local` network at protocol 28;
config-dir containing the issuer, buyer, and supplier identities; and a test-only
classic-asset SAC whose buyer balance covers six 100-unit PO fundings. The
script never reads or prints secret keys. All transactions explicitly target
`--network local`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

EXPECTED_WASM_SIZE = 25_478
EXPECTED_WASM_SHA = "e27caede18f9b6c6275d7076b9a671c4964a27b815329ebf399ade0e3770e339"
EXPECTED_STALE_ERROR = "HostError: Error(Contract, #14)"  # PoError::QuantityExceedsRemaining
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
        self.summary: dict[str, Any] = {"repeat_runs": []}

    def close(self) -> None:
        self.log.close()

    def run(
        self, args: list[str], expected_exit: int = 0, read_only: bool = False
    ) -> tuple[int, str, str]:
        cmd = self.cli + args
        attempts = 4 if read_only else 1
        for attempt in range(attempts):
            self.log.write(f"\n$ {shlex.join(cmd)}\n")
            proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
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
        if expected_exit >= 0 and proc.returncode != expected_exit:
            raise RuntimeError(
                f"command exit {proc.returncode}, expected {expected_exit}; see {self.log_path}"
            )
        return proc.returncode, proc.stdout, proc.stderr

    def invoke(
        self, contract: str, source: str, method: str, *args: str, read_only: bool = False
    ) -> tuple[int, str, str]:
        command = [
                "contract", "invoke", "--contract-id", contract,
                "--source-account", source, "--network", "local", "--",
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
            ["token", "balance", "--id", self.args.sac_id, "--account", account, "--network", "local"],
            read_only=True,
        )
        text = (out + "\n" + err).strip().splitlines()
        for line in reversed(text):
            try:
                return int(line.strip())
            except ValueError:
                continue
        raise RuntimeError(f"no raw token balance in CLI output: {out!r} {err!r}")

    def deploy(self) -> str:
        _, out, err = self.run(
            [
                "contract", "deploy", "--wasm", str(self.wasm),
                "--source-account", self.args.issuer_identity, "--network", "local",
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

    def run_changed(self, contract: str, po_id: int) -> dict[str, Any]:
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
            "--source-account", self.args.buyer_identity, "--network", "local", "--",
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

    def run_all(self) -> None:
        size = self.wasm.stat().st_size
        sha = hashlib.sha256(self.wasm.read_bytes()).hexdigest()
        assert size == EXPECTED_WASM_SIZE and sha == EXPECTED_WASM_SHA
        source = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo_root,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        self.summary["source_sha"] = source
        self.summary["stellar_cli"] = self.run(["--version"])[1].strip()
        self.summary["wasm"] = {"path": str(self.wasm), "size": size, "sha256": sha}
        _, network_out, network_err = self.run(["network", "info"], read_only=True)
        network_info = network_out + network_err
        assert "Standalone Network ; February 2017" in network_info
        assert "Protocol Version: 28" in network_info
        self.summary["local_network"] = network_info.strip()
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
        self.summary["control"] = self.run_control(primary, po_base)
        self.summary["deliberate_change"] = self.run_changed(primary, po_base + 1)
        self.summary["stale"] = self.run_stale(primary, po_base + 2)
        assert self.summary["control"]["r1_expected_and_paid"] == 56
        assert self.summary["control"]["r2_expected_and_paid"] == 44
        assert self.summary["deliberate_change"]["expected_and_paid"] == 50
        for run_number in range(1, 4):
            contract = self.deploy()
            result = self.run_control(contract, 1)
            result["execution"] = run_number
            self.summary["repeat_runs"].append(result)
        for result in self.summary["repeat_runs"]:
            assert result["r1_expected_and_paid"] == 56
            assert result["r2_expected_and_paid"] == 44
            assert result["final_po"]["status"] == "Closed"
            assert int(result["final_po"]["paid_value"]) == 100
            assert int(result["final_po"]["refunded_value"]) == 0
        self.summary["asset_after"] = {
            "buyer": self.balance(self.args.buyer_address),
            "supplier": self.balance(self.args.supplier_address),
        }
        control = self.summary["control"]
        changed = self.summary["deliberate_change"]
        stale = self.summary["stale"]
        repeat_summary = [
            {
                "execution": item["execution"], "contract_id": item["contract_id"],
                "first_payout": item["r1_expected_and_paid"],
                "final_payout": item["r2_expected_and_paid"],
                "status": item["final_po"]["status"],
                "paid": int(item["final_po"]["paid_value"]),
                "refunded": int(item["final_po"]["refunded_value"]),
            }
            for item in self.summary["repeat_runs"]
        ]
        final_summary = {
            "source_sha": source,
            "wasm_sha256": sha,
            "wasm_size": size,
            "stellar_cli": "28.1.0",
            "local_network": self.summary["local_network"],
            "linesettle_deployment_id": self.summary["primary_contract"],
            "settlement_sac_id": self.args.sac_id,
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
            "repeat_runs": repeat_summary,
        }
        self.log.write("\nSEMANTIC_SUMMARY_JSON:\n" + json.dumps(self.summary, indent=2, sort_keys=True) + "\n")
        self.log.flush()
        print("LINESETTLE_LOCAL_FLAGSHIP_PROOF_SUMMARY")
        print(json.dumps({"verdict": "PASS", "report": str(self.log_path), **final_summary}, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--wasm", type=Path, default=Path("target/wasm32v1-none/release/linesettle.wasm"))
    parser.add_argument("--report-dir", type=Path, default=Path(".codex/reports/stage5"))
    parser.add_argument("--issuer-identity", required=True)
    parser.add_argument("--buyer-identity", required=True)
    parser.add_argument("--buyer-address", required=True)
    parser.add_argument("--supplier-identity", required=True)
    parser.add_argument("--supplier-address", required=True)
    parser.add_argument("--sac-id", required=True)
    args = parser.parse_args()
    proof = Proof(args)
    try:
        proof.run_all()
    except Exception as exc:  # preserve failure reason and raw CLI log
        print(f"STAGE5_PROOF_FAILED: {exc}", file=sys.stderr)
        print(f"Raw log: {proof.log_path}", file=sys.stderr)
        return 1
    finally:
        proof.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

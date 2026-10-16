use soroban_sdk::{
    contract, contractimpl, contracttype,
    testutils::{Address as _, IssuerFlags, MockAuth, MockAuthInvoke},
    token::{StellarAssetClient, TokenClient},
    Address, Env, IntoVal, Vec,
};

use crate::{
    LineInput, PoSettlement, PoSettlementClient, PoStatus, PurchaseOrder, Receipt,
    ReceiptQuantityInput, ReceiptStatus, MAX_LINES,
};

// Stage 2A technical probe, intentionally test-only and outside the product API.
#[contract]
struct AtomicityProbe;

#[contracttype]
#[derive(Clone)]
enum ProbeKey {
    Marker,
}

#[contractimpl]
impl AtomicityProbe {
    pub fn settle(env: Env, token_address: Address, supplier: Address, amount: i128, marker: u32) {
        let contract = env.current_contract_address();
        env.storage().persistent().set(&ProbeKey::Marker, &marker);
        TokenClient::new(&env, &token_address).transfer(&contract, &supplier, &amount);
    }

    pub fn marker(env: Env) -> u32 {
        env.storage()
            .persistent()
            .get(&ProbeKey::Marker)
            .unwrap_or(0)
    }
}

struct Fixture {
    env: Env,
    contract: Address,
    buyer: Address,
    supplier: Address,
    token: Address,
}

fn fixture() -> Fixture {
    let env = Env::default();
    let buyer = Address::generate(&env);
    let supplier = Address::generate(&env);
    let issuer = Address::generate(&env);
    let token = env.register_stellar_asset_contract_v2(issuer).address();
    let contract = env.register(PoSettlement, ());
    Fixture {
        env,
        contract,
        buyer,
        supplier,
        token,
    }
}

impl Fixture {
    fn client(&self) -> PoSettlementClient<'_> {
        PoSettlementClient::new(&self.env, &self.contract)
    }
}

fn valid_lines(env: &Env) -> Vec<LineInput> {
    soroban_sdk::vec![
        env,
        LineInput {
            line_id: 1,
            ordered_quantity: 10,
            unit_price: 6
        },
        LineInput {
            line_id: 2,
            ordered_quantity: 8,
            unit_price: 5
        },
    ]
}

fn authorized_create(f: &Fixture, po_id: u64, lines: &Vec<LineInput>) {
    let invoke = MockAuthInvoke {
        contract: &f.contract,
        fn_name: "create_po",
        args: (
            po_id,
            f.buyer.clone(),
            f.supplier.clone(),
            f.token.clone(),
            lines.clone(),
        )
            .into_val(&f.env),
        sub_invokes: &[],
    };
    f.client()
        .mock_auths(&[MockAuth {
            address: &f.buyer,
            invoke: &invoke,
        }])
        .create_po(&po_id, &f.buyer, &f.supplier, &f.token, lines);
}

fn authorized_accept(f: &Fixture, po_id: u64) {
    let invoke = MockAuthInvoke {
        contract: &f.contract,
        fn_name: "accept_po",
        args: (po_id,).into_val(&f.env),
        sub_invokes: &[],
    };
    f.client()
        .mock_auths(&[MockAuth {
            address: &f.supplier,
            invoke: &invoke,
        }])
        .accept_po(&po_id);
}

fn header(f: &Fixture, po_id: u64) -> PurchaseOrder {
    f.client().get_po(&po_id)
}

fn create_and_accept(f: &Fixture, po_id: u64, lines: &Vec<LineInput>) {
    authorized_create(f, po_id, lines);
    authorized_accept(f, po_id);
}

fn token_balance(f: &Fixture, address: &Address) -> i128 {
    TokenClient::new(&f.env, &f.token).balance(address)
}

fn create_funded_po(f: &Fixture, po_id: u64, lines: &Vec<LineInput>) {
    let admin = StellarAssetClient::new(&f.env, &f.token);
    admin.mock_all_auths().mint(&f.buyer, &1_000);
    create_and_accept(f, po_id, lines);
    f.client().mock_all_auths().fund_po(&po_id);
}

fn valid_receipt(f: &Fixture) -> Vec<ReceiptQuantityInput> {
    soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 6,
        },
        ReceiptQuantityInput {
            line_id: 2,
            quantity_delta: 4,
        },
    ]
}

fn authorized_submit(
    f: &Fixture,
    po_id: u64,
    receipt_id: u64,
    quantities: &Vec<ReceiptQuantityInput>,
) {
    let invoke = MockAuthInvoke {
        contract: &f.contract,
        fn_name: "submit_receipt",
        args: (po_id, receipt_id, quantities.clone()).into_val(&f.env),
        sub_invokes: &[],
    };
    f.client()
        .mock_auths(&[MockAuth {
            address: &f.supplier,
            invoke: &invoke,
        }])
        .submit_receipt(&po_id, &receipt_id, quantities);
}

fn authorized_settle(f: &Fixture, po_id: u64, receipt_id: u64) -> i128 {
    let invoke = MockAuthInvoke {
        contract: &f.contract,
        fn_name: "accept_receipt_and_settle",
        args: (po_id, receipt_id).into_val(&f.env),
        sub_invokes: &[],
    };
    f.client()
        .mock_auths(&[MockAuth {
            address: &f.buyer,
            invoke: &invoke,
        }])
        .accept_receipt_and_settle(&po_id, &receipt_id)
}

fn funded_flagship(f: &Fixture, po_id: u64) {
    let lines = valid_lines(&f.env);
    create_funded_po(f, po_id, &lines);
}

fn authorized_cancel(f: &Fixture, po_id: u64) {
    let invoke = MockAuthInvoke {
        contract: &f.contract,
        fn_name: "cancel_po",
        args: (po_id,).into_val(&f.env),
        sub_invokes: &[],
    };
    f.client()
        .mock_auths(&[
            MockAuth {
                address: &f.buyer,
                invoke: &invoke,
            },
            MockAuth {
                address: &f.supplier,
                invoke: &invoke,
            },
        ])
        .cancel_po(&po_id);
}

fn assert_po_conservation(f: &Fixture, po_id: u64) {
    let po = header(f, po_id);
    let mut accepted_value = 0_i128;
    let mut i = 0;
    while i < po.line_count {
        let line_id = po.line_ids.get(i).unwrap();
        let line = f.client().get_line(&po_id, &line_id);
        accepted_value = accepted_value
            .checked_add(
                i128::from(line.accepted_quantity)
                    .checked_mul(line.unit_price)
                    .unwrap(),
            )
            .unwrap();
        i += 1;
    }
    assert_eq!(po.paid_value, accepted_value);

    if po.funded_value > 0 {
        let remaining = po
            .funded_value
            .checked_sub(po.paid_value)
            .unwrap()
            .checked_sub(po.refunded_value)
            .unwrap();
        assert!(remaining >= 0);
        assert_eq!(
            po.funded_value,
            po.paid_value
                .checked_add(po.refunded_value)
                .unwrap()
                .checked_add(remaining)
                .unwrap()
        );
        if po.status == PoStatus::Closed {
            assert_eq!(remaining, 0);
            assert_eq!(po.paid_value, po.total_value);
            assert_eq!(po.refunded_value, 0);
            for line_id in po.line_ids.iter() {
                let line = f.client().get_line(&po_id, &line_id);
                assert_eq!(line.accepted_quantity, line.ordered_quantity);
            }
        }
        if po.status == PoStatus::Cancelled {
            assert_eq!(remaining, 0);
            assert_eq!(
                po.funded_value,
                po.paid_value.checked_add(po.refunded_value).unwrap()
            );
        }
    }
}

#[test]
fn valid_po_creation_derives_total_and_stores_terms() {
    let f = fixture();
    let lines = valid_lines(&f.env);
    authorized_create(&f, 41, &lines);

    let po = header(&f, 41);
    assert_eq!(po.po_id, 41);
    assert_eq!(po.buyer, f.buyer);
    assert_eq!(po.supplier, f.supplier);
    assert_eq!(po.token, f.token);
    assert_eq!(po.status, PoStatus::Draft);
    assert_eq!(po.total_value, 100);
    assert_eq!(po.line_count, 2);
    assert_eq!(f.client().get_line(&41, &1).accepted_quantity, 0);
    assert_eq!(f.client().get_line(&41, &1).ordered_quantity, 10);
    assert_eq!(f.client().get_line(&41, &1).unit_price, 6);
    assert_eq!(f.client().get_line(&41, &2).accepted_quantity, 0);
    assert_eq!(f.client().get_line(&41, &2).ordered_quantity, 8);
    assert_eq!(f.client().get_line(&41, &2).unit_price, 5);
}

#[test]
fn supplier_acceptance_changes_only_status_and_second_acceptance_fails() {
    let f = fixture();
    let lines = valid_lines(&f.env);
    authorized_create(&f, 42, &lines);
    let before = header(&f, 42);
    let line_a = f.client().get_line(&42, &1);
    let line_b = f.client().get_line(&42, &2);

    authorized_accept(&f, 42);

    let after = header(&f, 42);
    assert_eq!(after.status, PoStatus::Accepted);
    assert_eq!(after.po_id, before.po_id);
    assert_eq!(after.buyer, before.buyer);
    assert_eq!(after.supplier, before.supplier);
    assert_eq!(after.token, before.token);
    assert_eq!(after.total_value, before.total_value);
    assert_eq!(after.line_count, before.line_count);
    assert_eq!(f.client().get_line(&42, &1), line_a);
    assert_eq!(f.client().get_line(&42, &2), line_b);

    let result = f.client().try_accept_po(&42);
    assert!(result.is_err(), "second acceptance must fail");
    assert_eq!(header(&f, 42), after);
}

#[test]
fn buyer_cannot_substitute_for_supplier_acceptance() {
    let f = fixture();
    let lines = valid_lines(&f.env);
    authorized_create(&f, 43, &lines);
    let before = header(&f, 43);
    let wrong_actor = f.buyer.clone();
    let invoke = MockAuthInvoke {
        contract: &f.contract,
        fn_name: "accept_po",
        args: (43u64,).into_val(&f.env),
        sub_invokes: &[],
    };
    let result = f
        .client()
        .mock_auths(&[MockAuth {
            address: &wrong_actor,
            invoke: &invoke,
        }])
        .try_accept_po(&43);
    assert!(result.is_err());
    assert_eq!(header(&f, 43), before);
}

#[test]
fn unauthorized_creation_leaves_no_po() {
    let f = fixture();
    let lines = valid_lines(&f.env);
    let result = f
        .client()
        .try_create_po(&44, &f.buyer, &f.supplier, &f.token, &lines);
    assert!(result.is_err());
    assert!(f.client().try_get_po(&44).is_err());
    assert!(f.client().try_get_line(&44, &1).is_err());
}

#[test]
fn zero_quantity_is_rejected_without_partial_state() {
    let f = fixture();
    let lines = soroban_sdk::vec![
        &f.env,
        LineInput {
            line_id: 1,
            ordered_quantity: 4,
            unit_price: 2
        },
        LineInput {
            line_id: 2,
            ordered_quantity: 0,
            unit_price: 5
        },
    ];
    let result =
        f.client()
            .mock_all_auths()
            .try_create_po(&45, &f.buyer, &f.supplier, &f.token, &lines);
    assert!(result.is_err());
    assert!(f.client().try_get_po(&45).is_err());
    assert!(f.client().try_get_line(&45, &1).is_err());
}

#[test]
fn zero_price_is_rejected() {
    let f = fixture();
    let lines = soroban_sdk::vec![
        &f.env,
        LineInput {
            line_id: 1,
            ordered_quantity: 1,
            unit_price: 0
        },
    ];
    let result =
        f.client()
            .mock_all_auths()
            .try_create_po(&46, &f.buyer, &f.supplier, &f.token, &lines);
    assert!(result.is_err());
    assert!(f.client().try_get_po(&46).is_err());
}

#[test]
fn empty_line_collection_is_rejected() {
    let f = fixture();
    let lines = Vec::<LineInput>::new(&f.env);
    let result =
        f.client()
            .mock_all_auths()
            .try_create_po(&55, &f.buyer, &f.supplier, &f.token, &lines);
    assert!(result.is_err());
    assert!(f.client().try_get_po(&55).is_err());
}

#[test]
fn duplicate_line_id_is_rejected_without_partial_state() {
    let f = fixture();
    let lines = soroban_sdk::vec![
        &f.env,
        LineInput {
            line_id: 1,
            ordered_quantity: 4,
            unit_price: 2
        },
        LineInput {
            line_id: 1,
            ordered_quantity: 2,
            unit_price: 7
        },
    ];
    let result =
        f.client()
            .mock_all_auths()
            .try_create_po(&47, &f.buyer, &f.supplier, &f.token, &lines);
    assert!(result.is_err());
    assert!(f.client().try_get_po(&47).is_err());
    assert!(f.client().try_get_line(&47, &1).is_err());
}

#[test]
fn duplicate_po_id_does_not_overwrite_header_or_lines() {
    let f = fixture();
    let original_lines = valid_lines(&f.env);
    authorized_create(&f, 48, &original_lines);
    let before_header = header(&f, 48);
    let before_line_a = f.client().get_line(&48, &1);
    let before_line_b = f.client().get_line(&48, &2);
    let replacement = soroban_sdk::vec![
        &f.env,
        LineInput {
            line_id: 1,
            ordered_quantity: 99,
            unit_price: 99
        },
    ];
    let result = f.client().mock_all_auths().try_create_po(
        &48,
        &f.buyer,
        &f.supplier,
        &f.token,
        &replacement,
    );
    assert!(result.is_err());
    assert_eq!(header(&f, 48), before_header);
    assert_eq!(f.client().get_line(&48, &1), before_line_a);
    assert_eq!(f.client().get_line(&48, &2), before_line_b);
}

#[test]
fn multiplication_overflow_is_rejected_before_persistence() {
    let f = fixture();
    let lines = soroban_sdk::vec![
        &f.env,
        LineInput {
            line_id: 1,
            ordered_quantity: u64::MAX,
            unit_price: i128::MAX
        },
    ];
    let result =
        f.client()
            .mock_all_auths()
            .try_create_po(&49, &f.buyer, &f.supplier, &f.token, &lines);
    assert!(result.is_err());
    assert!(f.client().try_get_po(&49).is_err());
    assert!(f.client().try_get_line(&49, &1).is_err());
}

#[test]
fn aggregate_total_overflow_is_rejected_before_persistence() {
    let f = fixture();
    let lines = soroban_sdk::vec![
        &f.env,
        LineInput {
            line_id: 1,
            ordered_quantity: 1,
            unit_price: i128::MAX
        },
        LineInput {
            line_id: 2,
            ordered_quantity: 1,
            unit_price: 1
        },
    ];
    let result =
        f.client()
            .mock_all_auths()
            .try_create_po(&50, &f.buyer, &f.supplier, &f.token, &lines);
    assert!(result.is_err());
    assert!(f.client().try_get_po(&50).is_err());
    assert!(f.client().try_get_line(&50, &1).is_err());
}

#[test]
fn line_count_limit_is_enforced_at_and_above_boundary() {
    let f = fixture();
    let mut max_lines = Vec::new(&f.env);
    let mut i = 0;
    while i < MAX_LINES {
        max_lines.push_back(LineInput {
            line_id: i + 1,
            ordered_quantity: 1,
            unit_price: 1,
        });
        i += 1;
    }
    f.client()
        .mock_all_auths()
        .create_po(&51, &f.buyer, &f.supplier, &f.token, &max_lines);
    assert_eq!(header(&f, 51).line_count, MAX_LINES);
    assert_eq!(header(&f, 51).total_value, i128::from(MAX_LINES));

    max_lines.push_back(LineInput {
        line_id: MAX_LINES + 1,
        ordered_quantity: 1,
        unit_price: 1,
    });
    let result =
        f.client()
            .mock_all_auths()
            .try_create_po(&52, &f.buyer, &f.supplier, &f.token, &max_lines);
    assert!(result.is_err());
    assert!(f.client().try_get_po(&52).is_err());
    assert!(f.client().try_get_line(&52, &1).is_err());
}

#[test]
fn buyer_cannot_be_its_own_supplier() {
    let f = fixture();
    let lines = valid_lines(&f.env);
    let result = f
        .client()
        .mock_all_auths()
        .try_create_po(&53, &f.buyer, &f.buyer, &f.token, &lines);
    assert!(result.is_err());
    assert!(f.client().try_get_po(&53).is_err());
}

#[test]
fn zero_po_and_line_ids_are_rejected() {
    let f = fixture();
    let lines = soroban_sdk::vec![
        &f.env,
        LineInput {
            line_id: 0,
            ordered_quantity: 1,
            unit_price: 1
        },
    ];
    let line_result =
        f.client()
            .mock_all_auths()
            .try_create_po(&54, &f.buyer, &f.supplier, &f.token, &lines);
    assert!(line_result.is_err());
    assert!(f.client().try_get_po(&54).is_err());

    let valid = valid_lines(&f.env);
    let po_result =
        f.client()
            .mock_all_auths()
            .try_create_po(&0, &f.buyer, &f.supplier, &f.token, &valid);
    assert!(po_result.is_err());
    assert!(f.client().try_get_po(&0).is_err());
}

#[test]
fn successful_funding_transfers_exact_stored_total_and_preserves_terms() {
    let f = fixture();
    let lines = valid_lines(&f.env);
    let admin = StellarAssetClient::new(&f.env, &f.token);
    admin.mock_all_auths().mint(&f.buyer, &500);
    // A pre-existing pooled balance is not attributed to this PO.
    admin.mock_all_auths().mint(&f.contract, &25);
    create_and_accept(&f, 60, &lines);
    let before_header = header(&f, 60);
    let before_a = f.client().get_line(&60, &1);
    let before_b = f.client().get_line(&60, &2);
    assert_eq!(before_header.funded_value, 0);
    assert_eq!(token_balance(&f, &f.buyer), 500);
    assert_eq!(token_balance(&f, &f.contract), 25);

    f.client().mock_all_auths().fund_po(&60);

    let after = header(&f, 60);
    assert_eq!(token_balance(&f, &f.buyer), 400);
    assert_eq!(token_balance(&f, &f.contract), 125);
    assert_eq!(after.status, PoStatus::Funded);
    assert_eq!(after.funded_value, 100);
    assert_eq!(after.total_value, 100);
    assert_eq!(after.po_id, before_header.po_id);
    assert_eq!(after.buyer, before_header.buyer);
    assert_eq!(after.supplier, before_header.supplier);
    assert_eq!(after.token, before_header.token);
    assert_eq!(after.line_count, before_header.line_count);
    assert_eq!(f.client().get_line(&60, &1), before_a);
    assert_eq!(f.client().get_line(&60, &2), before_b);
}

#[test]
fn funding_before_acceptance_fails_without_moving_tokens_or_state() {
    let f = fixture();
    let lines = valid_lines(&f.env);
    let admin = StellarAssetClient::new(&f.env, &f.token);
    admin.mock_all_auths().mint(&f.buyer, &500);
    authorized_create(&f, 61, &lines);
    let before_buyer = token_balance(&f, &f.buyer);
    let before_contract = token_balance(&f, &f.contract);

    assert!(f.client().mock_all_auths().try_fund_po(&61).is_err());

    let po = header(&f, 61);
    assert_eq!(po.status, PoStatus::Draft);
    assert_eq!(po.funded_value, 0);
    assert_eq!(token_balance(&f, &f.buyer), before_buyer);
    assert_eq!(token_balance(&f, &f.contract), before_contract);
}

#[test]
fn unauthorized_actor_cannot_fund_for_recorded_buyer() {
    let f = fixture();
    let lines = valid_lines(&f.env);
    let admin = StellarAssetClient::new(&f.env, &f.token);
    admin.mock_all_auths().mint(&f.buyer, &500);
    create_and_accept(&f, 62, &lines);
    let before_buyer = token_balance(&f, &f.buyer);
    let before_contract = token_balance(&f, &f.contract);
    let wrong_actor = Address::generate(&f.env);
    let invoke = MockAuthInvoke {
        contract: &f.contract,
        fn_name: "fund_po",
        args: (62u64,).into_val(&f.env),
        sub_invokes: &[],
    };

    let result = f
        .client()
        .mock_auths(&[MockAuth {
            address: &wrong_actor,
            invoke: &invoke,
        }])
        .try_fund_po(&62);

    assert!(result.is_err(), "buyer authorization must be required");
    let po = header(&f, 62);
    assert_eq!(po.status, PoStatus::Accepted);
    assert_eq!(po.funded_value, 0);
    assert_eq!(token_balance(&f, &f.buyer), before_buyer);
    assert_eq!(token_balance(&f, &f.contract), before_contract);
}

#[test]
fn insufficient_buyer_balance_cannot_leave_false_funded_state() {
    let f = fixture();
    let lines = valid_lines(&f.env);
    let admin = StellarAssetClient::new(&f.env, &f.token);
    admin.mock_all_auths().mint(&f.buyer, &99);
    create_and_accept(&f, 63, &lines);
    let before_buyer = token_balance(&f, &f.buyer);
    let before_contract = token_balance(&f, &f.contract);

    let result = f.client().mock_all_auths().try_fund_po(&63);

    assert!(
        result.is_err(),
        "SAC transfer must fail for insufficient balance"
    );
    let po = header(&f, 63);
    assert_eq!(po.status, PoStatus::Accepted);
    assert_eq!(po.funded_value, 0);
    assert_eq!(token_balance(&f, &f.buyer), before_buyer);
    assert_eq!(token_balance(&f, &f.contract), before_contract);
}

#[test]
fn duplicate_funding_cannot_charge_buyer_twice() {
    let f = fixture();
    let lines = valid_lines(&f.env);
    let admin = StellarAssetClient::new(&f.env, &f.token);
    admin.mock_all_auths().mint(&f.buyer, &500);
    create_and_accept(&f, 64, &lines);
    f.client().mock_all_auths().fund_po(&64);
    let buyer_after_first = token_balance(&f, &f.buyer);
    let contract_after_first = token_balance(&f, &f.contract);

    assert!(f.client().mock_all_auths().try_fund_po(&64).is_err());

    let po = header(&f, 64);
    assert_eq!(po.status, PoStatus::Funded);
    assert_eq!(po.funded_value, po.total_value);
    assert_eq!(token_balance(&f, &f.buyer), buyer_after_first);
    assert_eq!(token_balance(&f, &f.contract), contract_after_first);
}

#[test]
fn unknown_po_funding_fails_without_token_changes() {
    let f = fixture();
    let admin = StellarAssetClient::new(&f.env, &f.token);
    admin.mock_all_auths().mint(&f.buyer, &500);
    let before_buyer = token_balance(&f, &f.buyer);
    let before_contract = token_balance(&f, &f.contract);

    assert!(f.client().mock_all_auths().try_fund_po(&999).is_err());

    assert_eq!(token_balance(&f, &f.buyer), before_buyer);
    assert_eq!(token_balance(&f, &f.contract), before_contract);
}

#[test]
fn multiple_pos_keep_funding_accounting_independent() {
    let f = fixture();
    let admin = StellarAssetClient::new(&f.env, &f.token);
    admin.mock_all_auths().mint(&f.buyer, &500);
    let x = valid_lines(&f.env);
    let y = soroban_sdk::vec![
        &f.env,
        LineInput {
            line_id: 1,
            ordered_quantity: 3,
            unit_price: 7,
        },
    ];
    create_and_accept(&f, 65, &x);
    create_and_accept(&f, 66, &y);
    let before_contract = token_balance(&f, &f.contract);

    f.client().mock_all_auths().fund_po(&65);

    assert_eq!(header(&f, 65).status, PoStatus::Funded);
    assert_eq!(header(&f, 65).funded_value, 100);
    assert_eq!(header(&f, 66).status, PoStatus::Accepted);
    assert_eq!(header(&f, 66).funded_value, 0);
    assert_eq!(token_balance(&f, &f.contract), before_contract + 100);

    f.client().mock_all_auths().fund_po(&66);
    assert_eq!(header(&f, 65).funded_value, 100);
    assert_eq!(header(&f, 66).funded_value, 21);
    assert_eq!(header(&f, 66).status, PoStatus::Funded);
    assert_eq!(token_balance(&f, &f.contract), before_contract + 121);
}

#[test]
fn valid_receipt_submission_only_records_supplier_assertion() {
    let f = fixture();
    funded_flagship(&f, 70);
    let quantities = valid_receipt(&f);
    let buyer_before = token_balance(&f, &f.buyer);
    let contract_before = token_balance(&f, &f.contract);
    let po_before = header(&f, 70);
    authorized_submit(&f, 70, 1, &quantities);

    let receipt = f.client().get_receipt(&70, &1);
    assert_eq!(receipt.status, ReceiptStatus::Submitted);
    assert_eq!(receipt.line_count, 2);
    assert_eq!(f.client().get_receipt_line(&70, &1, &1).quantity_delta, 6);
    assert_eq!(f.client().get_receipt_line(&70, &1, &2).quantity_delta, 4);
    assert_eq!(header(&f, 70), po_before);
    assert_eq!(header(&f, 70).status, PoStatus::Funded);
    assert_eq!(header(&f, 70).funded_value, 100);
    assert_eq!(f.client().get_line(&70, &1).accepted_quantity, 0);
    assert_eq!(f.client().get_line(&70, &2).accepted_quantity, 0);
    assert_eq!(token_balance(&f, &f.buyer), buyer_before);
    assert_eq!(token_balance(&f, &f.contract), contract_before);
    assert_eq!(token_balance(&f, &f.supplier), 0);
}

#[test]
fn receipt_preflight_calculates_runtime_value_56() {
    let f = fixture();
    funded_flagship(&f, 71);
    let quantities = valid_receipt(&f);
    authorized_submit(&f, 71, 1, &quantities);

    assert_eq!(f.client().receipt_value(&71, &1), 56);
}

#[test]
fn non_supplier_cannot_submit_receipt() {
    let f = fixture();
    funded_flagship(&f, 72);
    let quantities = valid_receipt(&f);
    let po_before = header(&f, 72);
    let buyer_before = token_balance(&f, &f.buyer);
    let contract_before = token_balance(&f, &f.contract);
    let wrong_actor = Address::generate(&f.env);
    let invoke = MockAuthInvoke {
        contract: &f.contract,
        fn_name: "submit_receipt",
        args: (72u64, 1u64, quantities.clone()).into_val(&f.env),
        sub_invokes: &[],
    };

    let result = f
        .client()
        .mock_auths(&[MockAuth {
            address: &wrong_actor,
            invoke: &invoke,
        }])
        .try_submit_receipt(&72, &1, &quantities);

    assert!(result.is_err());
    assert!(f.client().try_get_receipt(&72, &1).is_err());
    assert_eq!(header(&f, 72), po_before);
    assert_eq!(f.client().get_line(&72, &1).accepted_quantity, 0);
    assert_eq!(token_balance(&f, &f.buyer), buyer_before);
    assert_eq!(token_balance(&f, &f.contract), contract_before);
}

#[test]
fn receipt_submission_before_funding_is_rejected() {
    let f = fixture();
    let lines = valid_lines(&f.env);
    create_and_accept(&f, 73, &lines);
    let quantities = valid_receipt(&f);

    let result = f
        .client()
        .mock_all_auths()
        .try_submit_receipt(&73, &1, &quantities);

    assert!(result.is_err());
    assert!(f.client().try_get_receipt(&73, &1).is_err());
    assert_eq!(header(&f, 73).status, PoStatus::Accepted);
}

#[test]
fn duplicate_receipt_id_preserves_original_receipt() {
    let f = fixture();
    funded_flagship(&f, 74);
    let original = valid_receipt(&f);
    authorized_submit(&f, 74, 1, &original);
    let before_header: Receipt = f.client().get_receipt(&74, &1);
    let before_a = f.client().get_receipt_line(&74, &1, &1);
    let before_b = f.client().get_receipt_line(&74, &1, &2);
    let replacement = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 2,
        },
    ];

    let result = f
        .client()
        .mock_all_auths()
        .try_submit_receipt(&74, &1, &replacement);

    assert!(result.is_err());
    assert_eq!(f.client().get_receipt(&74, &1), before_header);
    assert_eq!(f.client().get_receipt_line(&74, &1, &1), before_a);
    assert_eq!(f.client().get_receipt_line(&74, &1, &2), before_b);
}

#[test]
fn receipt_ids_are_scoped_per_po() {
    let f = fixture();
    let lines = valid_lines(&f.env);
    create_funded_po(&f, 75, &lines);
    create_funded_po(&f, 76, &lines);
    let quantities = valid_receipt(&f);

    authorized_submit(&f, 75, 1, &quantities);
    authorized_submit(&f, 76, 1, &quantities);

    assert_eq!(f.client().get_receipt(&75, &1).po_id, 75);
    assert_eq!(f.client().get_receipt(&76, &1).po_id, 76);
}

#[test]
fn empty_receipt_is_rejected_without_persistence() {
    let f = fixture();
    funded_flagship(&f, 77);
    let quantities = Vec::<ReceiptQuantityInput>::new(&f.env);

    let result = f
        .client()
        .mock_all_auths()
        .try_submit_receipt(&77, &1, &quantities);

    assert!(result.is_err());
    assert!(f.client().try_get_receipt(&77, &1).is_err());
}

#[test]
fn zero_receipt_id_is_rejected_and_receipt_lines_are_bounded_by_po() {
    let f = fixture();
    funded_flagship(&f, 90);
    let quantities = valid_receipt(&f);
    let zero_id = f
        .client()
        .mock_all_auths()
        .try_submit_receipt(&90, &0, &quantities);
    assert!(zero_id.is_err());
    assert!(f.client().try_get_receipt(&90, &0).is_err());

    let too_many = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 1,
        },
        ReceiptQuantityInput {
            line_id: 2,
            quantity_delta: 1,
        },
        ReceiptQuantityInput {
            line_id: 3,
            quantity_delta: 1,
        },
    ];
    let result = f
        .client()
        .mock_all_auths()
        .try_submit_receipt(&90, &1, &too_many);
    assert!(result.is_err());
    assert!(f.client().try_get_receipt(&90, &1).is_err());
}

#[test]
fn zero_receipt_quantity_is_rejected_without_persistence() {
    let f = fixture();
    funded_flagship(&f, 78);
    let quantities = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 0,
        },
    ];

    let result = f
        .client()
        .mock_all_auths()
        .try_submit_receipt(&78, &1, &quantities);

    assert!(result.is_err());
    assert!(f.client().try_get_receipt(&78, &1).is_err());
}

#[test]
fn duplicate_line_in_receipt_is_rejected_without_partial_state() {
    let f = fixture();
    funded_flagship(&f, 79);
    let quantities = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 2,
        },
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 3,
        },
    ];

    let result = f
        .client()
        .mock_all_auths()
        .try_submit_receipt(&79, &1, &quantities);

    assert!(result.is_err());
    assert!(f.client().try_get_receipt(&79, &1).is_err());
    assert!(f.client().try_get_receipt_line(&79, &1, &1).is_err());
}

#[test]
fn unknown_po_line_is_rejected_without_receipt_state() {
    let f = fixture();
    funded_flagship(&f, 80);
    let quantities = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 3,
            quantity_delta: 1,
        },
    ];

    let result = f
        .client()
        .mock_all_auths()
        .try_submit_receipt(&80, &1, &quantities);

    assert!(result.is_err());
    assert!(f.client().try_get_receipt(&80, &1).is_err());
}

#[test]
fn oversized_receipt_quantity_is_rejected_without_state_changes() {
    let f = fixture();
    funded_flagship(&f, 81);
    let quantities = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 11,
        },
    ];
    let po_before = header(&f, 81);
    let buyer_before = token_balance(&f, &f.buyer);
    let contract_before = token_balance(&f, &f.contract);

    let result = f
        .client()
        .mock_all_auths()
        .try_submit_receipt(&81, &1, &quantities);

    assert!(result.is_err());
    assert!(f.client().try_get_receipt(&81, &1).is_err());
    assert_eq!(header(&f, 81), po_before);
    assert_eq!(f.client().get_line(&81, &1).accepted_quantity, 0);
    assert_eq!(token_balance(&f, &f.buyer), buyer_before);
    assert_eq!(token_balance(&f, &f.contract), contract_before);
}

#[test]
fn full_line_quantity_boundary_preflights_to_total_value() {
    let f = fixture();
    funded_flagship(&f, 82);
    let quantities = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 10,
        },
        ReceiptQuantityInput {
            line_id: 2,
            quantity_delta: 8,
        },
    ];
    authorized_submit(&f, 82, 1, &quantities);

    assert_eq!(f.client().receipt_value(&82, &1), 100);
    assert_eq!(f.client().get_line(&82, &1).accepted_quantity, 0);
    assert_eq!(f.client().get_line(&82, &2).accepted_quantity, 0);
    assert_eq!(header(&f, 82).funded_value, 100);
}

#[test]
fn buyer_rejection_only_changes_receipt_status() {
    let f = fixture();
    funded_flagship(&f, 83);
    let quantities = valid_receipt(&f);
    authorized_submit(&f, 83, 1, &quantities);
    let po_before = header(&f, 83);
    let line_a = f.client().get_line(&83, &1);
    let line_b = f.client().get_line(&83, &2);
    let buyer_before = token_balance(&f, &f.buyer);
    let supplier_before = token_balance(&f, &f.supplier);
    let contract_before = token_balance(&f, &f.contract);

    let invoke = MockAuthInvoke {
        contract: &f.contract,
        fn_name: "reject_receipt",
        args: (83u64, 1u64).into_val(&f.env),
        sub_invokes: &[],
    };
    f.client()
        .mock_auths(&[MockAuth {
            address: &f.buyer,
            invoke: &invoke,
        }])
        .reject_receipt(&83, &1);

    assert_eq!(
        f.client().get_receipt(&83, &1).status,
        ReceiptStatus::Rejected
    );
    assert_eq!(header(&f, 83), po_before);
    assert_eq!(f.client().get_line(&83, &1), line_a);
    assert_eq!(f.client().get_line(&83, &2), line_b);
    assert_eq!(token_balance(&f, &f.buyer), buyer_before);
    assert_eq!(token_balance(&f, &f.supplier), supplier_before);
    assert_eq!(token_balance(&f, &f.contract), contract_before);
}

#[test]
fn unauthorized_rejection_leaves_receipt_and_po_unchanged() {
    let f = fixture();
    funded_flagship(&f, 84);
    let quantities = valid_receipt(&f);
    authorized_submit(&f, 84, 1, &quantities);
    let po_before = header(&f, 84);
    let wrong_actor = Address::generate(&f.env);
    let invoke = MockAuthInvoke {
        contract: &f.contract,
        fn_name: "reject_receipt",
        args: (84u64, 1u64).into_val(&f.env),
        sub_invokes: &[],
    };

    let result = f
        .client()
        .mock_auths(&[MockAuth {
            address: &wrong_actor,
            invoke: &invoke,
        }])
        .try_reject_receipt(&84, &1);

    assert!(result.is_err());
    assert_eq!(
        f.client().get_receipt(&84, &1).status,
        ReceiptStatus::Submitted
    );
    assert_eq!(header(&f, 84), po_before);
    assert_eq!(f.client().get_line(&84, &1).accepted_quantity, 0);
}

#[test]
fn repeated_rejection_fails_without_changing_rejected_receipt() {
    let f = fixture();
    funded_flagship(&f, 85);
    let quantities = valid_receipt(&f);
    authorized_submit(&f, 85, 1, &quantities);
    let invoke = MockAuthInvoke {
        contract: &f.contract,
        fn_name: "reject_receipt",
        args: (85u64, 1u64).into_val(&f.env),
        sub_invokes: &[],
    };
    f.client()
        .mock_auths(&[MockAuth {
            address: &f.buyer,
            invoke: &invoke,
        }])
        .reject_receipt(&85, &1);
    let rejected = f.client().get_receipt(&85, &1);

    let result = f
        .client()
        .mock_auths(&[MockAuth {
            address: &f.buyer,
            invoke: &invoke,
        }])
        .try_reject_receipt(&85, &1);

    assert!(result.is_err());
    assert_eq!(f.client().get_receipt(&85, &1), rejected);
}

#[test]
fn rejected_receipt_id_remains_consumed() {
    let f = fixture();
    funded_flagship(&f, 86);
    let quantities = valid_receipt(&f);
    authorized_submit(&f, 86, 1, &quantities);
    let invoke = MockAuthInvoke {
        contract: &f.contract,
        fn_name: "reject_receipt",
        args: (86u64, 1u64).into_val(&f.env),
        sub_invokes: &[],
    };
    f.client()
        .mock_auths(&[MockAuth {
            address: &f.buyer,
            invoke: &invoke,
        }])
        .reject_receipt(&86, &1);
    let rejected = f.client().get_receipt(&86, &1);
    let replacement = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 1,
        },
    ];

    let result = f
        .client()
        .mock_all_auths()
        .try_submit_receipt(&86, &1, &replacement);

    assert!(result.is_err());
    assert_eq!(f.client().get_receipt(&86, &1), rejected);
}

#[test]
fn receipt_preflight_is_read_only() {
    let f = fixture();
    funded_flagship(&f, 87);
    let quantities = valid_receipt(&f);
    authorized_submit(&f, 87, 1, &quantities);
    let po_before = header(&f, 87);
    let line_a = f.client().get_line(&87, &1);
    let line_b = f.client().get_line(&87, &2);
    let receipt = f.client().get_receipt(&87, &1);
    let receipt_a = f.client().get_receipt_line(&87, &1, &1);
    let receipt_b = f.client().get_receipt_line(&87, &1, &2);
    let buyer_before = token_balance(&f, &f.buyer);
    let supplier_before = token_balance(&f, &f.supplier);
    let contract_before = token_balance(&f, &f.contract);

    assert_eq!(f.client().receipt_value(&87, &1), 56);

    assert_eq!(header(&f, 87), po_before);
    assert_eq!(f.client().get_line(&87, &1), line_a);
    assert_eq!(f.client().get_line(&87, &2), line_b);
    assert_eq!(f.client().get_receipt(&87, &1), receipt);
    assert_eq!(f.client().get_receipt_line(&87, &1, &1), receipt_a);
    assert_eq!(f.client().get_receipt_line(&87, &1, &2), receipt_b);
    assert_eq!(token_balance(&f, &f.buyer), buyer_before);
    assert_eq!(token_balance(&f, &f.supplier), supplier_before);
    assert_eq!(token_balance(&f, &f.contract), contract_before);
}

#[test]
fn preflight_accepts_maximum_reachable_po_value_without_overflow() {
    let f = fixture();
    let lines = soroban_sdk::vec![
        &f.env,
        LineInput {
            line_id: 1,
            ordered_quantity: 1,
            unit_price: i128::MAX,
        },
    ];
    let admin = StellarAssetClient::new(&f.env, &f.token);
    admin.mock_all_auths().mint(&f.buyer, &i128::MAX);
    create_and_accept(&f, 88, &lines);
    f.client().mock_all_auths().fund_po(&88);
    let quantities = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 1,
        },
    ];
    authorized_submit(&f, 88, 1, &quantities);

    assert_eq!(f.client().receipt_value(&88, &1), i128::MAX);
}

#[test]
fn submitted_receipts_do_not_reserve_quantities_and_preflight_is_current_state_only() {
    let f = fixture();
    funded_flagship(&f, 89);
    let receipt_a = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 6,
        },
    ];
    let receipt_b = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 4,
        },
    ];
    authorized_submit(&f, 89, 1, &receipt_a);
    authorized_submit(&f, 89, 2, &receipt_b);

    assert_eq!(f.client().receipt_value(&89, &1), 36);
    assert_eq!(f.client().receipt_value(&89, &2), 24);
    assert_eq!(f.client().get_line(&89, &1).accepted_quantity, 0);
    assert_eq!(header(&f, 89).funded_value, 100);
    assert_eq!(header(&f, 89).status, PoStatus::Funded);
}

#[test]
fn flagship_partial_and_final_receipts_settle_quantity_derived_values() {
    let f = fixture();
    funded_flagship(&f, 90);
    let initial_buyer = token_balance(&f, &f.buyer);
    let initial_supplier = token_balance(&f, &f.supplier);
    let initial_contract = token_balance(&f, &f.contract);
    let r1 = valid_receipt(&f);
    authorized_submit(&f, 90, 1, &r1);

    assert_eq!(authorized_settle(&f, 90, 1), 56);
    assert_eq!(
        f.client().get_receipt(&90, &1).status,
        ReceiptStatus::Settled
    );
    assert_eq!(f.client().get_line(&90, &1).accepted_quantity, 6);
    assert_eq!(f.client().get_line(&90, &2).accepted_quantity, 4);
    assert_eq!(header(&f, 90).paid_value, 56);
    assert_eq!(header(&f, 90).funded_value, 100);
    assert_eq!(header(&f, 90).status, PoStatus::Funded);
    assert_eq!(token_balance(&f, &f.buyer), initial_buyer);
    assert_eq!(token_balance(&f, &f.supplier), initial_supplier + 56);
    assert_eq!(token_balance(&f, &f.contract), initial_contract - 56);
    assert_eq!(f.client().receipt_value(&90, &1), 56);
    let accepted_value = f.client().get_line(&90, &1).accepted_quantity as i128 * 6
        + f.client().get_line(&90, &2).accepted_quantity as i128 * 5;
    assert_eq!(header(&f, 90).paid_value, accepted_value);

    let r2 = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 4
        },
        ReceiptQuantityInput {
            line_id: 2,
            quantity_delta: 4
        },
    ];
    authorized_submit(&f, 90, 2, &r2);
    assert_eq!(authorized_settle(&f, 90, 2), 44);
    assert_eq!(
        f.client().get_receipt(&90, &2).status,
        ReceiptStatus::Settled
    );
    assert_eq!(f.client().get_line(&90, &1).accepted_quantity, 10);
    assert_eq!(f.client().get_line(&90, &2).accepted_quantity, 8);
    assert_eq!(header(&f, 90).paid_value, 100);
    assert_eq!(header(&f, 90).funded_value, 100);
    assert_eq!(token_balance(&f, &f.supplier), initial_supplier + 100);
    assert_eq!(token_balance(&f, &f.contract), initial_contract - 100);
    assert_eq!(header(&f, 90).paid_value, 10 * 6 + 8 * 5);
}

#[test]
fn only_recorded_buyer_can_settle_and_rejected_receipt_cannot_settle() {
    let f = fixture();
    funded_flagship(&f, 91);
    let quantities = valid_receipt(&f);
    authorized_submit(&f, 91, 1, &quantities);
    let po_before = header(&f, 91);
    let lines_before = (f.client().get_line(&91, &1), f.client().get_line(&91, &2));
    let balances_before = (
        token_balance(&f, &f.buyer),
        token_balance(&f, &f.supplier),
        token_balance(&f, &f.contract),
    );
    let invoke = MockAuthInvoke {
        contract: &f.contract,
        fn_name: "accept_receipt_and_settle",
        args: (91u64, 1u64).into_val(&f.env),
        sub_invokes: &[],
    };
    let wrong_actor = Address::generate(&f.env);
    let result = f
        .client()
        .mock_auths(&[MockAuth {
            address: &wrong_actor,
            invoke: &invoke,
        }])
        .try_accept_receipt_and_settle(&91, &1);
    assert!(result.is_err());
    assert_eq!(
        f.client().get_receipt(&91, &1).status,
        ReceiptStatus::Submitted
    );
    assert_eq!(header(&f, 91), po_before);
    assert_eq!(
        (f.client().get_line(&91, &1), f.client().get_line(&91, &2)),
        lines_before
    );
    assert_eq!(
        (
            token_balance(&f, &f.buyer),
            token_balance(&f, &f.supplier),
            token_balance(&f, &f.contract)
        ),
        balances_before
    );

    let reject_invoke = MockAuthInvoke {
        contract: &f.contract,
        fn_name: "reject_receipt",
        args: (91u64, 1u64).into_val(&f.env),
        sub_invokes: &[],
    };
    f.client()
        .mock_auths(&[MockAuth {
            address: &f.buyer,
            invoke: &reject_invoke,
        }])
        .reject_receipt(&91, &1);
    let rejected_header = header(&f, 91);
    let rejected_lines = (f.client().get_line(&91, &1), f.client().get_line(&91, &2));
    let result = f
        .client()
        .mock_all_auths()
        .try_accept_receipt_and_settle(&91, &1);
    assert!(result.is_err());
    assert_eq!(
        f.client().get_receipt(&91, &1).status,
        ReceiptStatus::Rejected
    );
    assert_eq!(header(&f, 91), rejected_header);
    assert_eq!(
        (f.client().get_line(&91, &1), f.client().get_line(&91, &2)),
        rejected_lines
    );
    assert_eq!(
        (
            token_balance(&f, &f.buyer),
            token_balance(&f, &f.supplier),
            token_balance(&f, &f.contract)
        ),
        balances_before
    );
}

#[test]
fn settled_receipt_cannot_be_paid_twice() {
    let f = fixture();
    funded_flagship(&f, 92);
    let quantities = valid_receipt(&f);
    authorized_submit(&f, 92, 1, &quantities);
    assert_eq!(authorized_settle(&f, 92, 1), 56);
    let balances = (
        token_balance(&f, &f.supplier),
        token_balance(&f, &f.contract),
    );
    let po = header(&f, 92);
    let a = f.client().get_line(&92, &1);
    let b = f.client().get_line(&92, &2);
    assert!(f
        .client()
        .mock_all_auths()
        .try_accept_receipt_and_settle(&92, &1)
        .is_err());
    assert_eq!(
        (
            token_balance(&f, &f.supplier),
            token_balance(&f, &f.contract)
        ),
        balances
    );
    assert_eq!(header(&f, 92), po);
    assert_eq!(f.client().get_line(&92, &1), a);
    assert_eq!(f.client().get_line(&92, &2), b);
    assert_eq!(
        f.client().get_receipt(&92, &1).status,
        ReceiptStatus::Settled
    );
}

#[test]
fn stale_receipt_is_revalidated_against_current_accepted_quantity() {
    let f = fixture();
    funded_flagship(&f, 93);
    let six = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 6
        }
    ];
    let five = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 5
        }
    ];
    authorized_submit(&f, 93, 1, &six);
    authorized_submit(&f, 93, 2, &five);
    assert_eq!(f.client().receipt_value(&93, &1), 36);
    assert_eq!(f.client().receipt_value(&93, &2), 30);
    assert_eq!(authorized_settle(&f, 93, 1), 36);
    let supplier = token_balance(&f, &f.supplier);
    let contract = token_balance(&f, &f.contract);
    let paid = header(&f, 93).paid_value;
    assert!(f
        .client()
        .mock_all_auths()
        .try_accept_receipt_and_settle(&93, &2)
        .is_err());
    assert_eq!(
        f.client().get_receipt(&93, &2).status,
        ReceiptStatus::Submitted
    );
    assert_eq!(f.client().get_line(&93, &1).accepted_quantity, 6);
    assert_eq!(header(&f, 93).paid_value, paid);
    assert_eq!(token_balance(&f, &f.supplier), supplier);
    assert_eq!(token_balance(&f, &f.contract), contract);
}

#[test]
fn stale_multi_line_receipt_fails_without_partial_line_settlement() {
    let f = fixture();
    funded_flagship(&f, 94);
    let six_b = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 2,
            quantity_delta: 6
        }
    ];
    let mixed = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 4
        },
        ReceiptQuantityInput {
            line_id: 2,
            quantity_delta: 3
        },
    ];
    authorized_submit(&f, 94, 1, &six_b);
    authorized_submit(&f, 94, 2, &mixed);
    assert_eq!(authorized_settle(&f, 94, 1), 30);
    let supplier = token_balance(&f, &f.supplier);
    let contract = token_balance(&f, &f.contract);
    let paid = header(&f, 94).paid_value;
    assert!(f
        .client()
        .mock_all_auths()
        .try_accept_receipt_and_settle(&94, &2)
        .is_err());
    assert_eq!(
        f.client().get_receipt(&94, &2).status,
        ReceiptStatus::Submitted
    );
    assert_eq!(f.client().get_line(&94, &1).accepted_quantity, 0);
    assert_eq!(f.client().get_line(&94, &2).accepted_quantity, 6);
    assert_eq!(header(&f, 94).paid_value, paid);
    assert_eq!(token_balance(&f, &f.supplier), supplier);
    assert_eq!(token_balance(&f, &f.contract), contract);
}

#[test]
fn exact_remaining_quantity_settles_and_further_positive_delta_is_rejected() {
    let f = fixture();
    funded_flagship(&f, 95);
    let six = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 6
        }
    ];
    authorized_submit(&f, 95, 1, &six);
    assert_eq!(authorized_settle(&f, 95, 1), 36);
    let four = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 4
        }
    ];
    authorized_submit(&f, 95, 2, &four);
    assert_eq!(authorized_settle(&f, 95, 2), 24);
    assert_eq!(f.client().get_line(&95, &1).accepted_quantity, 10);
    assert_eq!(header(&f, 95).paid_value, 60);
    let one = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 1
        }
    ];
    assert!(f
        .client()
        .mock_all_auths()
        .try_submit_receipt(&95, &3, &one)
        .is_err());
    assert!(f.client().try_get_receipt(&95, &3).is_err());
}

#[test]
fn settlement_requires_funded_po_state() {
    let f = fixture();
    let lines = valid_lines(&f.env);
    authorized_create(&f, 101, &lines);
    assert!(f
        .client()
        .mock_all_auths()
        .try_accept_receipt_and_settle(&101, &1)
        .is_err());
    authorized_accept(&f, 101);
    assert!(f
        .client()
        .mock_all_auths()
        .try_accept_receipt_and_settle(&101, &1)
        .is_err());
    assert_eq!(header(&f, 101).funded_value, 0);
    assert_eq!(header(&f, 101).status, PoStatus::Accepted);
    assert_eq!(token_balance(&f, &f.contract), 0);
}

#[test]
fn settlement_keeps_shared_token_po_accounting_isolated() {
    let f = fixture();
    funded_flagship(&f, 96);
    funded_flagship(&f, 97);
    let before_contract = token_balance(&f, &f.contract);
    let before_supplier = token_balance(&f, &f.supplier);
    let quantities = valid_receipt(&f);
    authorized_submit(&f, 96, 1, &quantities);
    assert_eq!(authorized_settle(&f, 96, 1), 56);
    assert_eq!(header(&f, 96).paid_value, 56);
    assert_eq!(header(&f, 97).paid_value, 0);
    assert_eq!(f.client().get_line(&97, &1).accepted_quantity, 0);
    assert_eq!(f.client().get_line(&97, &2).accepted_quantity, 0);
    assert_eq!(token_balance(&f, &f.contract), before_contract - 56);
    assert_eq!(token_balance(&f, &f.supplier), before_supplier + 56);
}

#[test]
fn unrelated_preexisting_balance_does_not_change_per_po_paid_accounting() {
    let f = fixture();
    let admin = StellarAssetClient::new(&f.env, &f.token);
    admin.mock_all_auths().mint(&f.contract, &25);
    funded_flagship(&f, 98);
    assert_eq!(header(&f, 98).paid_value, 0);
    assert_eq!(token_balance(&f, &f.contract), 125);
    let quantities = valid_receipt(&f);
    authorized_submit(&f, 98, 1, &quantities);
    assert_eq!(authorized_settle(&f, 98, 1), 56);
    assert_eq!(header(&f, 98).paid_value, 56);
    assert_eq!(header(&f, 98).funded_value, 100);
    assert_eq!(token_balance(&f, &f.contract), 69);
}

#[test]
fn maximum_reachable_receipt_value_settles_without_overflow() {
    let f = fixture();
    let lines = soroban_sdk::vec![
        &f.env,
        LineInput {
            line_id: 1,
            ordered_quantity: 1,
            unit_price: i128::MAX
        },
    ];
    let admin = StellarAssetClient::new(&f.env, &f.token);
    admin.mock_all_auths().mint(&f.buyer, &i128::MAX);
    create_and_accept(&f, 99, &lines);
    f.client().mock_all_auths().fund_po(&99);
    let one = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 1
        }
    ];
    authorized_submit(&f, 99, 1, &one);
    assert_eq!(authorized_settle(&f, 99, 1), i128::MAX);
    assert_eq!(header(&f, 99).paid_value, i128::MAX);
    assert_eq!(f.client().get_line(&99, &1).accepted_quantity, 1);
    assert_eq!(token_balance(&f, &f.supplier), i128::MAX);
    assert_eq!(token_balance(&f, &f.contract), 0);
}

#[test]
fn failed_sac_outgoing_authorization_rolls_back_product_settlement() {
    let env = Env::default();
    let buyer = Address::generate(&env);
    let supplier = Address::generate(&env);
    let issuer = Address::generate(&env);
    let sac = env.register_stellar_asset_contract_v2(issuer);
    let issuer_control = sac.issuer();
    issuer_control.set_flag(IssuerFlags::RequiredFlag);
    issuer_control.set_flag(IssuerFlags::RevocableFlag);
    let token = sac.address();
    let contract = env.register(PoSettlement, ());
    let f = Fixture {
        env,
        contract,
        buyer,
        supplier,
        token,
    };
    let admin = StellarAssetClient::new(&f.env, &f.token);
    admin.mock_all_auths().set_authorized(&f.buyer, &true);
    admin.mock_all_auths().set_authorized(&f.supplier, &true);
    admin.mock_all_auths().set_authorized(&f.contract, &true);
    admin.mock_all_auths().mint(&f.buyer, &100);
    let lines = valid_lines(&f.env);
    create_and_accept(&f, 100, &lines);
    f.client().mock_all_auths().fund_po(&100);
    let quantities = valid_receipt(&f);
    authorized_submit(&f, 100, 1, &quantities);

    let po_before = header(&f, 100);
    let line_a_before = f.client().get_line(&100, &1);
    let line_b_before = f.client().get_line(&100, &2);
    let receipt_before = f.client().get_receipt(&100, &1);
    let buyer_before = token_balance(&f, &f.buyer);
    let supplier_before = token_balance(&f, &f.supplier);
    let contract_before = token_balance(&f, &f.contract);
    admin.mock_all_auths().set_authorized(&f.contract, &false);

    let result = f
        .client()
        .mock_all_auths()
        .try_accept_receipt_and_settle(&100, &1);
    assert!(result.is_err());
    assert_eq!(header(&f, 100), po_before);
    assert_eq!(f.client().get_line(&100, &1), line_a_before);
    assert_eq!(f.client().get_line(&100, &2), line_b_before);
    assert_eq!(f.client().get_receipt(&100, &1), receipt_before);
    assert_eq!(
        f.client().get_receipt(&100, &1).status,
        ReceiptStatus::Submitted
    );
    assert_eq!(token_balance(&f, &f.buyer), buyer_before);
    assert_eq!(token_balance(&f, &f.supplier), supplier_before);
    assert_eq!(token_balance(&f, &f.contract), contract_before);
}

// Preserve the Stage 2A contract-invocation rollback proof unchanged in scope.
#[test]
fn successful_transfer_commits_marker_and_exact_token_movement() {
    let env = Env::default();
    let issuer = Address::generate(&env);
    let token_address = env
        .register_stellar_asset_contract_v2(issuer.clone())
        .address();
    let probe_address = env.register(AtomicityProbe, ());
    let supplier = Address::generate(&env);
    let admin = StellarAssetClient::new(&env, &token_address);
    let token = TokenClient::new(&env, &token_address);
    let probe = AtomicityProbeClient::new(&env, &probe_address);
    env.mock_all_auths();
    admin.mint(&probe_address, &100);

    assert_eq!(probe.marker(), 0);
    assert_eq!(token.balance(&supplier), 0);
    assert_eq!(token.balance(&probe_address), 100);
    probe.settle(&token_address, &supplier, &37, &9);
    assert_eq!(token.balance(&supplier), 37);
    assert_eq!(token.balance(&probe_address), 63);
    assert_eq!(probe.marker(), 9);
}

#[test]
fn insufficient_balance_fails_and_rolls_back_marker_and_balances() {
    let env = Env::default();
    let issuer = Address::generate(&env);
    let token_address = env.register_stellar_asset_contract_v2(issuer).address();
    let probe_address = env.register(AtomicityProbe, ());
    let supplier = Address::generate(&env);
    let admin = StellarAssetClient::new(&env, &token_address);
    let token = TokenClient::new(&env, &token_address);
    let probe = AtomicityProbeClient::new(&env, &probe_address);
    env.mock_all_auths();
    admin.mint(&probe_address, &100);

    assert_eq!(probe.marker(), 0);
    assert_eq!(token.balance(&supplier), 0);
    assert_eq!(token.balance(&probe_address), 100);
    assert!(probe
        .try_settle(&token_address, &supplier, &101, &77)
        .is_err());
    assert_eq!(token.balance(&supplier), 0);
    assert_eq!(token.balance(&probe_address), 100);
    assert_eq!(probe.marker(), 0);
}

#[test]
fn final_settlement_automatically_closes_and_terminal_state_blocks_mutations() {
    let f = fixture();
    funded_flagship(&f, 201);
    let first = valid_receipt(&f);
    authorized_submit(&f, 201, 1, &first);
    assert_eq!(authorized_settle(&f, 201, 1), 56);
    assert_eq!(header(&f, 201).status, PoStatus::Funded);
    assert_po_conservation(&f, 201);

    let final_quantities = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 4
        },
        ReceiptQuantityInput {
            line_id: 2,
            quantity_delta: 4
        },
    ];
    authorized_submit(&f, 201, 2, &final_quantities);
    let stale_quantities = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 1
        },
    ];
    authorized_submit(&f, 201, 3, &stale_quantities);

    let supplier_before_final = token_balance(&f, &f.supplier);
    let contract_before_final = token_balance(&f, &f.contract);
    assert_eq!(authorized_settle(&f, 201, 2), 44);

    let po = header(&f, 201);
    assert_eq!(po.status, PoStatus::Closed);
    assert_eq!(po.funded_value, 100);
    assert_eq!(po.paid_value, 100);
    assert_eq!(po.refunded_value, 0);
    assert_eq!(f.client().get_line(&201, &1).accepted_quantity, 10);
    assert_eq!(f.client().get_line(&201, &2).accepted_quantity, 8);
    assert_eq!(
        f.client().get_receipt(&201, &1).status,
        ReceiptStatus::Settled
    );
    assert_eq!(
        f.client().get_receipt(&201, &2).status,
        ReceiptStatus::Settled
    );
    assert_eq!(
        f.client().get_receipt(&201, &3).status,
        ReceiptStatus::Submitted
    );
    assert_eq!(token_balance(&f, &f.supplier), supplier_before_final + 44);
    assert_eq!(token_balance(&f, &f.contract), contract_before_final - 44);
    assert_po_conservation(&f, 201);

    let balance_snapshot = (
        token_balance(&f, &f.buyer),
        token_balance(&f, &f.supplier),
        token_balance(&f, &f.contract),
    );
    assert!(f.client().mock_all_auths().try_fund_po(&201).is_err());
    assert!(f
        .client()
        .mock_all_auths()
        .try_submit_receipt(&201, &4, &stale_quantities)
        .is_err());
    assert!(f
        .client()
        .mock_all_auths()
        .try_accept_receipt_and_settle(&201, &3)
        .is_err());
    assert!(f.client().mock_all_auths().try_cancel_po(&201).is_err());
    assert_eq!(
        balance_snapshot,
        (
            token_balance(&f, &f.buyer),
            token_balance(&f, &f.supplier),
            token_balance(&f, &f.contract),
        )
    );
    assert_eq!(header(&f, 201), po);
    assert_eq!(
        f.client().get_receipt(&201, &3).status,
        ReceiptStatus::Submitted
    );
}

#[test]
fn failed_final_supplier_transfer_rolls_back_closeout_and_all_settlement_writes() {
    let env = Env::default();
    let buyer = Address::generate(&env);
    let supplier = Address::generate(&env);
    let issuer = Address::generate(&env);
    let sac = env.register_stellar_asset_contract_v2(issuer);
    let issuer_control = sac.issuer();
    issuer_control.set_flag(IssuerFlags::RequiredFlag);
    issuer_control.set_flag(IssuerFlags::RevocableFlag);
    let token = sac.address();
    let contract = env.register(PoSettlement, ());
    let f = Fixture {
        env,
        contract,
        buyer,
        supplier,
        token,
    };
    let admin = StellarAssetClient::new(&f.env, &f.token);
    admin.mock_all_auths().set_authorized(&f.buyer, &true);
    admin.mock_all_auths().set_authorized(&f.supplier, &true);
    admin.mock_all_auths().set_authorized(&f.contract, &true);
    admin.mock_all_auths().mint(&f.buyer, &100);
    let lines = valid_lines(&f.env);
    create_and_accept(&f, 202, &lines);
    f.client().mock_all_auths().fund_po(&202);
    let partial = valid_receipt(&f);
    authorized_submit(&f, 202, 1, &partial);
    assert_eq!(authorized_settle(&f, 202, 1), 56);
    let final_quantities = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 4
        },
        ReceiptQuantityInput {
            line_id: 2,
            quantity_delta: 4
        },
    ];
    authorized_submit(&f, 202, 2, &final_quantities);

    let po_before = header(&f, 202);
    let line_a_before = f.client().get_line(&202, &1);
    let line_b_before = f.client().get_line(&202, &2);
    let receipt_before = f.client().get_receipt(&202, &2);
    let buyer_before = token_balance(&f, &f.buyer);
    let supplier_before = token_balance(&f, &f.supplier);
    let contract_before = token_balance(&f, &f.contract);
    admin.mock_all_auths().set_authorized(&f.contract, &false);

    assert!(f
        .client()
        .mock_all_auths()
        .try_accept_receipt_and_settle(&202, &2)
        .is_err());
    assert_eq!(header(&f, 202), po_before);
    assert_eq!(header(&f, 202).status, PoStatus::Funded);
    assert_eq!(f.client().get_line(&202, &1), line_a_before);
    assert_eq!(f.client().get_line(&202, &2), line_b_before);
    assert_eq!(f.client().get_receipt(&202, &2), receipt_before);
    assert_eq!(
        f.client().get_receipt(&202, &2).status,
        ReceiptStatus::Submitted
    );
    assert_eq!(token_balance(&f, &f.buyer), buyer_before);
    assert_eq!(token_balance(&f, &f.supplier), supplier_before);
    assert_eq!(token_balance(&f, &f.contract), contract_before);
    assert_po_conservation(&f, 202);
}

#[test]
fn accepted_unfunded_po_cancels_with_both_parties_and_no_refund() {
    let f = fixture();
    let lines = valid_lines(&f.env);
    create_and_accept(&f, 203, &lines);
    let balances_before = (
        token_balance(&f, &f.buyer),
        token_balance(&f, &f.supplier),
        token_balance(&f, &f.contract),
    );

    authorized_cancel(&f, 203);
    let po = header(&f, 203);
    assert_eq!(po.status, PoStatus::Cancelled);
    assert_eq!(po.funded_value, 0);
    assert_eq!(po.paid_value, 0);
    assert_eq!(po.refunded_value, 0);
    assert_eq!(
        balances_before,
        (
            token_balance(&f, &f.buyer),
            token_balance(&f, &f.supplier),
            token_balance(&f, &f.contract),
        )
    );
    assert!(f.client().mock_all_auths().try_cancel_po(&203).is_err());
    assert!(f.client().mock_all_auths().try_fund_po(&203).is_err());
    let quantities = valid_receipt(&f);
    assert!(f
        .client()
        .mock_all_auths()
        .try_submit_receipt(&203, &1, &quantities)
        .is_err());
    assert!(f
        .client()
        .mock_all_auths()
        .try_accept_receipt_and_settle(&203, &1)
        .is_err());
    assert_po_conservation(&f, 203);
}

#[test]
fn cancellation_requires_buyer_and_supplier_authorization() {
    let f = fixture();
    let lines = valid_lines(&f.env);
    create_and_accept(&f, 204, &lines);
    let invoke = MockAuthInvoke {
        contract: &f.contract,
        fn_name: "cancel_po",
        args: (204_u64,).into_val(&f.env),
        sub_invokes: &[],
    };

    assert!(f
        .client()
        .mock_auths(&[MockAuth {
            address: &f.buyer,
            invoke: &invoke,
        }])
        .try_cancel_po(&204)
        .is_err());
    assert!(f
        .client()
        .mock_auths(&[MockAuth {
            address: &f.supplier,
            invoke: &invoke,
        }])
        .try_cancel_po(&204)
        .is_err());
    let unrelated = Address::generate(&f.env);
    assert!(f
        .client()
        .mock_auths(&[MockAuth {
            address: &unrelated,
            invoke: &invoke,
        }])
        .try_cancel_po(&204)
        .is_err());
    assert_eq!(header(&f, 204).status, PoStatus::Accepted);
    assert_eq!(header(&f, 204).funded_value, 0);
    assert_eq!(header(&f, 204).refunded_value, 0);
}

#[test]
fn partial_settlement_mutual_cancel_refunds_only_remaining_value_and_pending_receipts_are_inert() {
    let f = fixture();
    funded_flagship(&f, 205);
    let first = valid_receipt(&f);
    authorized_submit(&f, 205, 1, &first);
    assert_eq!(authorized_settle(&f, 205, 1), 56);
    let pending = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 1
        },
    ];
    authorized_submit(&f, 205, 2, &pending);
    let buyer_before_cancel = token_balance(&f, &f.buyer);
    let supplier_before_cancel = token_balance(&f, &f.supplier);
    let contract_before_cancel = token_balance(&f, &f.contract);
    let accepted_a = f.client().get_line(&205, &1).accepted_quantity;
    let accepted_b = f.client().get_line(&205, &2).accepted_quantity;

    authorized_cancel(&f, 205);
    let po = header(&f, 205);
    assert_eq!(po.status, PoStatus::Cancelled);
    assert_eq!(po.funded_value, 100);
    assert_eq!(po.paid_value, 56);
    assert_eq!(po.refunded_value, 44);
    assert_eq!(f.client().get_line(&205, &1).accepted_quantity, accepted_a);
    assert_eq!(f.client().get_line(&205, &2).accepted_quantity, accepted_b);
    assert_eq!(token_balance(&f, &f.buyer), buyer_before_cancel + 44);
    assert_eq!(token_balance(&f, &f.supplier), supplier_before_cancel);
    assert_eq!(token_balance(&f, &f.contract), contract_before_cancel - 44);
    assert_eq!(
        f.client().get_receipt(&205, &1).status,
        ReceiptStatus::Settled
    );
    assert_eq!(
        f.client().get_receipt(&205, &2).status,
        ReceiptStatus::Submitted
    );
    assert!(f.client().mock_all_auths().try_cancel_po(&205).is_err());
    assert!(f.client().mock_all_auths().try_fund_po(&205).is_err());
    let another = soroban_sdk::vec![
        &f.env,
        ReceiptQuantityInput {
            line_id: 1,
            quantity_delta: 1
        },
    ];
    assert!(f
        .client()
        .mock_all_auths()
        .try_submit_receipt(&205, &3, &another)
        .is_err());
    assert!(f
        .client()
        .mock_all_auths()
        .try_accept_receipt_and_settle(&205, &2)
        .is_err());
    assert_po_conservation(&f, 205);
}

#[test]
fn failed_refund_transfer_rolls_back_mutual_cancellation() {
    let env = Env::default();
    let buyer = Address::generate(&env);
    let supplier = Address::generate(&env);
    let issuer = Address::generate(&env);
    let sac = env.register_stellar_asset_contract_v2(issuer);
    let issuer_control = sac.issuer();
    issuer_control.set_flag(IssuerFlags::RequiredFlag);
    issuer_control.set_flag(IssuerFlags::RevocableFlag);
    let token = sac.address();
    let contract = env.register(PoSettlement, ());
    let f = Fixture {
        env,
        contract,
        buyer,
        supplier,
        token,
    };
    let admin = StellarAssetClient::new(&f.env, &f.token);
    admin.mock_all_auths().set_authorized(&f.buyer, &true);
    admin.mock_all_auths().set_authorized(&f.supplier, &true);
    admin.mock_all_auths().set_authorized(&f.contract, &true);
    admin.mock_all_auths().mint(&f.buyer, &1_000);
    let lines = valid_lines(&f.env);
    create_and_accept(&f, 206, &lines);
    f.client().mock_all_auths().fund_po(&206);
    let first = valid_receipt(&f);
    authorized_submit(&f, 206, 1, &first);
    assert_eq!(authorized_settle(&f, 206, 1), 56);

    let po_before = header(&f, 206);
    let line_a_before = f.client().get_line(&206, &1);
    let line_b_before = f.client().get_line(&206, &2);
    let buyer_before = token_balance(&f, &f.buyer);
    let supplier_before = token_balance(&f, &f.supplier);
    let contract_before = token_balance(&f, &f.contract);
    admin.mock_all_auths().set_authorized(&f.contract, &false);

    assert!(f.client().mock_all_auths().try_cancel_po(&206).is_err());
    assert_eq!(header(&f, 206), po_before);
    assert_eq!(header(&f, 206).status, PoStatus::Funded);
    assert_eq!(header(&f, 206).refunded_value, 0);
    assert_eq!(f.client().get_line(&206, &1), line_a_before);
    assert_eq!(f.client().get_line(&206, &2), line_b_before);
    assert_eq!(f.client().get_line(&206, &1).accepted_quantity, 6);
    assert_eq!(f.client().get_line(&206, &2).accepted_quantity, 4);
    assert_eq!(token_balance(&f, &f.buyer), buyer_before);
    assert_eq!(token_balance(&f, &f.supplier), supplier_before);
    assert_eq!(token_balance(&f, &f.contract), contract_before);
    assert_po_conservation(&f, 206);
}

#[test]
fn cancellation_state_and_conservation_are_isolated_per_po_with_shared_token() {
    let f = fixture();
    funded_flagship(&f, 207);
    funded_flagship(&f, 208);
    let first = valid_receipt(&f);
    authorized_submit(&f, 207, 1, &first);
    assert_eq!(authorized_settle(&f, 207, 1), 56);
    assert_eq!(header(&f, 208).paid_value, 0);
    assert_eq!(header(&f, 208).refunded_value, 0);
    assert_eq!(f.client().get_line(&208, &1).accepted_quantity, 0);
    assert_eq!(f.client().get_line(&208, &2).accepted_quantity, 0);
    authorized_cancel(&f, 207);

    let cancelled = header(&f, 207);
    let untouched = header(&f, 208);
    assert_eq!(cancelled.status, PoStatus::Cancelled);
    assert_eq!(cancelled.paid_value, 56);
    assert_eq!(cancelled.refunded_value, 44);
    assert_eq!(untouched.status, PoStatus::Funded);
    assert_eq!(untouched.funded_value, 100);
    assert_eq!(untouched.paid_value, 0);
    assert_eq!(untouched.refunded_value, 0);
    assert_eq!(token_balance(&f, &f.contract), 100);
    assert_po_conservation(&f, 207);
    assert_po_conservation(&f, 208);
}

#[test]
fn draft_and_unknown_pos_cannot_be_cancelled() {
    let f = fixture();
    let lines = valid_lines(&f.env);
    authorized_create(&f, 209, &lines);
    assert!(f.client().mock_all_auths().try_cancel_po(&209).is_err());
    assert!(f.client().mock_all_auths().try_cancel_po(&999).is_err());
    assert_eq!(header(&f, 209).status, PoStatus::Draft);
    assert!(f.client().try_get_po(&999).is_err());
}

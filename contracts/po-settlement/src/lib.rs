#![no_std]

use soroban_sdk::{contract, contracterror, contractimpl, contracttype, token, Address, Env, Vec};

/// Conservative v0.1 invocation bound. Lines use separate persistent entries;
/// this cap bounds caller input, duplicate checking, and total calculation.
pub const MAX_LINES: u32 = 8;

/// Renew contract/data TTLs when fewer than 120 days plus one ledger remain,
/// targeting 150 days. At the usual ~5 second ledger cadence these are
/// 2,073,601 and 2,592,000 ledgers. The target is below Stellar's currently
/// documented 180-day maximum; sufficiently long inactivity can still archive
/// entries.
pub const TTL_RENEW_THRESHOLD_LEDGERS: u32 = 2_073_601;
pub const TTL_RENEW_TO_LEDGERS: u32 = 2_592_000;

#[contracterror]
#[derive(Copy, Clone, Debug, Eq, PartialEq)]
#[repr(u32)]
pub enum PoError {
    AlreadyExists = 1,
    NotFound = 2,
    InvalidPoId = 3,
    InvalidLine = 4,
    DuplicateLineId = 5,
    TooManyLines = 6,
    ArithmeticOverflow = 7,
    InvalidParties = 8,
    InvalidState = 9,
    ReceiptAlreadyExists = 10,
    InvalidReceipt = 11,
    DuplicateReceiptLine = 12,
    UnknownPoLine = 13,
    QuantityExceedsRemaining = 14,
    InvalidReceiptState = 15,
    ReceiptNotFound = 16,
    SettlementExceedsFunded = 17,
}

#[contracttype]
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum PoStatus {
    Draft,
    Accepted,
    Funded,
    Closed,
    Cancelled,
}

#[contracttype]
#[derive(Copy, Clone, Debug, Eq, PartialEq)]
pub enum ReceiptStatus {
    Submitted,
    Rejected,
    Settled,
}

#[contracttype]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LineInput {
    pub line_id: u32,
    pub ordered_quantity: u64,
    /// Positive token base units per whole quantity unit.
    pub unit_price: i128,
}

#[contracttype]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PoLine {
    pub line_id: u32,
    pub ordered_quantity: u64,
    pub unit_price: i128,
    pub accepted_quantity: u64,
}

#[contracttype]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReceiptQuantityInput {
    pub line_id: u32,
    pub quantity_delta: u64,
}

#[contracttype]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReceiptLine {
    pub line_id: u32,
    pub quantity_delta: u64,
}

#[contracttype]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Receipt {
    pub receipt_id: u64,
    pub po_id: u64,
    pub status: ReceiptStatus,
    pub line_count: u32,
    /// Bounded index used to enumerate per-receipt line entries for preflight.
    pub line_ids: Vec<u32>,
}

#[contracttype]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PurchaseOrder {
    pub po_id: u64,
    pub buyer: Address,
    pub supplier: Address,
    pub token: Address,
    pub status: PoStatus,
    /// Derived total in positive signed-i128 token base units.
    pub total_value: i128,
    /// Zero until the entire stored total has transferred into this contract.
    pub funded_value: i128,
    /// Sum of successfully settled receipt values for this PO.
    pub paid_value: i128,
    /// Value refunded to the buyer by mutual cancellation.
    pub refunded_value: i128,
    pub line_count: u32,
    /// Bounded index used for closeout and accepted-value invariant checks.
    pub line_ids: Vec<u32>,
}

#[contracttype]
#[derive(Clone)]
enum DataKey {
    Header(u64),
    Line(u64, u32),
    Receipt(u64, u64),
    ReceiptLine(u64, u64, u32),
}

#[contract]
pub struct PoSettlement;

#[contractimpl]
impl PoSettlement {
    pub fn create_po(
        env: Env,
        po_id: u64,
        buyer: Address,
        supplier: Address,
        token: Address,
        lines: Vec<LineInput>,
    ) -> Result<(), PoError> {
        extend_contract_ttl(&env);
        buyer.require_auth();

        if po_id == 0 {
            return Err(PoError::InvalidPoId);
        }
        if buyer == supplier {
            return Err(PoError::InvalidParties);
        }
        let header_key = DataKey::Header(po_id);
        if env.storage().persistent().has(&header_key) {
            return Err(PoError::AlreadyExists);
        }
        if lines.is_empty() {
            return Err(PoError::InvalidLine);
        }
        if lines.len() > MAX_LINES {
            return Err(PoError::TooManyLines);
        }

        // Validate and calculate fully before writing any PO state.
        let mut total_value: i128 = 0;
        let mut i = 0;
        while i < lines.len() {
            let line = lines.get(i).ok_or(PoError::InvalidLine)?;
            if line.line_id == 0 || line.ordered_quantity == 0 || line.unit_price <= 0 {
                return Err(PoError::InvalidLine);
            }

            let mut prior = 0;
            while prior < i {
                let prior_line = lines.get(prior).ok_or(PoError::InvalidLine)?;
                if prior_line.line_id == line.line_id {
                    return Err(PoError::DuplicateLineId);
                }
                prior += 1;
            }

            let quantity = i128::from(line.ordered_quantity);
            let line_value = quantity
                .checked_mul(line.unit_price)
                .ok_or(PoError::ArithmeticOverflow)?;
            total_value = total_value
                .checked_add(line_value)
                .ok_or(PoError::ArithmeticOverflow)?;
            i += 1;
        }
        if total_value <= 0 {
            return Err(PoError::ArithmeticOverflow);
        }

        let mut line_ids = Vec::new(&env);
        let mut i = 0;
        while i < lines.len() {
            line_ids.push_back(lines.get(i).ok_or(PoError::InvalidLine)?.line_id);
            i += 1;
        }
        let header = PurchaseOrder {
            po_id,
            buyer,
            supplier,
            token,
            status: PoStatus::Draft,
            total_value,
            funded_value: 0,
            paid_value: 0,
            refunded_value: 0,
            line_count: lines.len(),
            line_ids,
        };
        env.storage().persistent().set(&header_key, &header);
        env.storage().persistent().extend_ttl(
            &header_key,
            TTL_RENEW_THRESHOLD_LEDGERS,
            TTL_RENEW_TO_LEDGERS,
        );
        let mut i = 0;
        while i < lines.len() {
            let input = lines.get(i).ok_or(PoError::InvalidLine)?;
            let line = PoLine {
                line_id: input.line_id,
                ordered_quantity: input.ordered_quantity,
                unit_price: input.unit_price,
                accepted_quantity: 0,
            };
            let line_key = DataKey::Line(po_id, input.line_id);
            env.storage().persistent().set(&line_key, &line);
            env.storage().persistent().extend_ttl(
                &line_key,
                TTL_RENEW_THRESHOLD_LEDGERS,
                TTL_RENEW_TO_LEDGERS,
            );
            i += 1;
        }
        Ok(())
    }

    pub fn accept_po(env: Env, po_id: u64) -> Result<(), PoError> {
        extend_contract_ttl(&env);
        let key = DataKey::Header(po_id);
        let mut header: PurchaseOrder = env
            .storage()
            .persistent()
            .get(&key)
            .ok_or(PoError::NotFound)?;
        if header.status != PoStatus::Draft {
            return Err(PoError::InvalidState);
        }
        header.supplier.require_auth();
        extend_po_ttl(&env, &header)?;
        header.status = PoStatus::Accepted;
        env.storage().persistent().set(&key, &header);
        Ok(())
    }

    /// Funds an accepted PO for its exact stored total from its recorded buyer.
    pub fn fund_po(env: Env, po_id: u64) -> Result<(), PoError> {
        extend_contract_ttl(&env);
        let key = DataKey::Header(po_id);
        let mut header: PurchaseOrder = env
            .storage()
            .persistent()
            .get(&key)
            .ok_or(PoError::NotFound)?;
        if header.status != PoStatus::Accepted || header.funded_value != 0 {
            return Err(PoError::InvalidState);
        }

        header.buyer.require_auth();
        extend_po_ttl(&env, &header)?;
        let contract_address = env.current_contract_address();
        token::TokenClient::new(&env, &header.token).transfer(
            &header.buyer,
            &contract_address,
            &header.total_value,
        );

        // Only record funding after the exact token transfer succeeds. Soroban
        // invocation atomicity reverts both token and storage effects on error.
        header.funded_value = header.total_value;
        header.status = PoStatus::Funded;
        env.storage().persistent().set(&key, &header);
        Ok(())
    }

    /// Records the supplier's asserted quantity deltas for a funded PO.
    pub fn submit_receipt(
        env: Env,
        po_id: u64,
        receipt_id: u64,
        quantities: Vec<ReceiptQuantityInput>,
    ) -> Result<(), PoError> {
        extend_contract_ttl(&env);
        let header: PurchaseOrder = env
            .storage()
            .persistent()
            .get(&DataKey::Header(po_id))
            .ok_or(PoError::NotFound)?;
        if header.status != PoStatus::Funded {
            return Err(PoError::InvalidState);
        }
        header.supplier.require_auth();

        if receipt_id == 0 || quantities.is_empty() || quantities.len() > header.line_count {
            return Err(PoError::InvalidReceipt);
        }
        let receipt_key = DataKey::Receipt(po_id, receipt_id);
        if env.storage().persistent().has(&receipt_key) {
            return Err(PoError::ReceiptAlreadyExists);
        }

        // Validate all receipt data before persisting any header or line entry.
        let mut i = 0;
        while i < quantities.len() {
            let input = quantities.get(i).ok_or(PoError::InvalidReceipt)?;
            if input.line_id == 0 || input.quantity_delta == 0 {
                return Err(PoError::InvalidReceipt);
            }
            let line: PoLine = env
                .storage()
                .persistent()
                .get(&DataKey::Line(po_id, input.line_id))
                .ok_or(PoError::UnknownPoLine)?;
            let remaining = line
                .ordered_quantity
                .checked_sub(line.accepted_quantity)
                .ok_or(PoError::QuantityExceedsRemaining)?;
            if input.quantity_delta > remaining {
                return Err(PoError::QuantityExceedsRemaining);
            }

            let mut prior = 0;
            while prior < i {
                let prior_input = quantities.get(prior).ok_or(PoError::InvalidReceipt)?;
                if prior_input.line_id == input.line_id {
                    return Err(PoError::DuplicateReceiptLine);
                }
                prior += 1;
            }
            i += 1;
        }

        extend_po_ttl(&env, &header)?;

        let mut line_ids = Vec::new(&env);
        let mut i = 0;
        while i < quantities.len() {
            line_ids.push_back(quantities.get(i).ok_or(PoError::InvalidReceipt)?.line_id);
            i += 1;
        }
        let receipt = Receipt {
            receipt_id,
            po_id,
            status: ReceiptStatus::Submitted,
            line_count: quantities.len(),
            line_ids,
        };
        env.storage().persistent().set(&receipt_key, &receipt);
        env.storage().persistent().extend_ttl(
            &receipt_key,
            TTL_RENEW_THRESHOLD_LEDGERS,
            TTL_RENEW_TO_LEDGERS,
        );
        let mut i = 0;
        while i < quantities.len() {
            let input = quantities.get(i).ok_or(PoError::InvalidReceipt)?;
            let line = ReceiptLine {
                line_id: input.line_id,
                quantity_delta: input.quantity_delta,
            };
            let line_key = DataKey::ReceiptLine(po_id, receipt_id, input.line_id);
            env.storage().persistent().set(&line_key, &line);
            env.storage().persistent().extend_ttl(
                &line_key,
                TTL_RENEW_THRESHOLD_LEDGERS,
                TTL_RENEW_TO_LEDGERS,
            );
            i += 1;
        }
        Ok(())
    }

    /// Buyer rejects a supplier assertion. Receipt identity remains consumed.
    pub fn reject_receipt(env: Env, po_id: u64, receipt_id: u64) -> Result<(), PoError> {
        extend_contract_ttl(&env);
        let key = DataKey::Receipt(po_id, receipt_id);
        let mut receipt: Receipt = env
            .storage()
            .persistent()
            .get(&key)
            .ok_or(PoError::ReceiptNotFound)?;
        if receipt.status != ReceiptStatus::Submitted {
            return Err(PoError::InvalidReceiptState);
        }
        let header: PurchaseOrder = env
            .storage()
            .persistent()
            .get(&DataKey::Header(po_id))
            .ok_or(PoError::NotFound)?;
        header.buyer.require_auth();
        extend_po_ttl(&env, &header)?;
        extend_receipt_ttl(&env, &receipt)?;
        receipt.status = ReceiptStatus::Rejected;
        env.storage().persistent().set(&key, &receipt);
        Ok(())
    }

    /// Read-only calculation for a submitted receipt, rechecking current PO quantities.
    pub fn receipt_value(env: Env, po_id: u64, receipt_id: u64) -> Result<i128, PoError> {
        extend_contract_ttl(&env);
        let receipt: Receipt = env
            .storage()
            .persistent()
            .get(&DataKey::Receipt(po_id, receipt_id))
            .ok_or(PoError::ReceiptNotFound)?;
        match receipt.status {
            ReceiptStatus::Submitted => {
                calculate_receipt_value(&env, po_id, receipt_id, &receipt, true)
            }
            // Receipt quantities and accepted PO terms are immutable, so the
            // settled amount remains safely derivable without storing it twice.
            ReceiptStatus::Settled => {
                calculate_receipt_value(&env, po_id, receipt_id, &receipt, false)
            }
            ReceiptStatus::Rejected => Err(PoError::InvalidReceiptState),
        }
    }

    /// Atomically accepts the current submitted quantities and pays their
    /// quantity-derived value to the supplier recorded on the PO.
    pub fn accept_receipt_and_settle(
        env: Env,
        po_id: u64,
        receipt_id: u64,
    ) -> Result<i128, PoError> {
        extend_contract_ttl(&env);
        let po_key = DataKey::Header(po_id);
        let mut header: PurchaseOrder = env
            .storage()
            .persistent()
            .get(&po_key)
            .ok_or(PoError::NotFound)?;
        if header.status != PoStatus::Funded || header.funded_value != header.total_value {
            return Err(PoError::InvalidState);
        }

        let receipt_key = DataKey::Receipt(po_id, receipt_id);
        let mut receipt: Receipt = env
            .storage()
            .persistent()
            .get(&receipt_key)
            .ok_or(PoError::ReceiptNotFound)?;
        if receipt.status != ReceiptStatus::Submitted {
            return Err(PoError::InvalidReceiptState);
        }

        header.buyer.require_auth();
        // Earlier preview values are advisory: another receipt may have
        // settled since, so this uses the current accepted quantities.
        let settlement_value = calculate_receipt_value(&env, po_id, receipt_id, &receipt, true)?;
        let new_paid_value = header
            .paid_value
            .checked_add(settlement_value)
            .ok_or(PoError::ArithmeticOverflow)?;
        if settlement_value <= 0
            || new_paid_value > header.funded_value
            || new_paid_value > header.total_value
        {
            return Err(PoError::SettlementExceedsFunded);
        }

        // Apply the per-PO accounting before the external call. Soroban
        // invocation atomicity must revert these writes if token transfer fails.
        let mut i = 0;
        while i < receipt.line_count {
            let line_id = receipt.line_ids.get(i).ok_or(PoError::InvalidReceipt)?;
            let receipt_line: ReceiptLine = env
                .storage()
                .persistent()
                .get(&DataKey::ReceiptLine(po_id, receipt_id, line_id))
                .ok_or(PoError::ReceiptNotFound)?;
            let mut po_line: PoLine = env
                .storage()
                .persistent()
                .get(&DataKey::Line(po_id, line_id))
                .ok_or(PoError::UnknownPoLine)?;
            let next_accepted = po_line
                .accepted_quantity
                .checked_add(receipt_line.quantity_delta)
                .ok_or(PoError::ArithmeticOverflow)?;
            if next_accepted > po_line.ordered_quantity {
                return Err(PoError::QuantityExceedsRemaining);
            }
            po_line.accepted_quantity = next_accepted;
            env.storage()
                .persistent()
                .set(&DataKey::Line(po_id, line_id), &po_line);
            i += 1;
        }

        let accepted_value = calculate_accepted_value(&env, &header)?;
        if accepted_value != new_paid_value {
            return Err(PoError::InvalidState);
        }
        let all_lines_fulfilled = all_lines_fulfilled(&env, &header)?;
        if all_lines_fulfilled
            && (new_paid_value != header.total_value || header.refunded_value != 0)
        {
            return Err(PoError::InvalidState);
        }

        extend_po_ttl(&env, &header)?;
        extend_receipt_ttl(&env, &receipt)?;
        header.paid_value = new_paid_value;
        if all_lines_fulfilled {
            header.status = PoStatus::Closed;
        }
        receipt.status = ReceiptStatus::Settled;
        env.storage().persistent().set(&po_key, &header);
        env.storage().persistent().set(&receipt_key, &receipt);

        let contract_address = env.current_contract_address();
        token::TokenClient::new(&env, &header.token).transfer(
            &contract_address,
            &header.supplier,
            &settlement_value,
        );

        Ok(settlement_value)
    }

    /// Cancels an Accepted or Funded PO only with authorization from both
    /// recorded parties. A funded PO refunds only its unpaid, unrefunded value.
    pub fn cancel_po(env: Env, po_id: u64) -> Result<(), PoError> {
        extend_contract_ttl(&env);
        let key = DataKey::Header(po_id);
        let mut header: PurchaseOrder = env
            .storage()
            .persistent()
            .get(&key)
            .ok_or(PoError::NotFound)?;
        if header.status != PoStatus::Accepted && header.status != PoStatus::Funded {
            return Err(PoError::InvalidState);
        }

        header.buyer.require_auth();
        header.supplier.require_auth();

        let refund = match &header.status {
            PoStatus::Accepted => {
                if header.funded_value != 0 || header.paid_value != 0 || header.refunded_value != 0
                {
                    return Err(PoError::InvalidState);
                }
                0
            }
            PoStatus::Funded => {
                if header.funded_value != header.total_value
                    || calculate_accepted_value(&env, &header)? != header.paid_value
                {
                    return Err(PoError::InvalidState);
                }
                let remaining = header
                    .funded_value
                    .checked_sub(header.paid_value)
                    .and_then(|value| value.checked_sub(header.refunded_value))
                    .ok_or(PoError::ArithmeticOverflow)?;
                if remaining <= 0 {
                    return Err(PoError::InvalidState);
                }
                let refunded_value = header
                    .refunded_value
                    .checked_add(remaining)
                    .ok_or(PoError::ArithmeticOverflow)?;
                let accounted_value = header
                    .paid_value
                    .checked_add(refunded_value)
                    .ok_or(PoError::ArithmeticOverflow)?;
                if accounted_value != header.funded_value {
                    return Err(PoError::InvalidState);
                }
                header.refunded_value = refunded_value;
                remaining
            }
            _ => return Err(PoError::InvalidState),
        };

        extend_po_ttl(&env, &header)?;
        header.status = PoStatus::Cancelled;
        // Persist before the outgoing transfer so the failure path exercises
        // invocation rollback for the actual cancellation operation.
        env.storage().persistent().set(&key, &header);
        if refund > 0 {
            let contract_address = env.current_contract_address();
            token::TokenClient::new(&env, &header.token).transfer(
                &contract_address,
                &header.buyer,
                &refund,
            );
        }
        Ok(())
    }

    pub fn get_receipt(env: Env, po_id: u64, receipt_id: u64) -> Result<Receipt, PoError> {
        extend_contract_ttl(&env);
        env.storage()
            .persistent()
            .get(&DataKey::Receipt(po_id, receipt_id))
            .ok_or(PoError::ReceiptNotFound)
    }

    pub fn get_receipt_line(
        env: Env,
        po_id: u64,
        receipt_id: u64,
        line_id: u32,
    ) -> Result<ReceiptLine, PoError> {
        extend_contract_ttl(&env);
        env.storage()
            .persistent()
            .get(&DataKey::ReceiptLine(po_id, receipt_id, line_id))
            .ok_or(PoError::ReceiptNotFound)
    }

    pub fn get_po(env: Env, po_id: u64) -> Result<PurchaseOrder, PoError> {
        extend_contract_ttl(&env);
        env.storage()
            .persistent()
            .get(&DataKey::Header(po_id))
            .ok_or(PoError::NotFound)
    }

    pub fn get_line(env: Env, po_id: u64, line_id: u32) -> Result<PoLine, PoError> {
        extend_contract_ttl(&env);
        env.storage()
            .persistent()
            .get(&DataKey::Line(po_id, line_id))
            .ok_or(PoError::NotFound)
    }
}

/// Renew the executable and instance while the instance is still live.
fn extend_contract_ttl(env: &Env) {
    env.storage()
        .instance()
        .extend_ttl(TTL_RENEW_THRESHOLD_LEDGERS, TTL_RENEW_TO_LEDGERS);
}

/// Renew the header and every bounded line record needed for the PO lifecycle.
fn extend_po_ttl(env: &Env, header: &PurchaseOrder) -> Result<(), PoError> {
    let storage = env.storage().persistent();
    let header_key = DataKey::Header(header.po_id);
    if storage.get::<_, PurchaseOrder>(&header_key).is_none() {
        return Err(PoError::NotFound);
    }
    storage.extend_ttl(
        &header_key,
        TTL_RENEW_THRESHOLD_LEDGERS,
        TTL_RENEW_TO_LEDGERS,
    );

    let mut i = 0;
    while i < header.line_count {
        let line_id = header.line_ids.get(i).ok_or(PoError::InvalidLine)?;
        let line_key = DataKey::Line(header.po_id, line_id);
        if storage.get::<_, PoLine>(&line_key).is_none() {
            return Err(PoError::NotFound);
        }
        storage.extend_ttl(&line_key, TTL_RENEW_THRESHOLD_LEDGERS, TTL_RENEW_TO_LEDGERS);
        i += 1;
    }
    Ok(())
}

/// Renew one live receipt and each line record indexed by its bounded header.
fn extend_receipt_ttl(env: &Env, receipt: &Receipt) -> Result<(), PoError> {
    let storage = env.storage().persistent();
    let receipt_key = DataKey::Receipt(receipt.po_id, receipt.receipt_id);
    if storage.get::<_, Receipt>(&receipt_key).is_none() {
        return Err(PoError::ReceiptNotFound);
    }
    storage.extend_ttl(
        &receipt_key,
        TTL_RENEW_THRESHOLD_LEDGERS,
        TTL_RENEW_TO_LEDGERS,
    );

    let mut i = 0;
    while i < receipt.line_count {
        let line_id = receipt.line_ids.get(i).ok_or(PoError::InvalidReceipt)?;
        let line_key = DataKey::ReceiptLine(receipt.po_id, receipt.receipt_id, line_id);
        if storage.get::<_, ReceiptLine>(&line_key).is_none() {
            return Err(PoError::ReceiptNotFound);
        }
        storage.extend_ttl(&line_key, TTL_RENEW_THRESHOLD_LEDGERS, TTL_RENEW_TO_LEDGERS);
        i += 1;
    }
    Ok(())
}

fn calculate_accepted_value(env: &Env, header: &PurchaseOrder) -> Result<i128, PoError> {
    let mut total = 0_i128;
    let mut i = 0;
    while i < header.line_count {
        let line_id = header.line_ids.get(i).ok_or(PoError::InvalidLine)?;
        let line: PoLine = env
            .storage()
            .persistent()
            .get(&DataKey::Line(header.po_id, line_id))
            .ok_or(PoError::NotFound)?;
        let line_value = i128::from(line.accepted_quantity)
            .checked_mul(line.unit_price)
            .ok_or(PoError::ArithmeticOverflow)?;
        total = total
            .checked_add(line_value)
            .ok_or(PoError::ArithmeticOverflow)?;
        i += 1;
    }
    Ok(total)
}

fn all_lines_fulfilled(env: &Env, header: &PurchaseOrder) -> Result<bool, PoError> {
    let mut i = 0;
    while i < header.line_count {
        let line_id = header.line_ids.get(i).ok_or(PoError::InvalidLine)?;
        let line: PoLine = env
            .storage()
            .persistent()
            .get(&DataKey::Line(header.po_id, line_id))
            .ok_or(PoError::NotFound)?;
        if line.accepted_quantity != line.ordered_quantity {
            return Ok(false);
        }
        i += 1;
    }
    Ok(true)
}

fn calculate_receipt_value(
    env: &Env,
    po_id: u64,
    receipt_id: u64,
    receipt: &Receipt,
    check_remaining: bool,
) -> Result<i128, PoError> {
    let mut total: i128 = 0;
    let mut i = 0;
    while i < receipt.line_count {
        let line_id = receipt.line_ids.get(i).ok_or(PoError::InvalidReceipt)?;
        let receipt_line: ReceiptLine = env
            .storage()
            .persistent()
            .get(&DataKey::ReceiptLine(po_id, receipt_id, line_id))
            .ok_or(PoError::ReceiptNotFound)?;
        let po_line: PoLine = env
            .storage()
            .persistent()
            .get(&DataKey::Line(po_id, receipt_line.line_id))
            .ok_or(PoError::UnknownPoLine)?;
        if receipt_line.quantity_delta == 0 {
            return Err(PoError::InvalidReceipt);
        }
        if check_remaining {
            let remaining = po_line
                .ordered_quantity
                .checked_sub(po_line.accepted_quantity)
                .ok_or(PoError::QuantityExceedsRemaining)?;
            if receipt_line.quantity_delta > remaining {
                return Err(PoError::QuantityExceedsRemaining);
            }
        }
        let line_value = i128::from(receipt_line.quantity_delta)
            .checked_mul(po_line.unit_price)
            .ok_or(PoError::ArithmeticOverflow)?;
        total = total
            .checked_add(line_value)
            .ok_or(PoError::ArithmeticOverflow)?;
        i += 1;
    }
    if total <= 0 {
        return Err(PoError::InvalidReceipt);
    }
    Ok(total)
}

#[cfg(test)]
mod test;

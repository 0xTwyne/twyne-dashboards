#!/usr/bin/env python3
"""
Query Aave V3 wstETH/WETH positions via Multicall3.

Usage:
    python scripts/query_position.py 0xAbC123...
    python scripts/query_position.py 0xAbC123... 0xDef456...  (multiple)

Dependencies: web3 (pip install web3)
"""

import sys
from web3 import Web3

# --- Constants ---
# Default public RPC; override with RPC_URL env var
import os
RPC_URL = os.environ.get("RPC_URL", "https://eth.llamarpc.com")

# Aave V3 Mainnet
POOL = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
A_WSTETH = "0x0B925eD163218f6662a35e0f0371Ac234f9E9371"          # aEthwstETH
VAR_DEBT_WETH = "0xeA51d7853EEFb32b6ee06b1C12E6dcCA88Be0fFe"     # variableDebtWETH

# Minimal ABI fragments
BALANCE_OF_SIG = Web3.keccak(text="balanceOf(address)")[:4]                    # 0x70a08231
GET_USER_ACCOUNT_DATA_SIG = Web3.keccak(text="getUserAccountData(address)")[:4]  # 0xbf92857c
AGGREGATE3_SIG = Web3.keccak(text="aggregate3((address,bool,bytes)[])")[:4]      # 0x82ad56cb


def encode_balance_of(address: str) -> bytes:
    return BALANCE_OF_SIG + bytes.fromhex(address[2:].lower().zfill(64))


def encode_get_user_account_data(address: str) -> bytes:
    return GET_USER_ACCOUNT_DATA_SIG + bytes.fromhex(address[2:].lower().zfill(64))


def build_multicall_data(address: str) -> bytes:
    """Build Multicall3.aggregate3 calldata for 3 calls."""
    calls = [
        (A_WSTETH, True, encode_balance_of(address)),
        (VAR_DEBT_WETH, True, encode_balance_of(address)),
        (POOL, True, encode_get_user_account_data(address)),
    ]

    # ABI encode: aggregate3((address,bool,bytes)[])
    # tuple array at offset 0x20, length 3, then each tuple
    parts = []

    # Function selector
    parts.append(AGGREGATE3_SIG)

    # Offset to the array (0x20)
    parts.append((0x20).to_bytes(32, "big"))

    # Array length
    parts.append(len(calls).to_bytes(32, "big"))

    # Each element is a dynamic tuple, so we need offsets first
    # Calculate offsets for each tuple element
    # After the 3 offset words, we have the tuple data
    tuple_data_chunks = []
    for target, allow_failure, call_data in calls:
        # Each tuple: (address, bool, bytes)
        # address (padded to 32), bool (padded to 32), offset to bytes, bytes length, bytes data
        chunk = b""
        chunk += bytes.fromhex(target[2:].lower().zfill(64))  # address
        chunk += (1 if allow_failure else 0).to_bytes(32, "big")  # allowFailure
        chunk += (0x60).to_bytes(32, "big")  # offset to bytes within this tuple (3 * 32 = 0x60)
        chunk += len(call_data).to_bytes(32, "big")  # bytes length
        chunk += call_data + b"\x00" * (32 - len(call_data) % 32) if len(call_data) % 32 != 0 else call_data  # padded bytes
        tuple_data_chunks.append(chunk)

    # Now build offsets: each tuple element is at an offset from the start of the array data
    # The first 3 words after array length are offsets to each tuple
    offset_base = len(calls) * 32  # skip the offset words themselves
    current_offset = offset_base
    for i, chunk in enumerate(tuple_data_chunks):
        parts.append(current_offset.to_bytes(32, "big"))
        current_offset += len(chunk)

    # Append tuple data
    for chunk in tuple_data_chunks:
        parts.append(chunk)

    return b"".join(parts)


def decode_multicall_result(raw: bytes) -> list[tuple[bool, bytes]]:
    """Decode Multicall3.aggregate3 return: (bool success, bytes returnData)[]"""
    # Return is: offset to array, array length, then for each element: offset to tuple
    # Each tuple: bool success, offset to bytes, bytes length, bytes data

    # Skip offset word (first 32 bytes points to array start)
    array_offset = int.from_bytes(raw[:32], "big")
    array_start = array_offset
    array_len = int.from_bytes(raw[array_start:array_start + 32], "big")

    results = []
    for i in range(array_len):
        # Offset to this tuple element (relative to array_start + 32)
        elem_offset_pos = array_start + 32 + i * 32
        elem_offset = int.from_bytes(raw[elem_offset_pos:elem_offset_pos + 32], "big")
        elem_start = array_start + 32 + elem_offset

        success = int.from_bytes(raw[elem_start:elem_start + 32], "big") != 0
        # bytes offset within tuple
        bytes_offset = int.from_bytes(raw[elem_start + 32:elem_start + 64], "big")
        bytes_start = elem_start + bytes_offset
        bytes_len = int.from_bytes(raw[bytes_start:bytes_start + 32], "big")
        data = raw[bytes_start + 32:bytes_start + 32 + bytes_len]
        results.append((success, data))

    return results


def query_position(w3: Web3, address: str) -> dict:
    """Query a single address's Aave wstETH/WETH position via Multicall3."""
    address = Web3.to_checksum_address(address)
    calldata = build_multicall_data(address)

    result = w3.eth.call({
        "to": Web3.to_checksum_address(MULTICALL3),
        "data": calldata,
    })

    decoded = decode_multicall_result(result)

    # 1. aWstETH balance (uint256, 18 decimals)
    ok_col, col_data = decoded[0]
    if not ok_col or len(col_data) < 32:
        raise RuntimeError(f"aWstETH balanceOf failed for {address}")
    collateral_raw = int.from_bytes(col_data[:32], "big")
    collateral = collateral_raw / 1e18

    # 2. variableDebtWETH balance (uint256, 18 decimals)
    ok_debt, debt_data = decoded[1]
    if not ok_debt or len(debt_data) < 32:
        raise RuntimeError(f"variableDebtWETH balanceOf failed for {address}")
    debt_raw = int.from_bytes(debt_data[:32], "big")
    debt = debt_raw / 1e18

    # 3. getUserAccountData returns:
    #    (uint256 totalCollateralBase, uint256 totalDebtBase,
    #     uint256 availableBorrowsBase, uint256 currentLtv,
    #     uint256 currentLiquidationThreshold, uint256 healthFactor)
    #    All "Base" values are in USD with 8 decimals
    #    currentLtv and currentLiquidationThreshold are in basis points (1e4)
    #    healthFactor is in 1e18
    ok_acct, acct_data = decoded[2]
    if not ok_acct or len(acct_data) < 192:
        raise RuntimeError(f"getUserAccountData failed for {address}")

    total_collateral_usd = int.from_bytes(acct_data[0:32], "big") / 1e8
    total_debt_usd = int.from_bytes(acct_data[32:64], "big") / 1e8
    available_borrows_usd = int.from_bytes(acct_data[64:96], "big") / 1e8
    current_ltv_bps = int.from_bytes(acct_data[96:128], "big")
    liq_threshold_bps = int.from_bytes(acct_data[128:160], "big")
    health_factor_raw = int.from_bytes(acct_data[160:192], "big")
    # Aave returns max uint256 for HF when there's no debt
    health_factor = float("inf") if health_factor_raw == 2**256 - 1 else health_factor_raw / 1e18

    # Derived metrics
    actual_ltv = (total_debt_usd / total_collateral_usd * 100) if total_collateral_usd > 0 else 0.0
    leverage = 1 / (1 - actual_ltv / 100) if actual_ltv < 100 else float("inf")

    return {
        "address": address,
        "awsteth_balance": collateral,
        "var_debt_weth": debt,
        "total_collateral_usd": total_collateral_usd,
        "total_debt_usd": total_debt_usd,
        "available_borrows_usd": available_borrows_usd,
        "current_ltv_bps": current_ltv_bps,
        "liq_threshold_bps": liq_threshold_bps,
        "health_factor": health_factor,
        "actual_ltv_pct": actual_ltv,
        "leverage": leverage,
    }


def print_position(pos: dict) -> None:
    hf = pos["health_factor"]
    hf_str = "inf (no debt)" if hf == float("inf") else f"{hf:.4f}"
    lev = pos["leverage"]
    lev_str = "inf" if lev == float("inf") else f"{lev:.2f}x"

    print(f"\n{'=' * 60}")
    print(f"  Aave V3 Position: {pos['address']}")
    print(f"{'=' * 60}")
    print(f"  aWstETH balance:       {pos['awsteth_balance']:>18.6f} wstETH")
    print(f"  variableDebtWETH:      {pos['var_debt_weth']:>18.6f} WETH")
    print(f"  ---")
    print(f"  Total collateral (USD): ${pos['total_collateral_usd']:>16,.2f}")
    print(f"  Total debt (USD):       ${pos['total_debt_usd']:>16,.2f}")
    print(f"  Available borrows (USD):${pos['available_borrows_usd']:>16,.2f}")
    print(f"  ---")
    print(f"  Weighted avg LTV:       {pos['current_ltv_bps'] / 100:>17.2f}%")
    print(f"  Liquidation threshold:  {pos['liq_threshold_bps'] / 100:>17.2f}%")
    print(f"  Actual LTV:             {pos['actual_ltv_pct']:>17.2f}%")
    print(f"  Health factor:          {hf_str:>17}")
    print(f"  Leverage:               {lev_str:>17}")
    print(f"{'=' * 60}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/query_position.py <address> [address2 ...]")
        sys.exit(1)

    addresses = sys.argv[1:]
    w3 = Web3(Web3.HTTPProvider(RPC_URL))

    if not w3.is_connected():
        print(f"Error: Cannot connect to RPC at {RPC_URL}")
        sys.exit(1)

    print(f"Connected to chain {w3.eth.chain_id} via {RPC_URL}")

    for addr in addresses:
        try:
            pos = query_position(w3, addr)
            print_position(pos)
        except Exception as e:
            print(f"\nError querying {addr}: {e}")


if __name__ == "__main__":
    main()

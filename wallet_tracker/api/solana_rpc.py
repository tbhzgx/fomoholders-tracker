"""Solana RPC client for direct blockchain queries."""

from typing import Any

import httpx

from .base import APIError


class SolanaRPCClient:
    """
    Client for Solana JSON-RPC API.

    Uses public RPC endpoints or Helius RPC for better reliability.
    """

    # Public RPC endpoints (fallbacks)
    PUBLIC_RPCS = [
        "https://api.mainnet-beta.solana.com",
        "https://solana-mainnet.rpc.extrnode.com",
    ]

    def __init__(self, rpc_url: str | None = None):
        """
        Initialize Solana RPC client.

        Args:
            rpc_url: Custom RPC URL (e.g., Helius RPC with API key)
        """
        self.rpc_url = rpc_url or self.PUBLIC_RPCS[0]
        self.timeout = 30.0

    def _request(self, method: str, params: list[Any]) -> Any:
        """Make JSON-RPC request."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(self.rpc_url, json=payload)
            data = response.json()

            if "error" in data:
                error = data["error"]
                raise APIError(f"RPC Error: {error.get('message', error)}")

            return data.get("result")

    def get_token_supply(self, mint_address: str) -> dict[str, Any]:
        """
        Get the total supply of a token.

        Args:
            mint_address: Token mint address

        Returns:
            Token supply info with amount, decimals, uiAmount
        """
        result = self._request("getTokenSupply", [mint_address])
        return result.get("value", {}) if result else {}

    def get_token_supply_ui(self, mint_address: str) -> float:
        """
        Get token supply as a human-readable float.

        Args:
            mint_address: Token mint address

        Returns:
            Token supply as float
        """
        supply_info = self.get_token_supply(mint_address)
        return float(supply_info.get("uiAmount", 0) or 0)

    def get_account_info(self, address: str) -> dict[str, Any] | None:
        """
        Get account info for an address.

        Args:
            address: Account address

        Returns:
            Account info or None
        """
        result = self._request(
            "getAccountInfo",
            [address, {"encoding": "jsonParsed"}]
        )
        return result.get("value") if result else None

    def get_slot(self) -> int:
        """Get current slot number."""
        return self._request("getSlot", [])

    def get_block_time(self, slot: int) -> int | None:
        """
        Get Unix timestamp for a slot.

        Args:
            slot: Slot number

        Returns:
            Unix timestamp or None
        """
        return self._request("getBlockTime", [slot])

    def estimate_slot_for_timestamp(self, target_timestamp: int) -> int:
        """
        Estimate the slot number for a given timestamp.

        Solana produces ~2.5 slots per second on average.

        Args:
            target_timestamp: Unix timestamp

        Returns:
            Estimated slot number
        """
        import time

        current_slot = self.get_slot()
        current_time = int(time.time())

        # Average slot time is ~400ms
        slots_per_second = 2.5
        time_diff = current_time - target_timestamp
        slot_diff = int(time_diff * slots_per_second)

        estimated_slot = current_slot - slot_diff
        return max(0, estimated_slot)

    def get_signatures_for_address(
        self,
        address: str,
        limit: int = 1000,
        before: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get transaction signatures for an address.

        Args:
            address: Account address
            limit: Max signatures (max 1000)
            before: Get signatures before this one
            until: Get signatures until this one

        Returns:
            List of signature info
        """
        params: dict[str, Any] = {"limit": min(limit, 1000)}
        if before:
            params["before"] = before
        if until:
            params["until"] = until

        result = self._request("getSignaturesForAddress", [address, params])
        return result if result else []

    def get_transaction(
        self,
        signature: str,
        encoding: str = "jsonParsed",
    ) -> dict[str, Any] | None:
        """
        Get transaction details by signature.

        Args:
            signature: Transaction signature
            encoding: Response encoding

        Returns:
            Transaction data or None
        """
        result = self._request(
            "getTransaction",
            [signature, {"encoding": encoding, "maxSupportedTransactionVersion": 0}]
        )
        return result

    def get_multiple_transactions(
        self,
        signatures: list[str],
        encoding: str = "jsonParsed",
    ) -> list[dict[str, Any] | None]:
        """
        Get multiple transactions by signature.

        Note: Makes individual requests - no batch RPC support.

        Args:
            signatures: List of transaction signatures
            encoding: Response encoding

        Returns:
            List of transaction data (may contain None for failed lookups)
        """
        results = []
        for sig in signatures:
            try:
                tx = self.get_transaction(sig, encoding)
                results.append(tx)
            except APIError:
                results.append(None)
        return results

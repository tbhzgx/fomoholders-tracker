"""Helius API client for token holder lookups."""

from typing import Any

import httpx

from .base import BaseAPIClient, APIError


class HeliusClient(BaseAPIClient):
    """
    Client for Helius API (FREE tier: 1M credits/month).

    Primary use: getTokenAccounts to find all holders of a token
    and match by exact balance.
    """

    BASE_URL = "https://api.helius.xyz"
    RPC_BASE_URL = "https://mainnet.helius-rpc.com"

    def __init__(self, api_key: str):
        super().__init__(base_url=self.BASE_URL)
        self.api_key = api_key
        self.rpc_url = f"{self.RPC_BASE_URL}/?api-key={api_key}"

    def rpc_request(self, method: str, params: Any) -> Any:
        """
        Make a JSON-RPC request to Helius RPC endpoint.

        Args:
            method: RPC method name
            params: Method parameters (dict or list)

        Returns:
            RPC result
        """
        payload = {
            "jsonrpc": "2.0",
            "id": "wallet-tracker",
            "method": method,
            "params": params,
        }

        with httpx.Client(timeout=30.0) as client:
            response = client.post(self.rpc_url, json=payload)
            data = response.json()

            if "error" in data:
                raise APIError(f"RPC Error: {data['error']}")

            return data.get("result")

    def get_token_accounts(
        self,
        mint: str,
        page: int = 1,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """
        Get all token accounts for a mint address.

        Each account includes: address, owner, amount, decimals.

        Args:
            mint: Token mint address
            page: Page number (starts at 1)
            limit: Max results per page (max 1000)

        Returns:
            List of token account dicts
        """
        result = self.rpc_request(
            "getTokenAccounts",
            {
                "page": page,
                "limit": min(limit, 1000),
                "displayOptions": {},
                "mint": mint,
            },
        )

        if result and "token_accounts" in result:
            return result["token_accounts"]
        return []

    def get_all_holders(
        self,
        mint: str,
        max_pages: int = 50,
    ) -> list[dict[str, Any]]:
        """
        Paginate through ALL token holders for a mint.

        Args:
            mint: Token mint address
            max_pages: Safety limit on pages to fetch

        Returns:
            List of all token account dicts
        """
        all_accounts: list[dict[str, Any]] = []
        page = 1

        while page <= max_pages:
            accounts = self.get_token_accounts(mint, page=page, limit=1000)
            if not accounts:
                break
            all_accounts.extend(accounts)
            if len(accounts) < 1000:
                break
            page += 1

        return all_accounts

    def get_token_supply(self, mint: str) -> dict[str, Any]:
        """Get token supply info including decimals."""
        result = self.rpc_request(
            "getTokenSupply",
            [mint],
        )
        return result.get("value", {}) if result else {}

    # ------------------------------------------------------------------
    # Wallet API (identity, funded-by, balances)
    # ------------------------------------------------------------------

    def get_wallet_identity(self, address: str) -> dict[str, Any]:
        """
        Get identity info for a wallet (known exchange, protocol, etc.).

        Returns dict with: name, tags, type, etc.
        Returns empty dict if wallet is unknown.
        """
        try:
            return self.get(
                f"/v1/wallet/{address}/identity",
                params={"api-key": self.api_key},
            )
        except APIError:
            return {}

    def get_wallet_funded_by(self, address: str) -> dict[str, Any]:
        """
        Find the original funding source of a wallet.

        Returns dict with: funder, funderName, funderType, amount, date, etc.
        Returns empty dict if wallet has never received SOL.
        """
        try:
            return self.get(
                f"/v1/wallet/{address}/funded-by",
                params={"api-key": self.api_key},
            )
        except APIError:
            return {}

    def get_wallet_balances(
        self,
        address: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        """
        Get all token balances for a wallet with USD values.

        Returns dict with: tokens (list), totalUsdValue, pagination.
        """
        try:
            return self.get(
                f"/v1/wallet/{address}/balances",
                params={
                    "api-key": self.api_key,
                    "limit": min(limit, 100),
                    "showNfts": "false",
                },
            )
        except APIError:
            return {}

    # ------------------------------------------------------------------
    # Enhanced Transactions API (for swap/trade history)
    # ------------------------------------------------------------------

    def get_enhanced_transactions(
        self,
        signatures: list[str],
    ) -> list[dict[str, Any]]:
        """
        Parse transactions into human-readable enhanced format.

        Args:
            signatures: List of transaction signatures (max 100)

        Returns:
            List of enhanced transaction dicts
        """
        url = f"https://api-mainnet.helius-rpc.com/v0/transactions?api-key={self.api_key}"

        with httpx.Client(timeout=30.0) as client:
            response = client.post(url, json={"transactions": signatures[:100]})
            if response.status_code != 200:
                raise APIError(
                    f"Enhanced transactions error: {response.text}",
                    response.status_code,
                )
            return response.json()

    def get_swap_history(
        self,
        address: str,
        limit: int = 100,
        before: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get SWAP transaction history for a wallet address.

        Args:
            address: Wallet address
            limit: Max transactions to return (1-100)
            before: Pagination cursor (signature to start before)

        Returns:
            List of enhanced SWAP transactions
        """
        url = (
            f"https://api-mainnet.helius-rpc.com/v0/addresses/{address}/transactions"
            f"?api-key={self.api_key}"
            f"&type=SWAP"
            f"&limit={min(limit, 100)}"
        )
        if before:
            url += f"&before-signature={before}"

        with httpx.Client(timeout=30.0) as client:
            response = client.get(url)
            if response.status_code != 200:
                raise APIError(
                    f"Transaction history error: {response.text}",
                    response.status_code,
                )
            return response.json()

    def get_all_swaps_for_token(
        self,
        wallet_address: str,
        token_mint: str,
        max_pages: int = 5,
    ) -> list[dict[str, Any]]:
        """
        Get all SWAP transactions for a wallet involving a specific token.

        Args:
            wallet_address: Wallet address
            token_mint: Token mint address to filter swaps for
            max_pages: Safety limit on pages (100 txs per page)

        Returns:
            List of swap transactions involving the token
        """
        all_swaps: list[dict[str, Any]] = []
        before: str | None = None
        page = 0

        while page < max_pages:
            txs = self.get_swap_history(wallet_address, limit=100, before=before)
            if not txs:
                break

            # Filter to swaps involving our target token
            for tx in txs:
                swap = tx.get("events", {}).get("swap")
                if not swap:
                    continue
                # Check if this swap involves our token
                token_inputs = swap.get("tokenInputs", [])
                token_outputs = swap.get("tokenOutputs", [])
                mints_involved = {t.get("mint") for t in token_inputs + token_outputs}
                if token_mint in mints_involved:
                    all_swaps.append(tx)

            # Pagination
            if len(txs) < 100:
                break
            before = txs[-1].get("signature")
            if not before:
                break
            page += 1

        return all_swaps

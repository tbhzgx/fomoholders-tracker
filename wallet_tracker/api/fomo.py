"""Fomo.family API client for public SocialFi trade data."""

import time
import warnings
from typing import Any

import urllib3

from .base import APIError, RateLimitError

# Suppress SSL warnings (self-signed intermediate cert on Cloudflare-proxied endpoints)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# Network IDs used by Fomo
FOMO_NETWORK_IDS = {
    "solana": 1399811149,
    "base": 8453,
    "bsc": 56,
}
FOMO_NETWORK_IDS_REVERSE = {v: k for k, v in FOMO_NETWORK_IDS.items()}

# Chains advertised in x-supported-chains header
_SUPPORTED_CHAINS = "1399811149,8453,56,143"

# Privy app ID for fomo.family (from JWT aud field)
_PRIVY_APP_ID = "cm6h485o300n3zj9yl6vpedq7"


class FomoClient:
    """
    Client for the fomo.family public API.

    Authentication: Privy.io JWT (60-min access token) auto-refreshed using
    a long-lived refresh token captured once from app traffic.

    If only a bearer_token is supplied (no refresh_token), requests work until
    the JWT expires (~60 min). With a refresh_token the client auto-refreshes
    silently and runs indefinitely.

    Uses cloudscraper to bypass Cloudflare bot protection automatically.

    Key endpoints:
        GET /trades                         — user trade history
        GET /feed/token                     — public activity feed for a token
        GET /v2/users/fuzzy-search          — search users by name/handle
        GET /leaderboard                    — global leaderboard
    """

    BASE_URL = "https://prod-api.fomo.family"
    PRIVY_URL = "https://auth.privy.io"

    def __init__(
        self,
        bearer_token: str,
        installation_id: str | None = None,
        refresh_token: str | None = None,
    ):
        self.bearer_token = bearer_token
        self.refresh_token = refresh_token
        self.installation_id = installation_id or "install_wallet_tracker"
        self._token_exp: float = self._parse_exp(bearer_token)

        # Use plain requests with SSL verification disabled.
        # The Bearer JWT is what grants API access — cloudscraper's Cloudflare bypass
        # is not needed and causes IP-level blocks when running server-side.
        import ssl, requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.ssl_ import create_urllib3_context

        class _NoVerifyAdapter(HTTPAdapter):
            def init_poolmanager(self, *args, **kwargs):
                ctx = create_urllib3_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                kwargs["ssl_context"] = ctx
                super().init_poolmanager(*args, **kwargs)

        self._scraper = requests.Session()
        self._scraper.mount("https://", _NoVerifyAdapter())
        self._scraper.mount("http://", _NoVerifyAdapter())

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_exp(token: str) -> float:
        """Decode JWT expiry without a library."""
        import base64
        import json
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            data = json.loads(base64.b64decode(payload))
            return float(data.get("exp", 0))
        except Exception:
            return 0.0

    def _is_token_expired(self) -> bool:
        """Return True if the access token expires within the next 5 minutes."""
        return time.time() > (self._token_exp - 300)

    def _refresh_access_token(self) -> bool:
        """
        Use the Privy refresh token to obtain a new access token.
        Returns True on success, False if refresh fails.
        """
        if not self.refresh_token:
            return False
        try:
            resp = self._scraper.post(
                f"{self.PRIVY_URL}/api/v1/sessions",
                headers={
                    "privy-app-id": _PRIVY_APP_ID,
                    "content-type": "application/json",
                    "origin": "https://fomo.family",
                    "referer": "https://fomo.family/",
                },
                json={"refresh_token": self.refresh_token},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                new_token = data.get("token") or data.get("access_token")
                new_refresh = data.get("refresh_token")
                if new_token:
                    self.bearer_token = new_token
                    self._token_exp = self._parse_exp(new_token)
                    if new_refresh:
                        self.refresh_token = new_refresh
                    return True
        except Exception:
            pass
        return False

    def _ensure_fresh_token(self):
        """Refresh the access token if it's about to expire."""
        if self._is_token_expired():
            self._refresh_access_token()

    # ------------------------------------------------------------------
    # Core HTTP
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "accept": "*/*",
            "authorization": f"Bearer {self.bearer_token}",
            "x-app-installation-id": self.installation_id,
            "x-supported-chains": _SUPPORTED_CHAINS,
            "user-agent": "fomo/1.61.1/283/ios/iPhone 14 Pro/Apple",
            "accept-language": "en-GB,en;q=0.9",
        }

    def _request(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        json_data: dict | None = None,
        retries: int = 3,
    ) -> dict[str, Any]:
        self._ensure_fresh_token()
        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                resp = self._scraper.request(
                    method,
                    url,
                    params=params,
                    json=json_data,
                    headers=self._headers(),
                    timeout=30,
                )
                if resp.status_code == 401 and self.refresh_token:
                    # Token rejected — try refreshing immediately
                    if self._refresh_access_token():
                        continue
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    raise RateLimitError("Rate limit exceeded")
                if resp.status_code >= 400:
                    try:
                        msg = resp.json().get("message") or resp.json().get("error") or resp.text
                    except Exception:
                        msg = resp.text or f"HTTP {resp.status_code}"
                    raise APIError(msg, resp.status_code)
                return resp.json()
            except (RateLimitError, APIError):
                raise
            except Exception as e:
                last_err = e
                time.sleep(1)
        raise APIError(f"Request failed after {retries} retries: {last_err}")

    def get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        return self._request("GET", f"{self.BASE_URL}{path}", params=params)

    def close(self) -> None:
        self._scraper.close()

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------

    def get_user_trades(
        self,
        user_id: str,
        order_by: str = "realizedPnlUsd",
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        """
        Fetch a user's trade history sorted by PnL.

        Returns dict with keys: activeTrades, closedTrades, hasNextPage
        Each trade has: id, userAddress, tokenAddress, networkId, realizedPnlUsd,
        unrealizedPnlUsd, totalCostBasis, avgEntryPrice, avgExitPrice,
        humanTokenAmount, tokenMetadata (symbol, currentPrice, liquidity, imageLargeUrl)
        """
        params: dict[str, Any] = {
            "userId": user_id,
            "orderBy": order_by,
            "limit": limit,
            "offset": offset,
        }
        data = self.get("/trades", params=params)
        return data.get("responseObject", {})

    def get_all_user_trades(
        self,
        user_id: str,
        order_by: str = "realizedPnlUsd",
        page_size: int = 25,
        max_pages: int = 10,
    ) -> tuple[list[dict], list[dict]]:
        """
        Paginate through all of a user's trades.

        Returns (active_trades, closed_trades).
        Note: the API ignores offset and always returns the same top results,
        so we stop as soon as we see a duplicate trade ID.
        """
        active: list[dict] = []
        closed: list[dict] = []
        seen_ids: set[str] = set()

        for page in range(max_pages):
            result = self.get_user_trades(
                user_id,
                order_by=order_by,
                limit=page_size,
                offset=page * page_size,
            )
            new_items = False
            for item in result.get("activeTrades", []):
                tid = (item.get("trade") or {}).get("id") or item.get("id")
                if tid and tid not in seen_ids:
                    seen_ids.add(tid)
                    active.append(item)
                    new_items = True
            for item in result.get("closedTrades", []):
                tid = (item.get("trade") or {}).get("id") or item.get("id")
                if tid and tid not in seen_ids:
                    seen_ids.add(tid)
                    closed.append(item)
                    new_items = True
            if not new_items or not result.get("hasNextPage", False):
                break

        return active, closed

    def get_user_trades_for_token(
        self,
        user_id: str,
        token_address: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """
        Fetch a user's trades for a specific token.

        Returns dict with keys: activeTrades, closedTrades, hasNextPage
        """
        params: dict[str, Any] = {
            "userId": user_id,
            "tokenAddress": token_address,
            "limit": limit,
        }
        data = self.get("/trades", params=params)
        return data.get("responseObject", {})

    # ------------------------------------------------------------------
    # Token feed (public activity)
    # ------------------------------------------------------------------

    def get_token_feed(
        self,
        token_address: str,
        network_id: int | str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """
        Get the public activity feed for a token — all traders who bought/sold.

        Each item has: type (swap_buy/swap_sell), userId, displayName, userHandle,
        usdAmount, marketCap, createdAt, tradeId, profilePictureLink
        """
        params: dict[str, Any] = {
            "tokenAddress": token_address,
            "networkId": network_id,
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        data = self.get("/feed/token", params=params)
        return data.get("responseObject", {})

    def get_all_token_traders(
        self,
        token_address: str,
        network_id: int | str,
        max_pages: int = 5,
    ) -> list[dict]:
        """
        Paginate token feed to collect all unique traders.

        Returns a list of unique feed items (deduped by userId).
        """
        seen_users: set[str] = set()
        all_items: list[dict] = []
        cursor = None

        for _ in range(max_pages):
            result = self.get_token_feed(
                token_address, network_id, limit=50, cursor=cursor
            )
            items = result.get("items", [])
            if not items:
                break

            for item in items:
                uid = item.get("userId")
                if uid and uid not in seen_users:
                    seen_users.add(uid)
                    all_items.append(item)

            # Cursor-based pagination
            cursor = result.get("nextCursor")
            if not cursor:
                break

        return all_items

    # ------------------------------------------------------------------
    # User search
    # ------------------------------------------------------------------

    def search_users(self, search_term: str) -> list[dict[str, Any]]:
        """
        Fuzzy search fomo users by display name or handle.

        Each result has: id, address (Solana), evmAddress, displayName,
        userHandle, profilePictureLink, description, followers, following,
        numTrades, totalVolume, swapCount
        """
        data = self.get("/v2/users/fuzzy-search", params={"searchTerm": search_term})
        return data.get("responseObject", {}).get("users", [])

    def get_user_wallets(self, user_id: str) -> tuple[set[str], set[str]]:
        """
        Fetch all wallets linked to a user via /v2/users/{id}/balances.
        Returns (solana_wallets, evm_wallets) as sets of addresses.
        This is more reliable than trade data as it reflects all connected wallets.
        """
        try:
            data = self.get(f"/v2/users/{user_id}/balances")
            balances = data.get("responseObject", {}).get("balances", [])
        except Exception:
            return set(), set()

        sol_wallets: set[str] = set()
        evm_wallets: set[str] = set()
        for item in balances:
            addr = item.get("balance", {}).get("address", "")
            wallet_id = item.get("balance", {}).get("walletId", "")
            # walletId format: "{address}:{networkId}"
            network_id_str = wallet_id.split(":")[-1] if ":" in wallet_id else ""
            try:
                nid = int(network_id_str)
            except ValueError:
                nid = 0
            if addr:
                if nid == FOMO_NETWORK_IDS["solana"]:
                    sol_wallets.add(addr)
                elif nid in (FOMO_NETWORK_IDS["base"], FOMO_NETWORK_IDS["bsc"]):
                    evm_wallets.add(addr)
        return sol_wallets, evm_wallets

    # ------------------------------------------------------------------
    # User profile (built from trade data — no dedicated profile endpoint)
    # ------------------------------------------------------------------

    def get_user_summary(
        self,
        user_id: str,
        profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Build a user summary combining search profile data and trade history.

        Args:
            user_id: Fomo user UUID.
            profile: Optional pre-fetched dict from search_users() — supplies
                     displayName, userHandle, profilePictureLink, address,
                     evmAddress, description, followers, following, numTrades,
                     totalVolume.
        """
        # Fetch active trades and top closed trades by PnL (best lifetime trades).
        # Also fetch most recent closed trades separately for an accurate recent PnL sum
        # since the API ignores offset and always returns the same top results.
        active_result = self.get_user_trades(user_id, order_by="realizedPnlUsd", limit=25)
        top_closed_result = self.get_user_trades(user_id, order_by="realizedPnlUsd", limit=25)
        recent_closed_result = self.get_user_trades(user_id, order_by="closedAt", limit=25)
        active = active_result.get("activeTrades", [])
        closed = top_closed_result.get("closedTrades", [])  # best lifetime trades for display
        recent_closed = recent_closed_result.get("closedTrades", [])  # recent trades for PnL sum
        all_trades = [t["trade"] for t in active + recent_closed if "trade" in t]

        # Derive wallet addresses from actual trade data first — the profile
        # address field is the linked/registered wallet which may be empty or
        # different from the wallet the user actually trades from.
        solana_wallet: str | None = None
        evm_wallet: str | None = None

        p = profile or {}
        # Use profile pre-computed PnL when available (most accurate)
        _profile_pnl = p.get("totalPnL")
        _profile_pnl30d = p.get("pnl30d")
        use_profile_pnl = _profile_pnl is not None or _profile_pnl30d is not None
        total_realized: float = float(_profile_pnl or _profile_pnl30d or 0)
        total_unrealized = 0.0
        pnl_label = "30d PnL" if use_profile_pnl else "Recent PnL"

        trade_solana_wallet: str | None = None
        trade_evm_wallet: str | None = None

        for trade in all_trades:
            addr = trade.get("userAddress", "")
            net = trade.get("networkId")
            if not trade_solana_wallet and addr and net == FOMO_NETWORK_IDS["solana"]:
                trade_solana_wallet = addr
            elif not trade_evm_wallet and addr and net in (FOMO_NETWORK_IDS["base"], FOMO_NETWORK_IDS["bsc"]):
                trade_evm_wallet = addr
            total_unrealized += float(trade.get("unrealizedPnlUsd") or 0)
            # Only sum realized from trades if profile has no pre-computed PnL
            if not use_profile_pnl:
                total_realized += float(trade.get("realizedPnlUsd") or 0)

        profile_solana = p.get("address")
        profile_evm = p.get("evmAddress")

        # Use /v2/users/{id}/balances as primary wallet source — most accurate,
        # reflects all connected wallets including ones not seen in recent trades.
        balance_sol_wallets, balance_evm_wallets = self.get_user_wallets(user_id)
        balance_solana = next(iter(balance_sol_wallets), None)
        balance_evm = next(iter(balance_evm_wallets), None)

        # Priority: balances endpoint > trade wallet > profile address
        solana_wallet = balance_solana or trade_solana_wallet or profile_solana
        evm_wallet = balance_evm or trade_evm_wallet or profile_evm

        # Collect all unique extra wallets to surface
        all_sol = {w for w in [balance_solana, trade_solana_wallet, profile_solana] if w}
        all_evm = {w for w in [balance_evm, trade_evm_wallet, profile_evm] if w}
        extra_solana = next((w for w in all_sol if w != solana_wallet), None)
        extra_evm = next((w for w in all_evm if w != evm_wallet), None)

        # Top holdings deduped by symbol, sorted by cost basis
        seen_syms: set[str] = set()
        top_holdings = []
        for t in sorted(
            [t["trade"] for t in active if "trade" in t],
            key=lambda t: float(t.get("totalCostBasis") or 0),
            reverse=True,
        ):
            sym = t.get("tokenMetadata", {}).get("symbol", t.get("tokenAddress", "?"))
            if sym not in seen_syms:
                seen_syms.add(sym)
                top_holdings.append(t)
            if len(top_holdings) >= 5:
                break

        # Top closed trades deduped by symbol, sorted by PnL
        seen_syms_c: set[str] = set()
        top_closed = []
        for t in sorted(
            [t["trade"] for t in closed if "trade" in t],
            key=lambda t: float(t.get("realizedPnlUsd") or 0),
            reverse=True,
        ):
            sym = t.get("tokenMetadata", {}).get("symbol", t.get("tokenAddress", "?"))
            if sym not in seen_syms_c:
                seen_syms_c.add(sym)
                top_closed.append(t)
            if len(top_closed) >= 5:
                break

        return {
            "userId": user_id,
            "displayName": p.get("displayName"),
            "userHandle": p.get("userHandle"),
            "profilePictureLink": p.get("profilePictureLink"),
            "description": p.get("description"),
            "followers": p.get("followers", 0),
            "following": p.get("following", 0),
            "numTrades": p.get("numTrades", len(active) + len(closed)),
            "totalVolume": p.get("totalVolume", 0),
            "solana_wallet": solana_wallet,
            "evm_wallet": evm_wallet,
            "extra_solana_wallet": extra_solana,
            "extra_evm_wallet": extra_evm,
            "totalRealizedPnlUsd": total_realized,
            "totalUnrealizedPnlUsd": total_unrealized,
            "pnl_label": pnl_label,
            "pnl24h": p.get("pnl24h"),
            "pnl7d": p.get("pnl7d"),
            "pnl30d": p.get("pnl30d"),
            "top_holdings": top_holdings,
            "top_closed": top_closed,
            "active_count": len(active),
            "closed_count": len(closed),
        }

    def enrich_with_feed_identity(
        self,
        user_id: str,
        token_address: str,
        network_id: int,
    ) -> dict[str, Any]:
        """
        Pull displayName, userHandle, profilePictureLink for a userId
        by looking them up in a token's feed. Returns empty dict if not found.
        """
        result = self.get_token_feed(token_address, network_id, limit=50)
        for item in result.get("items", []):
            if item.get("userId") == user_id:
                return {
                    "displayName": item.get("displayName"),
                    "userHandle": item.get("userHandle"),
                    "profilePictureLink": item.get("profilePictureLink"),
                }
        return {}

    # ------------------------------------------------------------------
    # Token holders (via /hodlers/top)
    # ------------------------------------------------------------------

    def get_token_holders_with_pnl(
        self,
        token_address: str,
        network_id: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """
        Get all fomo holders for a token using the /hodlers/top endpoint.
        Already sorted by current position value descending.

        Returns (holders, token_image_url).

        Each holder has:
            userId, displayName, userHandle, profilePictureLink,
            solana_wallet, evm_wallet, networkId,
            realizedPnlUsd, unrealizedPnlUsd, totalCostBasis,
            positionValue, stillHolding
        """
        import json as _json
        tokens_param = _json.dumps([{"address": token_address, "networkId": network_id}])
        data = self.get("/hodlers/top", params={"tokens": tokens_param})
        results = data.get("responseObject", [])
        if not results:
            return [], None

        result_obj = results[0]
        top_holders = result_obj.get("topHolders", [])
        if not top_holders:
            return [], None

        # Pull token image from the response object
        token_data = result_obj.get("token") or result_obj.get("tokenMetadata") or {}
        token_image_url: str | None = (
            token_data.get("imageLargeUrl")
            or token_data.get("imageUrl")
            or token_data.get("image")
            or result_obj.get("imageLargeUrl")
            or result_obj.get("imageUrl")
            or None
        )
        # Fallback: try to get image from the first holder's token metadata in their trade
        if not token_image_url and top_holders:
            first_h = top_holders[0]
            first_meta = (
                first_h.get("tokenMetadata")
                or first_h.get("token")
                or {}
            )
            token_image_url = (
                first_meta.get("imageLargeUrl")
                or first_meta.get("imageUrl")
                or first_meta.get("image")
                or None
            )
        enriched = []
        for h in top_holders:
            user = h.get("user", {})
            uid = user.get("id", "")
            value = float(h.get("value") or 0)
            still_holding = value > 0

            # Wallet: use address field, falling back to evmAddress
            sol_wallet = user.get("address") if network_id == FOMO_NETWORK_IDS["solana"] else None
            evm_wallet = user.get("evmAddress") if network_id != FOMO_NETWORK_IDS["solana"] else None
            if not sol_wallet and not evm_wallet:
                sol_wallet = user.get("address")

            enriched.append({
                "userId": uid,
                "displayName": user.get("displayName") or user.get("userHandle") or uid[:8],
                "userHandle": user.get("userHandle"),
                "profilePictureLink": user.get("profilePictureLink"),
                "networkId": network_id,
                "solana_wallet": sol_wallet,
                "evm_wallet": evm_wallet,
                "realizedPnlUsd": float(h.get("realizedPnl") or 0),
                "unrealizedPnlUsd": float(h.get("unrealizedPnl") or 0),
                "totalCostBasis": float(h.get("costBasis") or 0),
                "positionValue": value,
                "stillHolding": still_holding,
                "tradeId": h.get("tradeId"),
                "latestComment": h.get("comment"),
            })

        return enriched, token_image_url

    def get_trade_comments(self, trade_id: str) -> list[dict[str, Any]]:
        """
        Fetch all comments for a trade position.

        Each comment has: id, userId, tradeId, comment, createdAt,
        numLikes, parentId, olderThesis, newerThesis
        """
        data = self.get(f"/trades/{trade_id}/comments")
        return data.get("responseObject", {}).get("comments", [])

    def get_token_comments(
        self,
        token_address: str,
        network_id: int,
    ) -> list[dict[str, Any]]:
        """
        Get all holder comments for a token, enriched with user info.
        Returns list of comment dicts with user display info attached.
        """
        import json as _json
        tokens_param = _json.dumps([{"address": token_address, "networkId": network_id}])
        data = self.get("/hodlers/top", params={"tokens": tokens_param})
        results = data.get("responseObject", [])
        if not results:
            return []

        comments_out = []
        for h in results[0].get("topHolders", []):
            trade_id = h.get("tradeId")
            if not trade_id:
                continue
            user = h.get("user", {})
            try:
                comments = self.get_trade_comments(trade_id)
            except Exception:
                comments = []
            for c in comments:
                comments_out.append({
                    "displayName": user.get("displayName") or user.get("userHandle", ""),
                    "userHandle": user.get("userHandle"),
                    "profilePictureLink": user.get("profilePictureLink"),
                    "positionValue": float(h.get("value") or 0),
                    "comment": c.get("comment", ""),
                    "numLikes": c.get("numLikes", 0),
                    "createdAt": c.get("createdAt", ""),
                    "tradeId": trade_id,
                    "commentId": c.get("id"),
                })

        # Sort by likes descending
        comments_out.sort(key=lambda x: x["numLikes"], reverse=True)
        return comments_out

    # ------------------------------------------------------------------
    # Leaderboard
    # ------------------------------------------------------------------

    def get_leaderboard(
        self,
        period: str = "all",
        network_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """
        Fetch the global leaderboard sorted by realized PnL.

        Args:
            period: "all", "7d", "30d"
            network_id: filter by chain (1399811149=Solana, 8453=Base, 56=BSC)
        """
        params: dict[str, Any] = {
            "period": period,
            "limit": limit,
            "offset": offset,
        }
        if network_id is not None:
            params["networkId"] = network_id
        data = self.get("/leaderboard", params=params)
        return data.get("responseObject", {}).get("items", [])

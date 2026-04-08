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

        for trade in all_trades:
            addr = trade.get("userAddress", "")
            net = trade.get("networkId")
            if not solana_wallet and addr and net == FOMO_NETWORK_IDS["solana"]:
                solana_wallet = addr
            elif not evm_wallet and addr and net in (FOMO_NETWORK_IDS["base"], FOMO_NETWORK_IDS["bsc"]):
                evm_wallet = addr
            total_unrealized += float(trade.get("unrealizedPnlUsd") or 0)
            # Only sum realized from trades if profile has no pre-computed PnL
            if not use_profile_pnl:
                total_realized += float(trade.get("realizedPnlUsd") or 0)

        # Fall back to profile addresses if not found in trades
        if not solana_wallet:
            solana_wallet = (profile or {}).get("address")
        if not evm_wallet:
            evm_wallet = (profile or {}).get("evmAddress")

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
    # Token holders (from feed)
    # ------------------------------------------------------------------

    def get_token_holders_with_pnl(
        self,
        token_address: str,
        network_id: int,
        top_n: int = 10,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """
        Get top N unique traders from a token's feed, enriched with their
        per-token PnL from /trades.

        Returns (holders, token_image_url).

        Each holder has:
            userId, displayName, userHandle, profilePictureLink,
            solana_wallet, evm_wallet, networkId,
            realizedPnlUsd, unrealizedPnlUsd, totalCostBasis,
            stillHolding, usdAmount
        """
        # Collect unique traders from feed (buy events first for entry prices)
        seen: dict[str, dict] = {}
        cursor = None

        for _ in range(10):  # max 10 pages to find top_n unique users
            result = self.get_token_feed(token_address, network_id, limit=50, cursor=cursor)
            for item in result.get("items", []):
                uid = item.get("userId")
                if uid and uid not in seen:
                    seen[uid] = item
                if len(seen) >= top_n * 3:  # gather 3x to filter by PnL
                    break
            cursor = result.get("nextCursor")
            if not cursor or len(seen) >= top_n * 3:
                break

        if not seen:
            return [], None

        # Enrich each trader with their per-token trade data
        token_image_url: str | None = None
        enriched = []
        for uid, feed_item in list(seen.items())[:top_n * 3]:
            try:
                trade_data = self.get_user_trades_for_token(uid, token_address, limit=5)
            except Exception:
                trade_data = {}

            all_token_trades = (
                [t["trade"] for t in trade_data.get("activeTrades", []) if "trade" in t]
                + [t["trade"] for t in trade_data.get("closedTrades", []) if "trade" in t]
            )

            # Grab token image from first trade that has it
            if not token_image_url and all_token_trades:
                token_image_url = all_token_trades[0].get("tokenMetadata", {}).get("imageLargeUrl")

            # Aggregate PnL across all positions for this token
            realized = sum(float(t.get("realizedPnlUsd") or 0) for t in all_token_trades)
            unrealized = sum(float(t.get("unrealizedPnlUsd") or 0) for t in all_token_trades)
            cost_basis = sum(float(t.get("totalCostBasis") or 0) for t in all_token_trades)
            still_holding = any(
                float(t.get("humanTokenAmount") or 0) > 0 for t in all_token_trades
            )

            # Wallet addresses
            solana_wallet = next(
                (t.get("userAddress") for t in all_token_trades
                 if t.get("networkId") == FOMO_NETWORK_IDS["solana"]), None
            )
            evm_wallet = next(
                (t.get("userAddress") for t in all_token_trades
                 if t.get("networkId") in (FOMO_NETWORK_IDS["base"], FOMO_NETWORK_IDS["bsc"])), None
            )

            enriched.append({
                "userId": uid,
                "displayName": feed_item.get("displayName") or uid[:8],
                "userHandle": feed_item.get("userHandle"),
                "profilePictureLink": feed_item.get("profilePictureLink"),
                "networkId": network_id,
                "solana_wallet": solana_wallet,
                "evm_wallet": evm_wallet,
                "realizedPnlUsd": realized,
                "unrealizedPnlUsd": unrealized,
                "totalCostBasis": cost_basis,
                "stillHolding": still_holding,
                "usdAmount": float(feed_item.get("usdAmount") or 0),
            })

        # Sort by realized PnL descending, return top N
        enriched.sort(key=lambda x: x["realizedPnlUsd"], reverse=True)
        return enriched[:top_n], token_image_url

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

# src/auth.py
"""
Authentication Module

Handles Fyers API authentication including TOTP verification,
token management, and session caching.

Author: Chandrakant Jagtap
Version: 5.0.0 (Simplified)
"""

import logging
import os
import time
from typing import Dict, Optional, Any
from urllib import parse

import pyotp
import requests
from dotenv import dotenv_values
from fyers_apiv3 import fyersModel

from src.config import (
    get_current_time,
    TOKEN_DIR,
    MIN_TOKEN_LENGTH,
    API_TIMEOUT,
    TOTP_MAX_RETRIES,
    TOTP_RETRY_DELAY,
    AUTH_MAX_RETRIES,
    FYERS_SUCCESS_CODE,
)


class AuthenticationError(Exception):
    """Authentication failure exception."""

    pass


class FyersAuth:
    """
    Handles complete Fyers authentication and token management.

    Combines authentication flow, token caching, and session management
    into a single cohesive class.

    Usage:
        auth = FyersAuth()
        fyers_client = auth.get_client()
    """

    # API endpoints
    BASE_URL = "https://api-t2.fyers.in/vagator/v2"
    BASE_URL_2 = "https://api-t1.fyers.in/api/v3"

    # Circuit breaker settings
    MAX_AUTH_FAILURES = 5
    AUTH_FAILURE_PAUSE = 300  # 5 minutes

    def __init__(self, env_file: str = "config/.env") -> None:
        """
        Initialize authentication manager.

        Args:
            env_file: Path to environment file with credentials
        """
        self._env_file = env_file
        self._credentials = self._load_credentials()

        # Token state
        self._fyers_client: Optional[fyersModel.FyersModel] = None
        self._last_auth_time: Optional[float] = None

        # Circuit breaker state
        self._auth_failures = 0
        self._last_failure_time: Optional[float] = None

        # Ensure token directory exists
        os.makedirs(TOKEN_DIR, exist_ok=True)

    # =========================================================================
    # PUBLIC API
    # =========================================================================

    def get_client(self) -> Optional[fyersModel.FyersModel]:
        """
        Get authenticated Fyers client.

        Implements three-tier strategy:
        1. Return cached client if valid
        2. Load and validate token from file
        3. Perform fresh authentication

        Returns:
            FyersModel instance or None if authentication fails
        """
        # Check circuit breaker
        if self._is_circuit_open():
            logging.error("Auth circuit breaker open - waiting for reset")
            return None

        # Try cached client
        if self._fyers_client and self._is_token_valid():
            if self._validate_client(self._fyers_client):
                logging.debug("Using cached Fyers client")
                return self._fyers_client
            self._reset_client()

        # Try saved token
        client = self._try_saved_token()
        if client:
            return client

        # Fresh authentication
        return self._authenticate_fresh()

    def force_reauthenticate(self) -> Optional[fyersModel.FyersModel]:
        """
        Force fresh authentication regardless of cached state.

        Returns:
            FyersModel instance or None
        """
        logging.info("Forcing fresh authentication...")
        self._reset_client()
        self._delete_token_file()
        self._auth_failures = 0  # Reset circuit breaker
        return self.get_client()

    @property
    def client_id(self) -> str:
        """Get Fyers client ID."""
        return f"{self._credentials['app_id']}-100"

    def is_authenticated(self) -> bool:
        """Check if currently authenticated with valid client."""
        return self._fyers_client is not None and self._validate_client(
            self._fyers_client
        )

    # =========================================================================
    # CREDENTIAL MANAGEMENT
    # =========================================================================

    def _load_credentials(self) -> Dict[str, str]:
        """
        Load credentials from environment file.

        Returns:
            Dictionary with credentials

        Raises:
            AuthenticationError: If credentials missing or invalid
        """
        secrets = dotenv_values(self._env_file)

        if not secrets:
            raise AuthenticationError(
                f"Failed to load credentials from {self._env_file}"
            )

        required = [
            "fyers_id",
            "app_id",
            "totp_key",
            "pin",
            "redirect_url",
            "app_type",
            "app_id_hash",
        ]
        missing = [k for k in required if not secrets.get(k)]

        if missing:
            raise AuthenticationError(f"Missing credentials: {', '.join(missing)}")

        logging.info("Credentials loaded for: %s", secrets["fyers_id"])
        return secrets

    # =========================================================================
    # TOKEN FILE MANAGEMENT
    # =========================================================================

    def _get_token_filename(self) -> str:
        """Get token filename for today."""
        today = get_current_time().date()
        return f"{TOKEN_DIR}/access-{today}.txt"

    def _read_token_file(self) -> Optional[str]:
        """Read token from file if valid."""
        filename = self._get_token_filename()

        if not os.path.exists(filename):
            return None

        try:
            with open(filename, "r", encoding="utf-8") as f:
                token = f.read().strip()

            if token and len(token) >= MIN_TOKEN_LENGTH:
                return token

            logging.warning("Invalid token in file")
            return None

        except IOError as exc:
            logging.error("Failed to read token file: %s", exc)
            return None

    def _write_token_file(self, token: str) -> bool:
        """Write token to file atomically."""
        filename = self._get_token_filename()
        temp_file = f"{filename}.tmp"

        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                f.write(token)
                f.flush()
                os.fsync(f.fileno())

            os.replace(temp_file, filename)
            logging.info("Token saved to: %s", filename)
            return True

        except IOError as exc:
            logging.error("Failed to save token: %s", exc)
            return False

    def _delete_token_file(self) -> None:
        """Delete token file if exists."""
        filename = self._get_token_filename()
        if os.path.exists(filename):
            try:
                os.remove(filename)
            except OSError:
                pass

    # =========================================================================
    # TOKEN VALIDATION
    # =========================================================================

    def _try_saved_token(self) -> Optional[fyersModel.FyersModel]:
        """Try to use saved token from file."""
        token = self._read_token_file()
        if not token:
            return None

        logging.info("Attempting to use saved token")

        try:
            client = fyersModel.FyersModel(
                client_id=self.client_id,
                is_async=False,
                token=token,
                log_path="data/logs/",
            )

            if self._validate_client(client):
                self._fyers_client = client
                self._last_auth_time = time.time()
                self._record_auth_success()
                logging.info("Saved token validated successfully")
                return client

            logging.warning("Saved token validation failed")
            self._delete_token_file()
            return None

        except Exception as exc:
            logging.error("Error using saved token: %s", exc)
            self._delete_token_file()
            return None

    def _validate_client(self, client: fyersModel.FyersModel) -> bool:
        """Validate client by calling profile API."""
        try:
            profile = client.get_profile()
            return profile.get("code") == FYERS_SUCCESS_CODE
        except Exception as exc:
            logging.warning("Client validation failed: %s", exc)
            return False

    def _is_token_valid(self) -> bool:
        """Check if current token is within valid timeframe."""
        if self._last_auth_time is None:
            return False

        age = time.time() - self._last_auth_time
        max_age = 86400 - 3600  # 23 hours (24h validity - 1h buffer)
        return age < max_age

    def _reset_client(self) -> None:
        """Reset client state."""
        self._fyers_client = None
        self._last_auth_time = None

    # =========================================================================
    # CIRCUIT BREAKER
    # =========================================================================

    def _is_circuit_open(self) -> bool:
        """Check if circuit breaker is open (blocking auth attempts)."""
        if self._auth_failures < self.MAX_AUTH_FAILURES:
            return False

        if self._last_failure_time is None:
            return False

        elapsed = time.time() - self._last_failure_time
        if elapsed >= self.AUTH_FAILURE_PAUSE:
            logging.info("Circuit breaker reset after %d seconds", int(elapsed))
            self._auth_failures = 0
            return False

        return True

    def _record_auth_success(self) -> None:
        """Record successful authentication."""
        self._auth_failures = 0
        self._last_failure_time = None

    def _record_auth_failure(self) -> None:
        """Record failed authentication."""
        self._auth_failures += 1
        self._last_failure_time = time.time()

        if self._auth_failures >= self.MAX_AUTH_FAILURES:
            logging.warning(
                "Circuit breaker OPEN after %d failures. Pausing for %d seconds.",
                self._auth_failures,
                self.AUTH_FAILURE_PAUSE,
            )

    # =========================================================================
    # FRESH AUTHENTICATION
    # =========================================================================

    def _authenticate_fresh(self) -> Optional[fyersModel.FyersModel]:
        """
        Perform fresh authentication flow.

        Flow: Request Key → TOTP → PIN → Auth Code → Access Token

        Returns:
            FyersModel instance or None
        """
        print("\n🔐 Starting Fyers Authentication...")
        logging.info("Starting fresh authentication")

        try:
            # Step 1: Get request key
            print("📝 Step 1/5: Getting request key...")
            request_key = self._get_request_key()

            # Step 2: Verify TOTP
            print("🔐 Step 2/5: Verifying TOTP...")
            verify_key = self._verify_totp_with_retry(request_key)

            # Step 3: Verify PIN
            print("🔑 Step 3/5: Verifying PIN...")
            access_token = self._verify_pin(verify_key)

            # Step 4: Get auth code
            print("📄 Step 4/5: Getting authorization code...")
            auth_code = self._get_auth_code(access_token)

            # Step 5: Get final access token
            print("🎫 Step 5/5: Getting access token...")
            final_token = self._get_access_token(auth_code)

            # Create client
            client = fyersModel.FyersModel(
                client_id=self.client_id,
                is_async=False,
                token=final_token,
                log_path="data/logs/",
            )

            # Validate and save
            if self._validate_client(client):
                self._fyers_client = client
                self._last_auth_time = time.time()
                self._write_token_file(final_token)
                self._record_auth_success()

                print("✅ Authentication successful!")
                logging.info("Fresh authentication completed successfully")
                return client

            logging.error("Fresh authentication validation failed")
            self._record_auth_failure()
            return None

        except Exception as exc:
            print(f"❌ Authentication failed: {exc}")
            logging.error("Authentication failed: %s", exc)
            self._record_auth_failure()
            return None

    def _get_request_key(self) -> str:
        """Get request key for authentication."""
        url = f"{self.BASE_URL}/send_login_otp"
        data = {"fy_id": self._credentials["fyers_id"], "app_id": "2"}

        response = self._api_request("POST", url, data)

        request_key = response.get("request_key")
        if not request_key:
            raise AuthenticationError(f"No request_key in response: {response}")

        return request_key

    def _verify_totp_with_retry(self, request_key: str) -> str:
        """Verify TOTP with retry logic."""
        last_error = None

        for attempt in range(TOTP_MAX_RETRIES):
            try:
                # Wait briefly to ensure TOTP validity
                time.sleep(1)

                # Generate TOTP
                totp = pyotp.TOTP(self._credentials["totp_key"]).now()
                logging.debug("Generated TOTP for attempt %d", attempt + 1)

                # Verify
                url = f"{self.BASE_URL}/verify_otp"
                data = {"request_key": request_key, "otp": totp}
                response = self._api_request("POST", url, data)

                verify_key = response.get("request_key")
                if verify_key:
                    return verify_key

                raise AuthenticationError(f"No verify key in response: {response}")

            except Exception as exc:
                last_error = exc
                logging.warning("TOTP attempt %d failed: %s", attempt + 1, exc)

                if attempt < TOTP_MAX_RETRIES - 1:
                    print(f"   ⚠️ Retrying in {TOTP_RETRY_DELAY}s...")
                    time.sleep(TOTP_RETRY_DELAY)

        raise AuthenticationError(
            f"TOTP verification failed after {TOTP_MAX_RETRIES} attempts: {last_error}"
        )

    def _verify_pin(self, request_key: str) -> str:
        """Verify PIN and get access token."""
        url = f"{self.BASE_URL}/verify_pin"
        data = {
            "request_key": request_key,
            "identity_type": "pin",
            "identifier": self._credentials["pin"],
        }

        response = self._api_request("POST", url, data)

        if "data" not in response or "access_token" not in response["data"]:
            raise AuthenticationError(f"Invalid PIN response: {response}")

        return response["data"]["access_token"]

    def _get_auth_code(self, access_token: str) -> str:
        """Get authorization code."""
        url = f"{self.BASE_URL_2}/token"
        data = {
            "fyers_id": self._credentials["fyers_id"],
            "app_id": self._credentials["app_id"],
            "redirect_uri": self._credentials["redirect_url"],
            "appType": self._credentials["app_type"],
            "response_type": "code",
            "create_cookie": True,
        }
        headers = {"Authorization": f"Bearer {access_token}"}

        response = self._api_request("POST", url, data, headers)

        redirect_url = response.get("Url")
        if not redirect_url:
            raise AuthenticationError(f"No URL in response: {response}")

        # Extract auth code from URL
        parsed = parse.urlparse(redirect_url)
        params = parse.parse_qs(parsed.query)
        auth_code = params.get("auth_code", [None])[0]

        if not auth_code:
            raise AuthenticationError(f"No auth_code in URL: {redirect_url}")

        return auth_code

    def _get_access_token(self, auth_code: str) -> str:
        """Get final access token."""
        url = f"{self.BASE_URL_2}/validate-authcode"
        data = {
            "grant_type": "authorization_code",
            "appIdHash": self._credentials["app_id_hash"],
            "code": auth_code,
        }

        response = self._api_request("POST", url, data)

        access_token = response.get("access_token")
        if not access_token:
            raise AuthenticationError(f"No access_token in response: {response}")

        return access_token

    # =========================================================================
    # HTTP REQUEST HELPER
    # =========================================================================

    def _api_request(
        self,
        method: str,
        url: str,
        data: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Make API request with retry logic.

        Args:
            method: HTTP method (GET/POST)
            url: Request URL
            data: Request payload
            headers: Optional headers

        Returns:
            JSON response as dictionary

        Raises:
            AuthenticationError: If all retries fail
        """
        last_error = None

        for attempt in range(AUTH_MAX_RETRIES):
            try:
                if method.upper() == "POST":
                    response = requests.post(
                        url, json=data, headers=headers, timeout=API_TIMEOUT
                    )
                else:
                    response = requests.get(
                        url, params=data, headers=headers, timeout=API_TIMEOUT
                    )

                # Handle rate limiting
                if response.status_code == 429:
                    wait_time = 2 ** (attempt + 1)
                    logging.warning("Rate limited. Waiting %d seconds.", wait_time)
                    time.sleep(wait_time)
                    continue

                response.raise_for_status()
                return response.json()

            except requests.exceptions.RequestException as exc:
                last_error = exc
                logging.warning("Request failed (attempt %d): %s", attempt + 1, exc)

                if attempt < AUTH_MAX_RETRIES - 1:
                    wait_time = 2**attempt
                    time.sleep(wait_time)

        raise AuthenticationError(
            f"API request failed after {AUTH_MAX_RETRIES} attempts: {last_error}"
        )


# =============================================================================
# CONVENIENCE FUNCTION
# =============================================================================

_auth_instance: Optional[FyersAuth] = None


def get_fyers_client() -> Optional[fyersModel.FyersModel]:
    """
    Get authenticated Fyers client (singleton pattern).

    Returns:
        FyersModel instance or None
    """
    global _auth_instance

    if _auth_instance is None:
        _auth_instance = FyersAuth()

    return _auth_instance.get_client()


def get_auth_instance() -> FyersAuth:
    """
    Get FyersAuth singleton instance.

    Returns:
        FyersAuth instance
    """
    global _auth_instance

    if _auth_instance is None:
        _auth_instance = FyersAuth()

    return _auth_instance


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    "FyersAuth",
    "AuthenticationError",
    "get_fyers_client",
    "get_auth_instance",
]

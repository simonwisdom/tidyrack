"""Sign in with OpenRouter, for the public copy.

OpenRouter's OAuth PKCE flow: we send the person to openrouter.ai/auth with a
challenge (the hash of a secret only this browser's cookie holds); they approve
Tidyrack there and come back with a one-time code; we trade the code and the
secret for their own API key and their OpenRouter user id. Their model calls then
run on that key, so they pay for their own photos.

Docs: https://openrouter.ai/docs/guides/overview/auth/oauth
"""

import base64
import hashlib
import secrets
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

AUTH_URL = "https://openrouter.ai/auth"
EXCHANGE_URL = "https://openrouter.ai/api/v1/auth/keys"
KEY_LABEL = "Tidyrack"


class SignInError(Exception):
    """Signing in didn't work. The message is one of the codes static/app.js turns
    into words (SIGN_IN_ERRORS), so a link can't make the page say anything else."""


def new_verifier() -> str:
    return secrets.token_urlsafe(48)


def challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def sign_in_url(callback_url: str, verifier: str, state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "callback_url": callback_url,
        "code_challenge": challenge(verifier),
        "code_challenge_method": "S256",
        "key_label": KEY_LABEL,
        "state": state,
    })


def key_hash(key: str) -> str:
    """How OpenRouter names a key in its links: https://openrouter.ai/keys/<hash>."""
    return hashlib.sha256(key.encode()).hexdigest()


def exchange(code: str, verifier: str) -> tuple[str, str]:
    """Trade the one-time code for (API key, OpenRouter user id)."""
    response = httpx.post(EXCHANGE_URL, json={
        "code": code, "code_verifier": verifier, "code_challenge_method": "S256",
    }, timeout=30)
    if response.status_code != 200:
        raise SignInError("refused")
    data = response.json()
    if not data.get("key") or not data.get("user_id"):
        # Each sign-in makes a new key, so without the user id we couldn't find their items next time.
        raise SignInError("no_user")
    return data["key"], data["user_id"]


class Keys:
    """Encrypts OpenRouter keys before they're stored, with TIDYRACK_SECRET.
    Make one with: uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    """

    def __init__(self, secret: str | None):
        if not secret:
            raise RuntimeError("The public copy needs TIDYRACK_SECRET to encrypt people's OpenRouter keys")
        self._fernet = Fernet(secret)

    def lock(self, key: str) -> str:
        return self._fernet.encrypt(key.encode()).decode()

    def unlock(self, locked: str) -> str | None:
        """The key, or None if it can't be read (say, the secret was changed)."""
        try:
            return self._fernet.decrypt(locked.encode()).decode()
        except InvalidToken:
            return None

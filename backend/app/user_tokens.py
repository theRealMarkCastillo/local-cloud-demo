"""The signed-in user's own Keycloak tokens. They live only in the server-side session, never in the browser."""
from collections.abc import Awaitable, Callable

REFRESH_BEFORE_SECONDS = 30


class SignInRequired(Exception):
    """No usable tokens are left for this session; the user has to sign in again."""


def save(session: dict, token: dict, now: float) -> None:
    session["tokens"] = {"access": token["access_token"], "refresh": token.get("refresh_token", ""),
                         "expires_at": now + float(token.get("expires_in") or 0)}


async def access_token(session: dict, refresh: Callable[[str], Awaitable[dict]], now: float) -> str:
    tokens = session.get("tokens")
    if not tokens:
        raise SignInRequired()
    if tokens["expires_at"] - now > REFRESH_BEFORE_SECONDS:
        return tokens["access"]
    try:
        if not tokens["refresh"]:
            raise ValueError("no refresh token")
        save(session, await refresh(tokens["refresh"]), now)
    except Exception:
        # Expired or revoked at the identity provider: drop the tokens instead of retrying on every request.
        session.pop("tokens", None)
        raise SignInRequired() from None
    return session["tokens"]["access"]

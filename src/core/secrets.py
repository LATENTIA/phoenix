"""
Token storage abstraction.

Phoenix needs to remember each account's IBKR Flex token. Where that token
lives depends on how the app is deployed:

  - **plaintext mode** (default, for local dev): the token is stored as a
    plain string in `data.db` under `accounts.flex_token`. Convenient,
    zero infra. Anyone with read access to the .db file gets the token.

  - **AWS Secrets Manager mode**: only an opaque reference (an
    `aws-sm://<secret-name>` URI) is stored in `data.db`. The real token
    lives in AWS Secrets Manager and is fetched on demand via boto3.
    Compromised DB or DB backup does not leak the token.

The mode is selected by the `PHOENIX_SECRETS_BACKEND` env var:

  PHOENIX_SECRETS_BACKEND=plaintext   (default)
  PHOENIX_SECRETS_BACKEND=aws

Other env vars (only read when backend=aws):
  AWS_REGION               (default eu-west-1)
  PHOENIX_SECRETS_PREFIX   (default phoenix/, becomes the secret-name prefix)

The DB layer doesn't know which backend is active. It just stores whatever
`store_token()` returns and hands it back to `resolve_token()` later. New
backends (HashiCorp Vault, GCP Secret Manager, age-encrypted file, ...) can
be added by extending the two `_<backend>_*` helpers.
"""

import logging
import os

log = logging.getLogger("ibkr.secrets")


SECRET_PREFIX = "aws-sm://"
_BACKEND = os.environ.get("PHOENIX_SECRETS_BACKEND", "plaintext").lower()


def _aws_region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "eu-west-1"


def _aws_prefix() -> str:
    return os.environ.get("PHOENIX_SECRETS_PREFIX", "phoenix/")


def _aws_client():
    """Lazily import boto3 so the dep stays optional when not in AWS mode."""
    import boto3  # noqa: WPS433 (intentional local import)
    return boto3.client("secretsmanager", region_name=_aws_region())


# ---------- Public API ----------

def store_token(account_name: str, token: str | None) -> str | None:
    """Persist `token` and return the value the DB layer should save.

    Plaintext: returns `token` unchanged.
    AWS:       writes to Secrets Manager and returns an `aws-sm://...`
               reference. `None`/empty input is always a passthrough.
    """
    if not token:
        return token
    if _BACKEND != "aws":
        return token

    secret_name = f"{_aws_prefix()}{account_name}/flex_token"
    client = _aws_client()
    try:
        client.create_secret(Name=secret_name, SecretString=token)
        log.info(f"secrets: created AWS SM secret {secret_name}")
    except client.exceptions.ResourceExistsException:
        client.update_secret(SecretId=secret_name, SecretString=token)
        log.info(f"secrets: updated existing AWS SM secret {secret_name}")
    return f"{SECRET_PREFIX}{secret_name}"


def resolve_token(stored: str | None) -> str | None:
    """Read what's in the DB and return the actual token string for use.

    - `None` / empty: returns None.
    - Plain string (no `aws-sm://` prefix): returns it unchanged.
    - `aws-sm://<name>` reference: fetches from AWS Secrets Manager.
      Raises on lookup failure so the caller can fail fast (better than
      silently calling IBKR with `None`).
    """
    if not stored:
        return None
    if not stored.startswith(SECRET_PREFIX):
        return stored

    secret_name = stored[len(SECRET_PREFIX):]
    log.info(f"secrets: fetching AWS SM secret {secret_name}")
    client = _aws_client()
    resp = client.get_secret_value(SecretId=secret_name)
    val = resp.get("SecretString")
    if not val:
        raise RuntimeError(f"AWS SM secret {secret_name} has no SecretString")
    return val


def delete_token(stored: str | None) -> None:
    """Best-effort cleanup when an account is removed. AWS-mode only;
    plaintext mode has nothing to clean up (the DB row goes away with the
    account)."""
    if not stored or not stored.startswith(SECRET_PREFIX):
        return
    secret_name = stored[len(SECRET_PREFIX):]
    try:
        _aws_client().delete_secret(
            SecretId=secret_name, ForceDeleteWithoutRecovery=True,
        )
        log.info(f"secrets: deleted AWS SM secret {secret_name}")
    except Exception as e:
        # Not fatal. The secret can be hand-removed from AWS later.
        log.warning(f"secrets: failed to delete {secret_name}: {e}")

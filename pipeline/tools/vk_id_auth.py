#!/usr/bin/env python3
"""VK ID user token for the community wall: get it, refresh it, see what it can do.

VK closed the legacy Implicit Flow on 2024-06-25 and the Kate Mobile app_id we
rode on until 2026-09-08. The only way to a USER token now is an application in
the VK ID cabinet (id.vk.ru, needs a confirmed business profile) with OAuth 2.1
+ PKCE; the `wall` and `photos` rights are granted per application by
devsupport@corp.vk.com. Tokens live an hour and roll over through a refresh
token, so the pipeline will keep a token store rather than a static env value.

Subcommands (state lives in --store, default ~/.posinus-vk-id.json, mode 0600):

  url       --client-id ID [--scope "wall photos groups"] [--redirect URL]
            prints the authorize URL for the group admin to open; remembers
            state + PKCE verifier
  exchange  "<redirect URL with code, device_id and state>"
            trades the code for tokens (VK_ID_SERVICE_TOKEN from the env is
            sent when set — confidential applications require it)
  refresh   rolls the tokens over
  probe     calls a few methods with the access token; posts nothing

Stdlib only, like the rest of the pipeline.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AUTHORIZE = "https://id.vk.ru/authorize"
TOKEN = "https://id.vk.ru/oauth2/auth"
API = "https://api.vk.ru/method/"
DEFAULT_REDIRECT = "https://wildcar.org/auth/vk-id/"
DEFAULT_SCOPE = "wall photos groups"
DEFAULT_GROUP = "233237778"


def load(store: Path) -> dict:
    return json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}


def save(store: Path, data: dict) -> None:
    store.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    store.chmod(0o600)


def post_form(url: str, fields: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode("utf-8"),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except ValueError:
            return {"error": f"http {exc.code}", "error_description": body[:300]}


def shape(token: str) -> str:
    return f"{token[:6]}… ({len(token)} chars)"


def cmd_url(args: argparse.Namespace, store: Path) -> int:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    data = load(store)
    data.update(client_id=args.client_id, redirect_uri=args.redirect, scope=args.scope,
                state=state, code_verifier=verifier)
    save(store, data)
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": args.client_id, "redirect_uri": args.redirect,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        "scope": args.scope,
    })
    print(f"{AUTHORIZE}?{query}")
    print(f"\nOpen it as the group admin, allow, then run:\n  {sys.argv[0]} exchange '<the URL you land on>'",
          file=sys.stderr)
    return 0


def cmd_exchange(args: argparse.Namespace, store: Path) -> int:
    data = load(store)
    if not data.get("code_verifier"):
        print("no pending authorization: run `url` first", file=sys.stderr)
        return 2
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(args.redirect_url).query)
    code, device_id, state = (params.get(k, [""])[0] for k in ("code", "device_id", "state"))
    if not code or not device_id:
        print("the URL carries no code/device_id", file=sys.stderr)
        return 2
    if state != data["state"]:
        print("state mismatch: this redirect does not belong to the pending authorization", file=sys.stderr)
        return 2
    fields = {
        "grant_type": "authorization_code", "code_verifier": data["code_verifier"],
        "redirect_uri": data["redirect_uri"], "code": code, "client_id": data["client_id"],
        "device_id": device_id, "state": state,
    }
    if os.environ.get("VK_ID_SERVICE_TOKEN"):
        fields["service_token"] = os.environ["VK_ID_SERVICE_TOKEN"]
    return _store_tokens(store, data, post_form(TOKEN, fields), device_id)


def cmd_refresh(args: argparse.Namespace, store: Path) -> int:
    data = load(store)
    if not data.get("refresh_token"):
        print("no refresh token in the store: run `url` and `exchange` first", file=sys.stderr)
        return 2
    state = secrets.token_urlsafe(32)
    fields = {
        "grant_type": "refresh_token", "refresh_token": data["refresh_token"],
        "client_id": data["client_id"], "device_id": data["device_id"], "state": state,
    }
    if os.environ.get("VK_ID_SERVICE_TOKEN"):
        fields["service_token"] = os.environ["VK_ID_SERVICE_TOKEN"]
    reply = post_form(TOKEN, fields)
    if reply.get("state") not in (None, state):
        print("state mismatch in the refresh reply", file=sys.stderr)
        return 1
    return _store_tokens(store, data, reply, data["device_id"])


def _store_tokens(store: Path, data: dict, reply: dict, device_id: str) -> int:
    if "access_token" not in reply:
        print("VK ID answered:", json.dumps(reply, ensure_ascii=False)[:600], file=sys.stderr)
        return 1
    data.update(access_token=reply["access_token"], refresh_token=reply.get("refresh_token", data.get("refresh_token")),
                device_id=device_id, user_id=reply.get("user_id"), token_scope=reply.get("scope"),
                expires_at=int(time.time()) + int(reply.get("expires_in") or 0))
    data.pop("code_verifier", None)
    save(store, data)
    print(f"access token {shape(data['access_token'])}, expires in {reply.get('expires_in')} s, "
          f"scope {reply.get('scope')!r}, user {reply.get('user_id')}; refresh token {shape(data['refresh_token'] or '')}")
    print(f"stored in {store}")
    return 0


def cmd_probe(args: argparse.Namespace, store: Path) -> int:
    data = load(store)
    token = data.get("access_token")
    if not token:
        print("no access token in the store", file=sys.stderr)
        return 2
    left = data.get("expires_at", 0) - int(time.time())
    print(f"access token {shape(token)}, {left} s left")

    def call(method: str, **params: object) -> None:
        params.update(access_token=token, v="5.199")
        reply = post_form(API + method, params)
        if "error" in reply:
            err = reply["error"]
            print(f"  {method:<30} -> ERROR {err.get('error_code')} {err.get('error_msg')}")
        else:
            print(f"  {method:<30} -> ok {json.dumps(reply['response'], ensure_ascii=False)[:120]}")

    call("users.get")
    call("account.getAppPermissions")
    call("groups.getById", group_id=args.group_id)
    call("photos.getWallUploadServer", group_id=args.group_id)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--store", default=str(Path.home() / ".posinus-vk-id.json"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("url"); p.add_argument("--client-id", required=True)
    p.add_argument("--scope", default=DEFAULT_SCOPE); p.add_argument("--redirect", default=DEFAULT_REDIRECT)
    p = sub.add_parser("exchange"); p.add_argument("redirect_url")
    sub.add_parser("refresh")
    p = sub.add_parser("probe"); p.add_argument("--group-id", default=DEFAULT_GROUP)
    args = parser.parse_args(argv)
    store = Path(args.store).expanduser()
    return {"url": cmd_url, "exchange": cmd_exchange, "refresh": cmd_refresh, "probe": cmd_probe}[args.cmd](args, store)


if __name__ == "__main__":
    sys.exit(main())

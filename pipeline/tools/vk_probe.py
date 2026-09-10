#!/usr/bin/env python3
"""Probe what a VK access token can still do for the community wall.

Written on 2026-09-10, when VK cut third-party app quotas and the Kate Mobile
user token the publisher relied on started answering «9 Flood control» to every
method. The probe answers, without touching the live pipeline config:

  1. does the token answer API calls at all (groups.getById);
  2. what rights it carries (groups.getTokenPermissions, community keys only);
  3. can it upload a wall photo (photos.getWallUploadServer) — user keys only,
     a community key gets error 27;
  4. can it post on the community wall (wall.post) — tested with a POSTPONED
     post carrying a link attachment, so nothing reaches subscribers; the post
     is deleted right after (wall.delete).

The token is read from VK_PROBE_TOKEN or asked for interactively (never from
the command line, never printed). Group id from VK_GROUP_ID (default: the
posinus community). Stdlib only, like the rest of the pipeline.

    VK_GROUP_ID=233237778 python3 pipeline/tools/vk_probe.py
"""
from __future__ import annotations

import getpass
import json
import os
import sys
import time
import urllib.parse
import urllib.request

API = "https://api.vk.ru/method/"
VERSION = os.environ.get("VK_API_VERSION", "5.199")
LINK = os.environ.get("VK_PROBE_LINK", "https://wildcar.ru/")


def call(token: str, method: str, **params: object) -> tuple[dict | list | None, str]:
    """Return (response, error_text). error_text is '' on success."""
    query = {k: v for k, v in params.items() if v is not None}
    query["access_token"] = token
    query["v"] = VERSION
    req = urllib.request.Request(
        API + method,
        data=urllib.parse.urlencode(query).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # network / HTTP layer
        return None, f"transport: {exc}"
    if "error" in payload:
        err = payload["error"]
        return None, f"{err.get('error_code')} {err.get('error_msg')}"
    return payload.get("response"), ""


def main() -> int:
    group_id = os.environ.get("VK_GROUP_ID", "233237778")
    token = os.environ.get("VK_PROBE_TOKEN") or getpass.getpass("VK access token (not echoed): ")
    token = token.strip()
    if not token:
        print("no token given", file=sys.stderr)
        return 2
    kind = "user key (vk1.)" if token.startswith("vk1.") else "VK ID token (vk2.a.)" if token.startswith("vk2.a.") else "community key or unknown"
    print(f"token shape: {kind}, {len(token)} chars; group {group_id}; api v{VERSION}")

    verdict: dict[str, str] = {}

    resp, err = call(token, "groups.getById", group_id=group_id, fields="members_count")
    if err:
        print(f"[1] groups.getById      -> ERROR {err}")
        verdict["api"] = "no"
        if err.startswith("9 "):
            print("    every method answers Flood control: the token's app is cut off, nothing below will work")
            return 1
    else:
        groups = resp.get("groups", resp) if isinstance(resp, dict) else resp
        g = groups[0] if isinstance(groups, list) and groups else {}
        print(f"[1] groups.getById      -> ok: {g.get('name')!r}, members {g.get('members_count')}")
        verdict["api"] = "yes"

    resp, err = call(token, "groups.getTokenPermissions")
    if err:
        print(f"[2] getTokenPermissions -> {err} (normal for a user key)")
    else:
        names = ",".join(p.get("name", "?") for p in resp.get("permissions", []))
        print(f"[2] getTokenPermissions -> mask {resp.get('mask')}: {names}")

    resp, err = call(token, "photos.getWallUploadServer", group_id=group_id)
    if err:
        print(f"[3] photos.getWallUploadServer -> ERROR {err}")
        verdict["photo_upload"] = "no"
    else:
        print("[3] photos.getWallUploadServer -> ok (photo posts possible)")
        verdict["photo_upload"] = "yes"

    publish_date = int(time.time()) + 2 * 24 * 3600
    resp, err = call(
        token, "wall.post",
        owner_id=f"-{group_id}", from_group=1, publish_date=publish_date,
        message="Проверка ключа доступа. Отложенная запись, удаляется скриптом.",
        attachments=LINK,
    )
    if err:
        print(f"[4] wall.post (postponed, link) -> ERROR {err}")
        verdict["wall_post"] = "no"
    else:
        post_id = resp.get("post_id") if isinstance(resp, dict) else resp
        print(f"[4] wall.post (postponed, link) -> ok, postponed post_id {post_id}")
        verdict["wall_post"] = "yes"
        _, derr = call(token, "wall.delete", owner_id=f"-{group_id}", post_id=post_id)
        if derr:
            print(f"    wall.delete -> ERROR {derr}; remove the postponed post by hand: "
                  f"https://vk.ru/wall-{group_id}?section=postponed")
        else:
            print("    wall.delete -> ok, probe post removed")

    print("verdict:", json.dumps(verdict, ensure_ascii=False))
    if verdict.get("wall_post") == "yes" and verdict.get("photo_upload") == "yes":
        print("=> full mode: photo upload + wall.post, the publisher works as is")
    elif verdict.get("wall_post") == "yes":
        print("=> link-card mode only: wall.post with the article URL attached, no photo upload")
    else:
        print("=> this token cannot post on the community wall")
    return 0


if __name__ == "__main__":
    sys.exit(main())

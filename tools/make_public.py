"""Make ONLY `public/*` in the bucket anonymously readable. Run once.

    python tools/make_public.py            # show what it would change
    python tools/make_public.py --yes      # actually change it

Why this is a separate tool and not something the pipeline does
---------------------------------------------------------------
Because it changes the security posture of somebody's AWS account, and that
must never be a side effect of running a campaign. A pipeline that quietly
relaxes Block Public Access the first time it needs a link is a pipeline you
cannot let near a client's account. So: an explicit command, a dry run by
default, and a printed diff of exactly what changes.

What it does
------------
1. Block Public Access, set to a MIXED state rather than simply "off":

       BlockPublicAcls       true   (unchanged)  -- no object may be made public by ACL
       IgnorePublicAcls      true   (unchanged)  -- and any existing public ACL is ignored
       BlockPublicPolicy     false  (CHANGED)    -- a policy may grant public read
       RestrictPublicBuckets false  (CHANGED)    -- and that policy is honoured

   Only the two policy flags move. The ACL flags stay on, so the ONLY route
   to public access is the single policy below -- there is no second mechanism
   by which a stray upload can expose itself.

2. A bucket policy granting exactly one action on exactly one prefix:

       s3:GetObject   on   arn:aws:s3:::<bucket>/public/*

   Not `s3:*`. Not `/*`. Not ListBucket -- which is the important omission:
   without it the bucket cannot be enumerated, so an object is reachable only
   by knowing its full key, and every key contains a 190-bit random token.

To undo it
----------
    python tools/make_public.py --revoke --yes

which deletes the policy and puts all four Block Public Access flags back on.
Note that this closes the door for FUTURE readers; it does not un-share a link
someone has already saved and fetched.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import hmac
import json
import os
import sys
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from pipeline.env import load_dotenv
from pipeline.storage.base import PUBLIC_ROOT
from pipeline.storage.s3 import (ALGORITHM, S3Storage, _sha256,
                                 canonical_request, signing_key)


def bucket_request(s: S3Storage, method: str, subresource: str,
                   body: bytes = b"", content_type: str = ""):
    """A signed request against a bucket SUBRESOURCE (`?policy`, `?publicAccessBlock`).

    The storage adapter only ever signs object paths with an empty query
    string, and these calls are the opposite shape: path `/`, and the
    subresource carried in the query. The signature covers the canonical query
    string, so `?policy` has to be signed as `policy=` -- an empty value, not
    a bare key. Getting that wrong is a 403 that reads exactly like bad
    credentials.

    Kept in the tool rather than added to the adapter: this is administration,
    and the runtime path has no business being able to rewrite bucket policy.
    """
    host = (f"{s.bucket}.s3.amazonaws.com" if s.region == "us-east-1"
            else f"{s.bucket}.s3.{s.region}.amazonaws.com")
    now = _dt.datetime.now(_dt.timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    payload_hash = _sha256(body)

    headers = {"host": host, "x-amz-content-sha256": payload_hash,
               "x-amz-date": amzdate}
    if content_type:
        headers["content-type"] = content_type
    if body:
        # PutPublicAccessBlock REQUIRES Content-MD5. It is one of the few
        # remaining S3 calls that does, and the error when it is missing does
        # not mention MD5 at all.
        import base64
        headers["content-md5"] = base64.b64encode(hashlib.md5(body).digest()).decode()

    query = f"{quote(subresource, safe='')}="
    creq, signed = canonical_request(method, "/", query, headers, payload_hash)
    scope = f"{datestamp}/{s.region}/s3/aws4_request"
    to_sign = "\n".join([ALGORITHM, amzdate, scope, _sha256(creq.encode("utf-8"))])
    sig = hmac.new(signing_key(s.secret, datestamp, s.region, "s3"),
                   to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["Authorization"] = (f"{ALGORITHM} Credential={s.access}/{scope}, "
                                f"SignedHeaders={signed}, Signature={sig}")
    return requests.request(method, f"https://{host}/?{query}",
                            headers=headers, data=body, timeout=60)


BPA_OPEN = """<?xml version="1.0" encoding="UTF-8"?>
<PublicAccessBlockConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <BlockPublicAcls>true</BlockPublicAcls>
  <IgnorePublicAcls>true</IgnorePublicAcls>
  <BlockPublicPolicy>false</BlockPublicPolicy>
  <RestrictPublicBuckets>false</RestrictPublicBuckets>
</PublicAccessBlockConfiguration>"""

BPA_CLOSED = """<?xml version="1.0" encoding="UTF-8"?>
<PublicAccessBlockConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <BlockPublicAcls>true</BlockPublicAcls>
  <IgnorePublicAcls>true</IgnorePublicAcls>
  <BlockPublicPolicy>true</BlockPublicPolicy>
  <RestrictPublicBuckets>true</RestrictPublicBuckets>
</PublicAccessBlockConfiguration>"""


def policy_for(bucket: str, prefix: str) -> str:
    return json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "PublicReadForSharedRunsOnly",
            "Effect": "Allow",
            "Principal": "*",
            "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{bucket}/{prefix}/*",
        }],
    }, indent=2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true",
                    help="actually apply the change (without this it is a dry run)")
    ap.add_argument("--revoke", action="store_true",
                    help="remove the policy and re-block public access")
    args = ap.parse_args()

    load_dotenv()
    s = S3Storage()

    print(f"bucket : {s.bucket}")
    print(f"region : {s.region}")
    print(f"prefix : {PUBLIC_ROOT}/  (nothing outside this becomes readable)")
    print()

    r = bucket_request(s, "GET", "publicAccessBlock")
    print(f"current Block Public Access : {r.status_code}")
    if r.status_code == 200:
        for flag in ("BlockPublicAcls", "IgnorePublicAcls",
                     "BlockPublicPolicy", "RestrictPublicBuckets"):
            on = f"<{flag}>true</{flag}>" in r.text
            print(f"    {flag:22} {'true' if on else 'false'}")
    r = bucket_request(s, "GET", "policy")
    print(f"current bucket policy       : "
          f"{'none' if r.status_code == 404 else r.status_code}")
    print()

    if args.revoke:
        print("WOULD REVOKE: delete the bucket policy, re-block all public access.")
        if not args.yes:
            print("\nDry run. Re-run with --yes to apply.")
            return 0
        d = bucket_request(s, "DELETE", "policy")
        print(f"  DELETE ?policy            -> {d.status_code}")
        b = bucket_request(s, "PUT", "publicAccessBlock",
                           BPA_CLOSED.encode(), "application/xml")
        print(f"  PUT    ?publicAccessBlock -> {b.status_code} "
              f"{(b.text or '')[:200]}")
        print("\nRevoked. Links already handed out stop working for anyone who has "
              "not already fetched the object.")
        return 0 if d.status_code in (200, 204) and b.status_code == 200 else 1

    policy = policy_for(s.bucket, PUBLIC_ROOT)
    print("WOULD APPLY")
    print("  Block Public Access: BlockPublicPolicy true -> false")
    print("                       RestrictPublicBuckets true -> false")
    print("                       (ACL flags stay ON)")
    print("  Bucket policy:")
    for line in policy.splitlines():
        print("    " + line)
    print()
    if not args.yes:
        print("Dry run. Nothing was changed. Re-run with --yes to apply.")
        return 0

    # Order matters: the policy PUT is REFUSED while BlockPublicPolicy is on,
    # with an AccessDenied that says nothing about which of the two settings
    # is at fault. Relax the block first.
    b = bucket_request(s, "PUT", "publicAccessBlock", BPA_OPEN.encode(), "application/xml")
    print(f"PUT ?publicAccessBlock -> {b.status_code} {(b.text or '')[:200]}")
    if b.status_code != 200:
        print("stopped: public access block was not updated, so the policy would fail too")
        return 1

    p = bucket_request(s, "PUT", "policy", policy.encode(), "application/json")
    print(f"PUT ?policy            -> {p.status_code} {(p.text or '')[:300]}")
    if p.status_code not in (200, 204):
        return 1

    print()
    print(f"Done. Objects under {PUBLIC_ROOT}/ are now readable by anyone holding")
    print("the full URL. The bucket still cannot be listed, so a URL cannot be")
    print("guessed. Undo with:  python tools/make_public.py --revoke --yes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

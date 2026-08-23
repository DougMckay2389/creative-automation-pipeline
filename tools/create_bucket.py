"""Create the S3 bucket this pipeline mirrors into.

Separate from the pipeline on purpose: creating infrastructure is not
something a render should ever do as a side effect. Run once, by hand.

    python tools/create_bucket.py creative-automation-doug [region]

Block Public Access is left ON (the account default). Nothing here needs to be
world-readable -- the creatives are read back through the same signed API that
wrote them.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.env import load_dotenv  # noqa: E402

load_dotenv()
import requests  # noqa: E402

from pipeline.storage.s3 import (ALGORITHM, _sha256, canonical_request,  # noqa: E402
                                 signing_key)

ACCESS = os.environ.get("AWS_ACCESS_KEY_ID", "")
SECRET = os.environ.get("AWS_SECRET_ACCESS_KEY", "")


def signed(method: str, host: str, uri: str, region: str, body: bytes = b""):
    now = dt.datetime.now(dt.timezone.utc)
    amz, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    ph = _sha256(body)
    h = {"host": host, "x-amz-content-sha256": ph, "x-amz-date": amz}
    creq, sh = canonical_request(method, uri, "", h, ph)
    scope = f"{day}/{region}/s3/aws4_request"
    ts = "\n".join([ALGORITHM, amz, scope, _sha256(creq.encode())])
    sig = hmac.new(signing_key(SECRET, day, region, "s3"), ts.encode(),
                   hashlib.sha256).hexdigest()
    h["Authorization"] = (f"{ALGORITHM} Credential={ACCESS}/{scope}, "
                          f"SignedHeaders={sh}, Signature={sig}")
    return requests.request(method, f"https://{host}{uri}", headers=h, data=body,
                            timeout=30)


def main() -> None:
    if not (ACCESS and SECRET):
        raise SystemExit("AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY not set")
    bucket = sys.argv[1] if len(sys.argv) > 1 else "creative-automation-doug"
    region = sys.argv[2] if len(sys.argv) > 2 else "us-east-1"
    host = f"{bucket}.s3.amazonaws.com"

    r = signed("HEAD", host, "/", region)
    if r.status_code == 200:
        print(f"  {bucket} already exists and is reachable.")
        return

    # us-east-1 is the one region that must NOT be named in the body. Sending
    # a LocationConstraint of us-east-1 is an InvalidLocationConstraint error.
    body = b"" if region == "us-east-1" else (
        '<CreateBucketConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"<LocationConstraint>{region}</LocationConstraint></CreateBucketConfiguration>"
    ).encode()

    r = signed("PUT", host, "/", region, body)
    if r.status_code in (200, 409):
        print(f"  created s3://{bucket}  ({region})"
              if r.status_code == 200 else f"  {bucket} already owned by you.")
    else:
        print(f"  PUT -> {r.status_code}\n{(r.text or '')[:500]}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

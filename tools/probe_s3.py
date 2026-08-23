"""Ask S3 which buckets these credentials can see, and where they live.

Uses the pipeline's own signing code against the real service -- if this
returns a bucket list, SigV4 is right in a way no unit test can prove.
"""
import os, re, sys, datetime as dt, hashlib, hmac
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.env import load_dotenv; load_dotenv()
import requests
from pipeline.storage.s3 import canonical_request, signing_key, _sha256, ALGORITHM

ACCESS = os.environ["AWS_ACCESS_KEY_ID"]; SECRET = os.environ["AWS_SECRET_ACCESS_KEY"]

def call(host, uri, region, query=""):
    now = dt.datetime.now(dt.timezone.utc)
    amz = now.strftime("%Y%m%dT%H%M%SZ"); day = now.strftime("%Y%m%d")
    ph = _sha256(b"")
    h = {"host": host, "x-amz-content-sha256": ph, "x-amz-date": amz}
    creq, signed = canonical_request("GET", uri, query, h, ph)
    scope = f"{day}/{region}/s3/aws4_request"
    ts = "\n".join([ALGORITHM, amz, scope, _sha256(creq.encode())])
    sig = hmac.new(signing_key(SECRET, day, region, "s3"), ts.encode(), hashlib.sha256).hexdigest()
    h["Authorization"] = (f"{ALGORITHM} Credential={ACCESS}/{scope}, "
                          f"SignedHeaders={signed}, Signature={sig}")
    url = f"https://{host}{uri}" + (f"?{query}" if query else "")
    return requests.get(url, headers=h, timeout=30)

r = call("s3.amazonaws.com", "/", "us-east-1")
print("ListBuckets ->", r.status_code)
if r.status_code != 200:
    print((r.text or "")[:400]); raise SystemExit(1)
buckets = re.findall(r"<Name>([^<]+)</Name>", r.text)
print(f"{len(buckets)} bucket(s):")
for b in buckets:
    loc = call(f"{b}.s3.amazonaws.com", "/", "us-east-1", "location=")
    m = re.search(r"<LocationConstraint[^>]*>([^<]*)</LocationConstraint>", loc.text or "")
    region = (m.group(1) if m and m.group(1) else "us-east-1")
    print(f"  {b:40s} region={region or 'us-east-1'}")

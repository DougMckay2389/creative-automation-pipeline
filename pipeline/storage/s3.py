"""S3, signed by hand.

No boto3. The pipeline ships with three dependencies and the point of that is
that you can read all of it -- pulling in an SDK the size of the rest of the
repo to make four HTTP calls is the wrong trade. Signature Version 4 is about
seventy lines of hashing, and writing it out shows the protocol instead of
hiding it behind `client.put_object`.

**It is not only AWS.** SigV4 against a path-style endpoint is also how you
talk to Cloudflare R2, MinIO, Backblaze B2 and DigitalOcean Spaces, so setting
`S3_ENDPOINT` points the same class at any of them. That matters more than it
looks: "which object store" is a client procurement decision, and this is the
adapter that makes it not a code decision.

Environment:
    AWS_ACCESS_KEY_ID       AWS_SECRET_ACCESS_KEY
    S3_BUCKET               S3_REGION    (default us-east-1)
    S3_ENDPOINT             optional -- R2/MinIO/Spaces
    S3_PREFIX               optional -- a folder inside the bucket
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import os
from urllib.parse import quote

import requests

from .base import PUBLIC_ROOT, Storage, StorageError, StoredObject

ALGORITHM = "AWS4-HMAC-SHA256"
UNSIGNED = "UNSIGNED-PAYLOAD"

# PUBLIC_ROOT is imported, not defined here. What it MEANS, though, is
# entirely about this backend:
#
# This is the hinge of the sharing design, so it is worth being explicit about
# what it does and does not buy.
#
# The bucket keeps Block Public Access ON for ACLs, keeps `s3:ListBucket`
# denied to the public, and carries a policy granting `s3:GetObject` on
# `arn:aws:s3:::<bucket>/public/*` and nothing else. Every run then writes
# under `public/<token>/`, where the token is 32 URL-safe characters of
# `secrets` randomness -- around 190 bits. Nobody can enumerate the bucket, so
# the only way to reach a run is to be given its link.
#
# What that is: a shareable URL that never expires, which is what a submission
# reviewed weeks later needs.
#
# What it is NOT: revocable. Anyone who has ever seen the link keeps access
# until the objects are deleted. That is the trade, it was made deliberately,
# and it is why masters, logs and anything not meant for an outside reader
# stay outside this prefix and are reachable only by a signed link.

# AWS caps a SigV4 presigned URL at seven days when it is signed with
# long-term IAM credentials. Anything larger is rejected at signing time, not
# at use time, so it is clamped here rather than discovered later.
MAX_PRESIGN_SECONDS = 7 * 24 * 3600


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, datestamp: str, region: str, service: str) -> bytes:
    """The four-step derivation. Each step signs the previous step's output.

    Deriving a key per date/region/service is what stops a captured signature
    being replayed against another region or another day.
    """
    k = _sign(("AWS4" + secret).encode("utf-8"), datestamp)
    k = _sign(k, region)
    k = _sign(k, service)
    return _sign(k, "aws4_request")


def canonical_request(method: str, uri: str, query: str,
                      headers: dict[str, str], payload_hash: str) -> tuple[str, str]:
    """Returns (canonical_request, signed_headers).

    Header names lowercased, values stripped, sorted by name -- the signature
    covers this exact serialisation, so a stray space or an unsorted header is
    a 403 that reads like a credentials problem.
    """
    lower = {k.lower().strip(): " ".join(str(v).split()) for k, v in headers.items()}
    signed = ";".join(sorted(lower))
    canon_headers = "".join(f"{k}:{lower[k]}\n" for k in sorted(lower))
    return ("\n".join([method, uri, query, canon_headers, signed, payload_hash]), signed)


class S3Storage(Storage):
    name = "s3"

    def __init__(self, bucket: str | None = None, region: str | None = None,
                 endpoint: str | None = None, prefix: str | None = None,
                 timeout_s: float = 60.0, **_ignored):
        self.access = os.environ.get("AWS_ACCESS_KEY_ID", "")
        self.secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        self.session_token = os.environ.get("AWS_SESSION_TOKEN", "")
        self.bucket = bucket or os.environ.get("S3_BUCKET", "")
        self.region = region or os.environ.get("S3_REGION") or "us-east-1"
        self.prefix = (prefix if prefix is not None
                       else os.environ.get("S3_PREFIX", "")).strip("/")
        self.timeout_s = timeout_s
        endpoint = endpoint or os.environ.get("S3_ENDPOINT", "")
        # Path-style against a custom endpoint, virtual-host style on AWS.
        # R2 and MinIO only reliably speak the former.
        self.endpoint = endpoint.rstrip("/") if endpoint else ""
        if not (self.access and self.secret and self.bucket):
            raise StorageError(
                "s3 storage needs AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and S3_BUCKET")

    # ------------------------------------------------------------------
    def _full_key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def _url_and_path(self, key: str) -> tuple[str, str, str]:
        """(url, canonical_uri, host). Each segment quoted, '/' preserved."""
        full = self._full_key(key)
        path = "/" + quote(full, safe="/")
        if self.endpoint:
            host = self.endpoint.split("://", 1)[-1]
            return f"{self.endpoint}/{self.bucket}{path}", f"/{self.bucket}{path}", host
        host = (f"{self.bucket}.s3.amazonaws.com" if self.region == "us-east-1"
                else f"{self.bucket}.s3.{self.region}.amazonaws.com")
        return f"https://{host}{path}", path, host

    def _request(self, method: str, key: str, data: bytes | None = None,
                 content_type: str | None = None):
        url, canon_uri, host = self._url_and_path(key)
        body = data or b""
        payload_hash = _sha256(body)
        now = _dt.datetime.now(_dt.timezone.utc)
        amzdate = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")

        headers = {"host": host, "x-amz-content-sha256": payload_hash,
                   "x-amz-date": amzdate}
        if self.session_token:
            headers["x-amz-security-token"] = self.session_token
        if content_type:
            headers["content-type"] = content_type

        creq, signed = canonical_request(method, canon_uri, "", headers, payload_hash)
        scope = f"{datestamp}/{self.region}/s3/aws4_request"
        to_sign = "\n".join([ALGORITHM, amzdate, scope, _sha256(creq.encode("utf-8"))])
        sig = hmac.new(signing_key(self.secret, datestamp, self.region, "s3"),
                       to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        headers["Authorization"] = (
            f"{ALGORITHM} Credential={self.access}/{scope}, "
            f"SignedHeaders={signed}, Signature={sig}")
        try:
            return requests.request(method, url, headers=headers, data=body,
                                    timeout=self.timeout_s)
        except requests.RequestException as exc:
            raise StorageError(f"{method} {key}: {exc}") from exc

    # ------------------------------------------------------------------
    def put(self, key, data, content_type="application/octet-stream"):
        r = self._request("PUT", key, data, content_type)
        if r.status_code not in (200, 201):
            raise StorageError(f"PUT {key} -> {r.status_code}: {(r.text or '')[:300]}")
        return StoredObject(key=key, uri=self.uri(key), size=len(data), backend=self.name)

    def get(self, key):
        r = self._request("GET", key)
        if r.status_code != 200:
            raise StorageError(f"GET {key} -> {r.status_code}: {(r.text or '')[:200]}")
        return r.content

    def exists(self, key):
        return self._request("HEAD", key).status_code == 200

    def uri(self, key):
        return f"s3://{self.bucket}/{self._full_key(key)}"

    # ------------------------------------------------------------------
    def public_url(self, key: str) -> str:
        """The plain https URL, with no signature on it.

        Only actually openable for keys under PUBLIC_ROOT, and only once the
        bucket policy from `tools/make_public.py` has been applied. It is
        computed here regardless because the URL is a property of the key, not
        of the policy -- and because a caller that wants to print the link
        before the policy exists should get the real link, not an empty
        string.
        """
        url, _, _ = self._url_and_path(key)
        return url

    def is_public_key(self, key: str) -> bool:
        """Would the bucket policy make this key anonymously readable?

        Tested against the FULL key, prefix included, because S3_PREFIX shifts
        everything: with `S3_PREFIX=archive`, the key `public/x/y.jpg` really
        lives at `archive/public/x/y.jpg`, which the policy does not cover.
        Getting this wrong in the optimistic direction would hand somebody a
        link that 403s, so it is checked where the object actually is.
        """
        return self._full_key(key).startswith(PUBLIC_ROOT + "/")

    def share_url(self, key: str, expires: int = MAX_PRESIGN_SECONDS) -> str:
        """One call that always returns a link somebody can open.

        Public prefix -> a permanent unsigned URL.
        Anywhere else -> a signed URL that expires.

        The point of choosing between them HERE is that no caller has to know
        the sharing rules. The runner asks for a link to an object and gets
        the strongest one that object is entitled to.
        """
        if self.is_public_key(key):
            return self.public_url(key)
        return self.presigned_url(key, expires=expires)

    # ------------------------------------------------------------------
    def presigned_url(self, key: str, expires: int = 3600) -> str:
        """A link that actually opens the object, without making it public.

        `s3://bucket/key` is an identifier, not a URL -- paste it in a browser
        and nothing happens. The obvious fix is to make the bucket public,
        which is how creative assets end up indexed by search engines months
        before a campaign launches.

        The right one is query-string SigV4: the same signature, moved out of
        the Authorization header and into the URL, with an expiry inside the
        signed material so the link stops working on its own. Nothing about
        the bucket changes; Block Public Access stays on.

        Signed with UNSIGNED-PAYLOAD because the recipient is a browser doing
        a GET -- there is no body to hash, and requiring one would mean the
        signer had to know the object's contents to link to it.
        """
        url, canon_uri, host = self._url_and_path(key)
        now = _dt.datetime.now(_dt.timezone.utc)
        amzdate = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        scope = f"{datestamp}/{self.region}/s3/aws4_request"
        # Clamp rather than let AWS reject the signature. Asking for eight
        # days does not produce a link that lasts eight days; it produces a
        # 400 at signing time, and the caller wanted a link.
        expires = max(1, min(int(expires), MAX_PRESIGN_SECONDS))

        params = {
            "X-Amz-Algorithm": ALGORITHM,
            "X-Amz-Credential": f"{self.access}/{scope}",
            "X-Amz-Date": amzdate,
            "X-Amz-Expires": str(int(expires)),
            "X-Amz-SignedHeaders": "host",
        }
        if self.session_token:
            params["X-Amz-Security-Token"] = self.session_token
        # Sorted by encoded name, every value percent-encoded. The signature
        # covers this exact string, so a differently-ordered query is a 403.
        query = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}"
                         for k, v in sorted(params.items()))

        creq, _ = canonical_request("GET", canon_uri, query, {"host": host}, UNSIGNED)
        to_sign = "\n".join([ALGORITHM, amzdate, scope, _sha256(creq.encode("utf-8"))])
        sig = hmac.new(signing_key(self.secret, datestamp, self.region, "s3"),
                       to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{url}?{query}&X-Amz-Signature={sig}"

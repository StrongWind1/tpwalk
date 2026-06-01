"""URL canonicalization and S3 URL conversion utilities.

These three pure functions form the normalization foundation that every other
tpwalk component depends on. URL normalization is the hardest constraint in the
project: 42 phantom duplicates exist in the 1,283-URL seed data that only
collapse under correct percent-encoding normalization.

Design decisions (per D-02 and FOUN-01):
- Canonical form is full HTTPS URL — the s3:// prefix is applied only at the
  final verified.txt write step (to_s5cmd_url).
- Idempotent: safe to normalize an already-normalized URL without changing it.
- Parentheses and other characters legal in S3 key paths are preserved unencoded.
"""

from __future__ import annotations

from urllib.parse import quote, unquote, urlsplit, urlunsplit


def url_normalize(raw: str) -> str:
    """Return the canonical HTTPS form of a TP-Link S3 URL.

    Handles the three encoding chaos classes present in the seed data:
    - Raw spaces (53 URLs): space becomes %20 in canonical form
    - Percent-encoded specials (49 URLs): %28/%29 parens decoded to literal ( )
    - Double-encoded variants: %2520 collapsed to %20

    Also collapses http:// to https:// so scheme variants of the same S3 object
    (common in Wayback snapshots) dedup to one entry instead of producing
    duplicate s3:// lines in verified.txt.

    Idempotent: url_normalize(url_normalize(x)) == url_normalize(x) for all x.

    Per FOUN-01 and D-02. The safe= chars include parentheses because TP-Link
    uses them in S3 key filenames (e.g., A10(JP)V1_GPL.tar.bz2) — encoding them
    creates phantom duplicates.

    Args:
        raw: Raw URL string, possibly with whitespace or encoding variations.

    Returns:
        Canonical HTTPS URL with normalized percent-encoding, or empty string
        for blank/whitespace-only input.

    """
    raw = raw.strip()
    if not raw:
        return raw
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    # Collapse http -> https: S3 object identity is (bucket, key) — the request
    # scheme is irrelevant (HEAD checks target s3.amazonaws.com over https via
    # to_s3_origin_url regardless of input scheme). Old Wayback snapshots surface
    # http:// variants of objects also discovered as https://; without this they
    # survive dedup as distinct keys, then collapse to identical s3:// lines in
    # verified.txt once to_s5cmd_url strips the scheme (28 phantom dupes observed).
    if scheme == "http":
        scheme = "https"
    netloc = parts.netloc.lower()
    # Decode-then-re-encode collapses double-encoding and normalizes
    # raw characters to their percent-encoded equivalents.
    # safe= preserves all chars that are legal unencoded in S3 key paths
    # and that appear in the seed data without encoding.
    path = quote(unquote(parts.path), safe="/:@!'()*-._~")
    return urlunsplit((scheme, netloc, path, "", ""))


def to_s3_origin_url(https_url: str) -> str:
    """Return the S3 origin URL for a canonical HTTPS URL.

    Targets s3.amazonaws.com/{bucket}/{key} directly, bypassing CloudFront,
    to receive richer metadata headers (x-amz-version-id, replication-status,
    x-amz-server-side-encryption) that CloudFront strips.

    The bucket name is the hostname of the original URL (e.g., static.tp-link.com
    or static.mercusys.com). This form works for all known TP-Link S3 buckets.

    Per VERF-04.

    Args:
        https_url: Canonical HTTPS URL (output of url_normalize).

    Returns:
        S3 origin URL in the form https://s3.amazonaws.com/{bucket}/{path}.

    """
    parts = urlsplit(https_url)
    # parts.netloc = "static.tp-link.com" → bucket name
    # parts.path = "/upload/gpl-code/..." → object key with leading slash
    return f"https://s3.amazonaws.com/{parts.netloc}{parts.path}"


def to_s3_regional_url(https_url: str, region: str) -> str:
    """Return the region-qualified S3 origin URL for a canonical HTTPS URL.

    The default path-style endpoint (s3.amazonaws.com) only serves buckets in
    us-east-1. A bucket in any other region answers a path-style request with
    301 Moved Permanently and names its real region in the x-amz-bucket-region
    response header. This builds the region-qualified endpoint
    (s3.{region}.amazonaws.com) that serves the object directly — and, unlike
    following the CloudFront redirect, still returns the rich x-amz-* metadata
    headers that to_s3_origin_url targets.

    The Mercusys GPL bucket (static.mercusys.com) lives in ap-southeast-1, so
    every Mercusys HEAD takes this regional-retry path. See docs/GPL-RECON.md
    ("The Two Buckets") — only static.tp-link.com is us-east-1.

    Per VERF-04.

    Args:
        https_url: Canonical HTTPS URL (output of url_normalize).
        region: AWS region code from the 301 response's x-amz-bucket-region header.

    Returns:
        S3 regional URL in the form https://s3.{region}.amazonaws.com/{bucket}/{path}.

    """
    parts = urlsplit(https_url)
    return f"https://s3.{region}.amazonaws.com/{parts.netloc}{parts.path}"


def to_s5cmd_url(https_url: str) -> str:
    """Convert canonical HTTPS URL to the s3:// form s5cmd expects.

    Applied only at the final verified.txt write step — not stored internally.
    The bucket name (hostname) becomes the S3 authority in the s3:// URI.

    s5cmd usage:
        s5cmd --no-sign-request cp s3://static.tp-link.com/... ./

    The path is percent-DECODED back to the literal object key. s5cmd keys on the
    raw object name and percent-encodes it itself when signing the S3 request, so
    handing it the canonical (already-encoded) path makes it double-encode — a
    space becomes %2520 on the wire and S3 returns 403/404. Every key containing a
    space, &, +, or [] breaks without this. Parentheses, which url_normalize keeps
    literal, are unaffected. Verified empirically: literal key -> 200, %-encoded
    key -> 403 against s3.amazonaws.com.

    Per VERF-06 and D-02.

    Args:
        https_url: Canonical HTTPS URL (output of url_normalize).

    Returns:
        s3:// URL in the form s3://{bucket}/{key} with the literal (decoded) key.

    """
    parts = urlsplit(https_url)
    return f"s3://{parts.netloc}{unquote(parts.path)}"


def to_s5cmd_cp_line(https_url: str) -> str:
    """Render one ``cp --if-size-differ`` command line for an s5cmd run-file.

    ``s5cmd --no-sign-request run <file>`` executes one command per line; this
    builds the copy for a single object. ``--if-size-differ`` makes the whole run
    idempotent and resumable: on re-run, files whose local size already matches are
    skipped and only missing/truncated ones are re-fetched. The local destination
    mirrors the bucket layout under ``gpl/`` (``gpl/{bucket}/{key}``).

    The source carries the literal (decoded) key from to_s5cmd_url — s5cmd
    re-encodes the key when signing, so a pre-encoded path would 403. Arguments are
    single-quoted so spaces, &, +, and () in keys survive shell word-splitting; the
    only char that would break this is a single quote, which no known
    TP-Link/Mercusys GPL key contains.

    Per VERF-06. See [[reference-s5cmd-download-command]] for the verified run flags.

    Args:
        https_url: Canonical HTTPS URL (output of url_normalize).

    Returns:
        A ``cp --if-size-differ '<s3://…>' 'gpl/<bucket>/<key>'`` command line.

    """
    s3 = to_s5cmd_url(https_url)
    dest = "gpl/" + s3.removeprefix("s3://")
    return f"cp --if-size-differ '{s3}' '{dest}'"

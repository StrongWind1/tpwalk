"""Tests for tpwalk._normalize — URL canonicalization and S3 URL conversion.

Verifies FOUN-01 (idempotent normalization), VERF-03 (encoding collapse),
VERF-06 (s5cmd URL format), and D-02 (canonical form is full HTTPS URL).
"""

from __future__ import annotations

import pytest

from tpwalk._normalize import to_s3_origin_url, to_s3_regional_url, to_s5cmd_cp_line, to_s5cmd_url, url_normalize
from tpwalk.models import DeadEntry, VerifiedEntry


class TestUrlNormalize:
    """Tests for url_normalize() idempotency and encoding correctness."""

    def test_url_normalize_idempotent(self, sample_urls: list[str]) -> None:
        for url in sample_urls:
            once = url_normalize(url)
            twice = url_normalize(once)
            assert once == twice, f"Not idempotent: {url!r} -> {once!r} -> {twice!r}"

    def test_url_normalize_collapses_space_encoding(self) -> None:
        raw = "https://static.tp-link.com/2018/201804/20180404/EAP Controller_V2.5_GPL.zip"
        pct = "https://static.tp-link.com/2018/201804/20180404/EAP%20Controller_V2.5_GPL.zip"
        assert url_normalize(raw) == url_normalize(pct)

    def test_url_normalize_preserves_parens(self) -> None:
        url = "https://static.tp-link.com/resources/gpl/A10(JP)V1_GPL.tar.bz2"
        result = url_normalize(url)
        assert "(" in result, f"Opening paren lost from {url!r}: {result!r}"
        assert ")" in result, f"Closing paren lost from {url!r}: {result!r}"
        assert "%28" not in result, f"Parens encoded in {result!r}"
        assert "%29" not in result, f"Parens encoded in {result!r}"

    def test_url_normalize_lowercases_scheme_host(self) -> None:
        mixed = "HTTPS://STATIC.TP-LINK.COM/resources/gpl/11N_GPL.tgz"
        result = url_normalize(mixed)
        assert result == "https://static.tp-link.com/resources/gpl/11N_GPL.tgz"

    def test_url_normalize_collapses_http_to_https(self) -> None:
        # http:// and https:// address the same S3 object; collapsing the scheme
        # stops Wayback's http:// snapshots from surviving dedup as distinct keys
        # and producing duplicate s3:// lines in verified.txt.
        http = "http://static.tp-link.com/resources/gpl/wr841nv9_en_gpl.tar.gz"
        https = "https://static.tp-link.com/resources/gpl/wr841nv9_en_gpl.tar.gz"
        assert url_normalize(http) == url_normalize(https)
        assert url_normalize(http) == https

    def test_url_normalize_strips_whitespace(self) -> None:
        padded = "  https://static.tp-link.com/resources/gpl/11N_GPL.tgz  "
        result = url_normalize(padded)
        assert result == "https://static.tp-link.com/resources/gpl/11N_GPL.tgz"

    def test_url_normalize_strips_query_fragment(self) -> None:
        with_query = "https://static.tp-link.com/resources/gpl/11N_GPL.tgz?foo=bar"
        with_frag = "https://static.tp-link.com/resources/gpl/11N_GPL.tgz#section"
        both = "https://static.tp-link.com/resources/gpl/11N_GPL.tgz?q=1#frag"
        expected = "https://static.tp-link.com/resources/gpl/11N_GPL.tgz"
        assert url_normalize(with_query) == expected
        assert url_normalize(with_frag) == expected
        assert url_normalize(both) == expected

    def test_url_normalize_empty_string(self) -> None:
        assert url_normalize("") == ""
        assert url_normalize("   ") == ""

    def test_url_normalize_acceptance_criteria(self) -> None:
        raw = "https://static.tp-link.com/2018/201804/20180404/EAP Controller_V2.5_GPL.zip"
        pct = "https://static.tp-link.com/2018/201804/20180404/EAP%20Controller_V2.5_GPL.zip"
        assert url_normalize(raw) == url_normalize(pct)


class TestToS3OriginUrl:
    """Tests for to_s3_origin_url() — bypass CloudFront to get richer S3 metadata."""

    def test_to_s3_origin_url_legacy(self) -> None:
        https_url = "https://static.tp-link.com/resources/gpl/11N_GPL.tgz"
        expected = "https://s3.amazonaws.com/static.tp-link.com/resources/gpl/11N_GPL.tgz"
        assert to_s3_origin_url(https_url) == expected

    def test_to_s3_origin_url_date_path(self) -> None:
        https_url = "https://static.tp-link.com/upload/gpl-code/2022/202201/20220101/X.tar.gz"
        expected = "https://s3.amazonaws.com/static.tp-link.com/upload/gpl-code/2022/202201/20220101/X.tar.gz"
        assert to_s3_origin_url(https_url) == expected

    def test_to_s3_origin_url_mercusys(self) -> None:
        https_url = "https://static.mercusys.com/gpl/MR500V1_gpl_src.tar.gz"
        expected = "https://s3.amazonaws.com/static.mercusys.com/gpl/MR500V1_gpl_src.tar.gz"
        assert to_s3_origin_url(https_url) == expected

    def test_to_s3_origin_url_acceptance_criteria(self) -> None:
        result = to_s3_origin_url("https://static.tp-link.com/resources/gpl/11N_GPL.tgz")
        assert result == "https://s3.amazonaws.com/static.tp-link.com/resources/gpl/11N_GPL.tgz"


class TestToS3RegionalUrl:
    """Tests for to_s3_regional_url() — region-qualified endpoint for non-us-east-1 buckets."""

    def test_to_s3_regional_url_mercusys(self) -> None:
        # The Mercusys GPL bucket is in ap-southeast-1; the path-style endpoint
        # 301-redirects there, so HEADs must target the regional endpoint.
        https_url = "https://static.mercusys.com/gpl/MR500V1_gpl_src.tar.gz"
        expected = "https://s3.ap-southeast-1.amazonaws.com/static.mercusys.com/gpl/MR500V1_gpl_src.tar.gz"
        assert to_s3_regional_url(https_url, "ap-southeast-1") == expected

    def test_to_s3_regional_url_preserves_key(self) -> None:
        # Spaces/parens already percent-encoded by url_normalize must survive untouched.
        https_url = "https://static.mercusys.com/gpl/Halo_H80X_GPL.tar20220704030817.gz"
        result = to_s3_regional_url(https_url, "ap-southeast-1")
        assert result.startswith("https://s3.ap-southeast-1.amazonaws.com/static.mercusys.com/")
        assert result.endswith("/gpl/Halo_H80X_GPL.tar20220704030817.gz")


class TestToS5cmdUrl:
    """Tests for to_s5cmd_url() — s3:// form for s5cmd bulk download input."""

    def test_to_s5cmd_url_legacy(self) -> None:
        https_url = "https://static.tp-link.com/resources/gpl/11N_GPL.tgz"
        expected = "s3://static.tp-link.com/resources/gpl/11N_GPL.tgz"
        assert to_s5cmd_url(https_url) == expected

    def test_to_s5cmd_url_date_path(self) -> None:
        https_url = "https://static.tp-link.com/upload/gpl-code/2022/202201/20220101/X.tar.gz"
        expected = "s3://static.tp-link.com/upload/gpl-code/2022/202201/20220101/X.tar.gz"
        assert to_s5cmd_url(https_url) == expected

    def test_to_s5cmd_url_mercusys(self) -> None:
        https_url = "https://static.mercusys.com/gpl/MR500V1_gpl_src.tar.gz"
        expected = "s3://static.mercusys.com/gpl/MR500V1_gpl_src.tar.gz"
        assert to_s5cmd_url(https_url) == expected

    def test_to_s5cmd_url_acceptance_criteria(self) -> None:
        result = to_s5cmd_url("https://static.tp-link.com/resources/gpl/11N_GPL.tgz")
        assert result == "s3://static.tp-link.com/resources/gpl/11N_GPL.tgz"

    def test_to_s5cmd_url_decodes_to_literal_key(self) -> None:
        # s5cmd keys on the literal object name and percent-encodes it itself; a
        # canonical (encoded) path would be double-encoded on the wire (%20 ->
        # %2520) and 403. The s3:// form must carry the decoded key. Verified
        # against S3: literal key -> 200, %-encoded key -> 403.
        cases = {
            "https://static.tp-link.com/upload/gpl-code/2022/202209/20220914/Tapo%20C420(GPL).tar.gz": "s3://static.tp-link.com/upload/gpl-code/2022/202209/20220914/Tapo C420(GPL).tar.gz",
            "https://static.tp-link.com/resources/gpl/GPL_Archer%20A6%26C6.tar.gz": "s3://static.tp-link.com/resources/gpl/GPL_Archer A6&C6.tar.gz",
            "https://static.tp-link.com/upload/gpl-code/2025/202503/20250307/GPL_Archer_GE550v1%2BGE650v1.tar.gz": "s3://static.tp-link.com/upload/gpl-code/2025/202503/20250307/GPL_Archer_GE550v1+GE650v1.tar.gz",
        }
        for encoded, expected in cases.items():
            assert to_s5cmd_url(encoded) == expected


class TestToS5cmdCpLine:
    """Tests for to_s5cmd_cp_line() — runnable `cp --if-size-differ` manifest lines."""

    def test_to_s5cmd_cp_line_basic(self) -> None:
        result = to_s5cmd_cp_line("https://static.tp-link.com/resources/gpl/11N_GPL.tgz")
        assert result == "cp --if-size-differ 's3://static.tp-link.com/resources/gpl/11N_GPL.tgz' 'gpl/static.tp-link.com/resources/gpl/11N_GPL.tgz'"

    def test_to_s5cmd_cp_line_has_if_size_differ(self) -> None:
        # The resume flag is the whole point — re-runs must skip same-size files.
        assert to_s5cmd_cp_line("https://static.tp-link.com/resources/gpl/11N_GPL.tgz").startswith("cp --if-size-differ ")

    def test_to_s5cmd_cp_line_literal_key_in_src_and_dest(self) -> None:
        # Both the s3:// source and the gpl/ destination must carry the decoded
        # literal key (s5cmd re-encodes the source; a pre-encoded key would 403).
        result = to_s5cmd_cp_line("https://static.tp-link.com/upload/gpl-code/2022/202209/20220914/Tapo%20C420(GPL).tar.gz")
        assert result == "cp --if-size-differ 's3://static.tp-link.com/upload/gpl-code/2022/202209/20220914/Tapo C420(GPL).tar.gz' 'gpl/static.tp-link.com/upload/gpl-code/2022/202209/20220914/Tapo C420(GPL).tar.gz'"

    def test_to_s5cmd_cp_line_mercusys(self) -> None:
        result = to_s5cmd_cp_line("https://static.mercusys.com/gpl/MR500V1_gpl_src.tar.gz")
        assert result == "cp --if-size-differ 's3://static.mercusys.com/gpl/MR500V1_gpl_src.tar.gz' 'gpl/static.mercusys.com/gpl/MR500V1_gpl_src.tar.gz'"


class TestDataModels:
    """Tests for VerifiedEntry and DeadEntry dataclass definitions."""

    def test_verified_entry_fields(self) -> None:
        entry = VerifiedEntry(
            url="https://static.tp-link.com/resources/gpl/11N_GPL.tgz",
            size=423447438,
            etag='"cef8ad3c3e60d16ddd36945117b02767"',
            content_type="application/x-gzip",
            last_modified="Tue, 07 Feb 2017 03:42:32 GMT",
            version_id=None,
            replication_status=None,
            encryption=None,
            server="AmazonS3",
            status=200,
            checked_at="2026-05-28T00:00:00+00:00",
        )
        assert entry.url == "https://static.tp-link.com/resources/gpl/11N_GPL.tgz"
        assert entry.size == 423447438
        assert entry.version_id is None
        assert entry.status == 200

    def test_dead_entry_fields(self) -> None:
        dead_http = DeadEntry(
            url="https://static.tp-link.com/resources/gpl/missing.tgz",
            status=404,
            error_type="http",
            checked_at="2026-05-28T00:00:00+00:00",
        )
        assert dead_http.status == 404

        dead_net = DeadEntry(
            url="https://static.tp-link.com/resources/gpl/missing.tgz",
            status=None,
            error_type="network",
            checked_at="2026-05-28T00:00:00+00:00",
        )
        assert dead_net.status is None

    def test_dataclasses_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        entry = VerifiedEntry(
            url="https://static.tp-link.com/resources/gpl/11N_GPL.tgz",
            size=1,
            etag='"abc"',
            content_type="application/x-gzip",
            last_modified="Tue, 07 Feb 2017 03:42:32 GMT",
            version_id=None,
            replication_status=None,
            encryption=None,
            server="AmazonS3",
            status=200,
            checked_at="2026-05-28T00:00:00+00:00",
        )
        with pytest.raises(FrozenInstanceError):
            entry.url = "changed"  # type: ignore[misc]

        dead = DeadEntry(
            url="https://static.tp-link.com/resources/gpl/missing.tgz",
            status=404,
            error_type="http",
            checked_at="2026-05-28T00:00:00+00:00",
        )
        with pytest.raises(FrozenInstanceError):
            dead.status = 200  # type: ignore[misc]

    def test_verified_entry_optional_fields_typed(self) -> None:
        entry_modern = VerifiedEntry(
            url="https://static.tp-link.com/upload/gpl-code/2022/202201/20220101/X.tar.gz",
            size=1000000,
            etag='"abc123"',
            content_type="application/x-gzip",
            last_modified="Mon, 01 Jan 2022 00:00:00 GMT",
            version_id="NB6JlD_Y7Cb3ZQPioiavpF3X1qRZztZY",
            replication_status="COMPLETED",
            encryption="AES256",
            server="AmazonS3",
            status=200,
            checked_at="2026-05-28T00:00:00+00:00",
        )
        assert entry_modern.version_id == "NB6JlD_Y7Cb3ZQPioiavpF3X1qRZztZY"
        assert entry_modern.replication_status == "COMPLETED"
        assert entry_modern.encryption == "AES256"

        entry_legacy = VerifiedEntry(
            url="https://static.tp-link.com/resources/gpl/11N_GPL.tgz",
            size=423447438,
            etag='"cef8ad3c"',
            content_type="application/x-gzip",
            last_modified="Tue, 07 Feb 2017 03:42:32 GMT",
            version_id=None,
            replication_status=None,
            encryption=None,
            server="AmazonS3",
            status=200,
            checked_at="2026-05-28T00:00:00+00:00",
        )
        assert entry_legacy.version_id is None
        assert entry_legacy.replication_status is None
        assert entry_legacy.encryption is None

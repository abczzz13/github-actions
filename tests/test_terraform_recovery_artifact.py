from contextlib import redirect_stdout
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO, StringIO
import json
from threading import Thread
import unittest
from unittest.mock import call, patch
from urllib.error import HTTPError, URLError
from zipfile import BadZipFile, ZipFile

import verify_terraform_recovery_artifact as verification


class RecoveryArtifactTests(unittest.TestCase):
    def setUp(self):
        self.name = "errored-tfstate-.infra-test-42-1-unique"
        self.api = "https://api.github.com"
        self.list_url = f"{self.api}/repos/owner/repository/actions/runs/42/artifacts?name={self.name}"
        self.zip_url = f"{self.api}/repos/owner/repository/actions/artifacts/123/zip"
        self.archive = self.zip_bytes({"errored.tfstate": verification.EXPECTED_STATE})
        self.metadata = {"id": 123, "name": self.name, "expired": False,
                         "digest": f"sha256:{hashlib.sha256(self.archive).hexdigest()}"}
        self.sleep = self.enterContext(patch.object(verification.time, "sleep"))
        self.read = self.enterContext(patch.object(verification, "read_url"))
        self.log = StringIO()
        self.enterContext(redirect_stdout(self.log))

    def zip_bytes(self, files):
        output = BytesIO()
        with ZipFile(output, "w") as zipped:
            for name, content in files.items():
                zipped.writestr(name, content)
        return output.getvalue()

    def listing(self, present=True, **overrides):
        artifacts = [self.metadata | overrides] if present else []
        return json.dumps({"total_count": len(artifacts), "artifacts": artifacts}).encode()

    def verify(self, failure="state"):
        verification.verify(self.api, "owner/repository", "42", self.name, failure, "private-token")
        self.assertNotIn("private-token", self.log.getvalue())

    def test_waits_for_the_named_artifact_then_verifies_digest_and_bytes(self):
        self.read.side_effect = [self.listing(False), self.listing(False), self.listing(), self.archive]
        self.verify()
        self.assertEqual(self.read.call_args_list,
                         [call(self.list_url, "private-token")] * 3 + [call(self.zip_url, "private-token")])
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])
        self.assertIn("SHA-256 and exact state bytes verified", self.log.getvalue())

    def test_missing_state_artifact_fails_after_a_bounded_wait(self):
        self.read.return_value = self.listing(False)
        with self.assertRaisesRegex(RuntimeError, "failed after 7 attempts"):
            self.verify()
        self.assertEqual(self.read.call_count, 7)
        self.assertEqual(self.sleep.call_args_list, [call(delay) for delay in verification.RETRY_DELAYS])

    def test_ordinary_failure_requires_a_successful_empty_listing(self):
        self.read.return_value = self.listing(False)
        self.verify("ordinary")
        self.read.assert_called_once_with(self.list_url, "private-token")
        self.sleep.assert_not_called()

    def test_ordinary_failure_rejects_an_unexpected_upload(self):
        self.read.return_value = self.listing()
        with self.assertRaisesRegex(RuntimeError, "unexpectedly uploaded"):
            self.verify("ordinary")
        self.sleep.assert_not_called()

    def test_authentication_and_api_errors_are_never_proof_of_absence(self):
        for failure in ("state", "ordinary"):
            for status in (401, 403, 404):
                with self.subTest(failure=failure, status=status):
                    self.read.reset_mock()
                    self.read.side_effect = HTTPError(self.list_url, status, "error", {}, None)
                    with self.assertRaisesRegex(RuntimeError, f"HTTP {status}"):
                        self.verify(failure)
                    self.read.assert_called_once()
        self.sleep.assert_not_called()

    def test_retries_service_and_transport_errors_without_retrying_assertions(self):
        errors = [HTTPError(self.list_url, 503, "unavailable", {}, None), URLError("connection reset"),
                  TimeoutError("read timed out")]
        self.read.side_effect = errors + [self.listing(), self.archive]
        self.verify()
        self.assertEqual(self.read.call_count, 5)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2), call(4)])

    def test_retries_a_temporarily_unavailable_archive(self):
        self.read.side_effect = [self.listing(), HTTPError(self.zip_url, 404, "not ready", {}, None),
                                 HTTPError(self.zip_url, 429, "rate limited", {}, None), self.archive]
        self.verify()
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])

    def test_a_persistent_service_error_fails_even_for_the_absence_check(self):
        self.read.side_effect = HTTPError(self.list_url, 503, "unavailable", {}, None)
        with self.assertRaisesRegex(RuntimeError, "failed after 7 attempts"):
            self.verify("ordinary")
        self.assertEqual(self.read.call_count, 7)

    def test_rejects_invalid_list_responses_without_retrying(self):
        responses = [b"not-json", b"[]", b"{}", b'{"artifacts":null}', b'{"total_count":false,"artifacts":[]}',
                     b'{"total_count":1,"artifacts":[null]}', self.listing(name="other-artifact")]
        for response in responses:
            with self.subTest(response=response):
                self.read.side_effect = None
                self.read.return_value = response
                with self.assertRaises((RuntimeError, ValueError)):
                    self.verify("ordinary")
        self.sleep.assert_not_called()

    def test_waits_while_the_count_and_list_disagree(self):
        lagging = json.dumps({"total_count": 1, "artifacts": []}).encode()
        self.read.side_effect = [lagging, self.listing(), self.archive]
        self.verify()
        self.assertEqual(self.sleep.call_args_list, [call(1)])
        self.assertIn("artifact is not visible yet", self.log.getvalue())

    def test_a_persistent_count_mismatch_is_never_proof_of_absence(self):
        for failure in ("state", "ordinary"):
            with self.subTest(failure=failure):
                self.read.reset_mock()
                self.read.side_effect = None
                self.read.return_value = json.dumps({"total_count": 1, "artifacts": []}).encode()
                with self.assertRaisesRegex(RuntimeError, "failed after 7 attempts"):
                    self.verify(failure)
                self.assertEqual(self.read.call_count, 7)

    def test_rejects_invalid_artifact_metadata(self):
        for metadata in ({"expired": True}, {"id": 0}, {"id": True}, {"digest": None}, {"digest": ""}):
            with self.subTest(metadata=metadata):
                self.read.return_value = self.listing(**metadata)
                with self.assertRaises(RuntimeError):
                    self.verify()
        self.sleep.assert_not_called()

    def test_digest_mismatch_fails_without_retrying(self):
        self.read.side_effect = [self.listing(digest="sha256:" + "0" * 64), self.archive]
        with self.assertRaisesRegex(RuntimeError, "digest does not match"):
            self.verify()
        self.sleep.assert_not_called()

    def test_rejects_wrong_bytes_extra_files_and_unsafe_paths(self):
        for files in ({"errored.tfstate": b"x" * len(verification.EXPECTED_STATE)},
                      {"errored.tfstate": b"short"},
                      {"errored.tfstate": verification.EXPECTED_STATE, "extra": b"x"},
                      {"../errored.tfstate": verification.EXPECTED_STATE}):
            with self.subTest(files=list(files)):
                archive = self.zip_bytes(files)
                digest = f"sha256:{hashlib.sha256(archive).hexdigest()}"
                self.read.side_effect = [self.listing(digest=digest), archive]
                with self.assertRaises(RuntimeError):
                    self.verify()
        self.sleep.assert_not_called()

    def test_rejects_an_invalid_zip_even_with_a_matching_digest(self):
        archive = b"not a zip"
        digest = f"sha256:{hashlib.sha256(archive).hexdigest()}"
        self.read.side_effect = [self.listing(digest=digest), archive]
        with self.assertRaises(BadZipFile):
            self.verify()
        self.sleep.assert_not_called()


class ArtifactTransportTests(unittest.TestCase):
    def test_archive_redirect_does_not_forward_the_token_to_blob_storage(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization")))
                if self.path == "/archive":
                    self.send_response(302)
                    self.send_header("Location", f"http://localhost:{self.server.server_port}/blob")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"archive-bytes")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = verification.read_url(f"http://127.0.0.1:{server.server_port}/archive", "private-token")
            self.assertEqual(result, b"archive-bytes")
            self.assertEqual(requests, [("/archive", "Bearer private-token"), ("/blob", None)])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()

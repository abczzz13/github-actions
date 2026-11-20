"""Verify the hosted recovery fixture through GitHub's artifact REST API."""

import hashlib
from http.client import HTTPException
from io import BytesIO
import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import BadZipFile, ZipFile

EXPECTED_STATE = b'{"fixture":"not-real-state"}\n'
RETRY_DELAYS = (1, 2, 4, 8, 8, 8)
MAX_RESPONSE_BYTES = 1024 * 1024


class ArtifactPending(Exception):
    pass


class WithoutAuthorizationRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            # The archive API redirects to blob storage. Never forward the token.
            redirected.remove_header("Authorization")
        return redirected


def read_url(url, token):
    request = Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with build_opener(WithoutAuthorizationRedirect()).open(request, timeout=10) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise RuntimeError("The fixture response exceeds its size limit.")
    return body


def retry(operation, description, retry_not_found=False):
    # Retry service availability, not assertions about the artifact's contents.
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return operation()
        except HTTPError as error:
            status = error.code
            error.close()
            if status not in (429, 500, 502, 503, 504) and not (retry_not_found and status == 404):
                raise RuntimeError(f"{description} failed: HTTP {status}.") from None
            reason = f"HTTP {status}"
        except (URLError, TimeoutError, ConnectionError, HTTPException):
            reason = "network error"
        except ArtifactPending:
            reason = "artifact is not visible yet"
        if attempt == len(RETRY_DELAYS):
            raise RuntimeError(f"{description} failed after {attempt + 1} attempts: {reason}.")
        delay = RETRY_DELAYS[attempt]
        print(f"{description}: {reason}; retrying in {delay}s.", flush=True)
        time.sleep(delay)


def verify(api_url, repository, run_id, name, failure, token):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not run_id.isdecimal():
        raise ValueError("A repository and numeric workflow run ID are required.")
    if not name or not token or failure not in ("state", "ordinary"):
        raise ValueError("An artifact name, token and valid fixture failure mode are required.")
    base = f"{api_url.rstrip('/')}/repos/{repository}/actions"
    url = f"{base}/runs/{run_id}/artifacts?{urlencode({'name': name})}"

    def find():
        response = json.loads(read_url(url, token))
        if not isinstance(response, dict) or not isinstance(response.get("artifacts"), list):
            raise RuntimeError("Invalid artifact-list response.")
        artifacts = response["artifacts"]
        if type(response.get("total_count")) is not int:
            raise RuntimeError("Invalid artifact-list response.")
        if any(not isinstance(item, dict) or item.get("name") != name for item in artifacts):
            raise RuntimeError("Unexpected artifact-list results for the reserved name.")
        if response["total_count"] != len(artifacts):
            # Shortly after an upload, the count and the list can disagree. Wait
            # for them to agree; a disagreement is never proof of absence.
            raise ArtifactPending()
        if not artifacts and failure == "state":
            raise ArtifactPending()
        return artifacts

    artifacts = retry(find, "Artifact lookup")
    if failure == "ordinary":
        if artifacts:
            raise RuntimeError("An ordinary apply failure unexpectedly uploaded an artifact.")
        print("Ordinary apply failure: the reserved recovery artifact is absent.")
        return
    if len(artifacts) != 1 or artifacts[0].get("expired") is not False:
        raise RuntimeError("Expected one unexpired recovery artifact.")
    artifact = artifacts[0]
    if type(artifact.get("id")) is not int or artifact["id"] <= 0:
        raise RuntimeError("The recovery artifact has no valid ID.")
    digest = artifact.get("digest", "")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise RuntimeError("The recovery artifact has no SHA-256 digest.")
    archive_url = f"{base}/artifacts/{artifact['id']}/zip"
    archive = retry(lambda: read_url(archive_url, token), "Artifact download", retry_not_found=True)
    if f"sha256:{hashlib.sha256(archive).hexdigest()}" != digest:
        raise RuntimeError("The recovery artifact digest does not match.")
    with ZipFile(BytesIO(archive)) as zipped:
        entries = zipped.infolist()
        if len(entries) != 1 or entries[0].filename != "errored.tfstate" or entries[0].file_size != len(EXPECTED_STATE):
            raise RuntimeError("The recovery artifact must contain only the expected state file.")
        # Read in memory, without extracting paths supplied by the archive.
        if zipped.read(entries[0]) != EXPECTED_STATE:
            raise RuntimeError("The recovery artifact state bytes do not match the fixture.")
    print(f"Recovery artifact {artifact['id']}: SHA-256 and exact state bytes verified.")


def main():
    try:
        verify(os.environ["GITHUB_API_URL"], os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_RUN_ID"],
               os.environ["ARTIFACT"], os.environ["FAILURE"], os.environ["GH_TOKEN"])
    except (KeyError, RuntimeError, ValueError, BadZipFile) as error:
        sys.exit(f"Recovery artifact verification failed: {error}")


if __name__ == "__main__":
    main()

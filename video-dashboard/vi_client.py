"""
Azure Arc Video Indexer Client
==============================
Handles authentication and API calls to the Video Indexer cloud API,
which proxies requests to the Arc-enabled VI extension on-cluster.

Auth flow:
  1. ClientSecretCredential → ARM token
  2. ARM generateAccessToken → VI access token (cached ~1hr)
  3. VI token used for all API calls
"""

import logging
import os
import threading
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# VI cloud API base (with location prefix)
VI_API_BASE = "https://api.videoindexer.ai"


class VideoIndexerClient:
    """Client for Arc-enabled Video Indexer via cloud API."""

    def __init__(self):
        self.account_id = os.getenv("VI_ACCOUNT_ID", "")
        self.account_name = os.getenv("VI_ACCOUNT_NAME", "")
        self.resource_group = os.getenv("VI_RESOURCE_GROUP", "")
        self.subscription_id = os.getenv("VI_SUBSCRIPTION_ID", "")
        self.location = os.getenv("VI_LOCATION", "eastus")

        self.tenant_id = os.getenv("AZURE_TENANT_ID", "")
        self.client_id = os.getenv("AZURE_CLIENT_ID", "")
        self.client_secret = os.getenv("AZURE_CLIENT_SECRET", "")

        self._vi_token: str | None = None
        self._vi_token_expiry: float = 0
        self._token_lock = threading.Lock()

        self._arm_credential = None

    @property
    def configured(self) -> bool:
        return bool(self.account_id and self.tenant_id and self.client_id and self.client_secret)

    def _get_arm_credential(self):
        """Lazy-init Azure credential."""
        if self._arm_credential is None:
            from azure.identity import ClientSecretCredential
            self._arm_credential = ClientSecretCredential(
                tenant_id=self.tenant_id,
                client_id=self.client_id,
                client_secret=self.client_secret,
            )
        return self._arm_credential

    def _get_arm_token(self) -> str:
        """Get ARM access token."""
        cred = self._get_arm_credential()
        token = cred.get_token("https://management.azure.com/.default")
        return token.token

    def _get_vi_token(self) -> str:
        """Get or refresh VI access token (thread-safe, cached)."""
        with self._token_lock:
            if self._vi_token and time.time() < self._vi_token_expiry - 60:
                return self._vi_token

            arm_token = self._get_arm_token()
            url = (
                f"https://management.azure.com/subscriptions/{self.subscription_id}"
                f"/resourceGroups/{self.resource_group}"
                f"/providers/Microsoft.VideoIndexer/accounts/{self.account_name}"
                f"/generateAccessToken?api-version=2024-01-01"
            )

            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    url,
                    json={"permissionType": "Contributor", "scope": "Account"},
                    headers={
                        "Authorization": f"Bearer {arm_token}",
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            self._vi_token = data["accessToken"]
            self._vi_token_expiry = time.time() + 3500  # ~58 min
            logger.info("VI access token refreshed (expires in ~58min)")
            return self._vi_token

    def _api_url(self, path: str) -> str:
        """Build full API URL."""
        return f"{VI_API_BASE}/{self.location}/Accounts/{self.account_id}/{path.lstrip('/')}"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_vi_token()}"}

    # ── Video operations ─────────────────────────────────────────────────

    def upload_video(self, file_path: str, name: str,
                     description: str = "", callback_url: str = "") -> dict:
        """Upload a video file to VI for indexing.

        Returns: {"id": "...", "name": "...", "state": 1, ...}
        """
        url = self._api_url("Videos")
        params = {
            "name": name,
            "language": "en-US",
            "indexingPreset": "Default",
            "streamingPreset": "NoStreaming",
        }
        if description:
            params["description"] = description
        if callback_url:
            params["callbackUrl"] = callback_url

        with open(file_path, "rb") as f:
            with httpx.Client(timeout=600.0) as client:
                resp = client.post(
                    url,
                    params=params,
                    headers=self._headers(),
                    files={"fileName": (Path(file_path).name, f, "video/mp4")},
                )
                resp.raise_for_status()
                return resp.json()

    def get_video(self, video_id: str) -> dict:
        """Get video metadata including state and progress."""
        url = self._api_url(f"Videos/{video_id}")
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    def get_video_index(self, video_id: str) -> dict:
        """Get full video index with all insights."""
        url = self._api_url(f"Videos/{video_id}/Index")
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    def list_videos(self, page_size: int = 25, skip: int = 0) -> dict:
        """List all indexed videos."""
        url = self._api_url("Videos")
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(
                url,
                params={"pageSize": page_size, "skip": skip},
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()

    def request_summary(self, video_id: str, length: str = "Medium",
                        style: str = "Neutral") -> dict | None:
        """Request textual summarization (async — returns 202)."""
        url = self._api_url(f"Videos/{video_id}/Summaries/Textual")
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                url,
                params={"length": length, "style": style},
                headers=self._headers(),
            )
            if resp.status_code == 202:
                return resp.json() if resp.content else {"status": "accepted"}
            resp.raise_for_status()
            return resp.json()

    def get_summary(self, video_id: str, summary_id: str) -> dict:
        """Get textual summary result."""
        url = self._api_url(f"Videos/{video_id}/Summaries/Textual/{summary_id}")
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    def search_videos(self, query: str) -> dict:
        """Natural language search across indexed videos."""
        url = self._api_url("videos/search")
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                url,
                json={"query": query},
                headers={**self._headers(), "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()

    def delete_video(self, video_id: str) -> bool:
        """Delete a video from VI."""
        url = self._api_url(f"Videos/{video_id}")
        with httpx.Client(timeout=30.0) as client:
            resp = client.delete(url, headers=self._headers())
            return resp.status_code == 204

    def health_check(self) -> bool:
        """Verify we can reach VI and authenticate."""
        try:
            result = self.list_videos(page_size=1)
            return "results" in result
        except Exception as exc:
            logger.warning("VI health check failed: %s", exc)
            return False

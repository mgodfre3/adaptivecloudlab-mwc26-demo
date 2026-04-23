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
        """Get video metadata including state and progress.

        The Arc VI API may return 404 on /Videos/{id} while /Videos/{id}/Index
        works. We try the direct endpoint first, then fall back to Index.
        """
        url = self._api_url(f"Videos/{video_id}")
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url, headers=self._headers())
            if resp.status_code == 200:
                return resp.json()

        # Fallback: use Index endpoint which reliably works on Arc VI
        index_data = self.get_video_index(video_id)
        return {
            "id": index_data.get("id", video_id),
            "name": index_data.get("name", ""),
            "state": index_data.get("state", "Unknown"),
            "processingProgress": "100%" if index_data.get("state") == "Processed" else "0%",
        }

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

    # ── BYOM: Custom Insights ───────────────────────────────────────────

    def patch_custom_insights(
        self,
        video_id: str,
        detections: list[dict],
        model_name: str = "Antenna Detection (YOLOv8)",
        replace: bool = False,
    ) -> bool:
        """Patch custom insights into an indexed video (BYOM integration).

        Takes detections from our YOLO model and injects them as custom
        insights visible in the VI portal and API.

        Args:
            video_id: VI video ID.
            detections: List of detection dicts from cv_inference (must have
                        label, confidence, timestamp fields).
            model_name: Display name for the custom insight group.
            replace: If True, replace existing custom insights; otherwise add.

        Returns:
            True if patch succeeded.

        The patch format follows the Azure Video Indexer custom insights
        schema: /insights/customInsights with JSON Patch operations.
        """
        if not detections:
            logger.info("BYOM: No detections to patch for video %s", video_id)
            return True

        # Group detections by label → time-based instances
        from collections import defaultdict
        label_instances: dict[str, list[dict]] = defaultdict(list)

        for det in detections:
            label = det.get("label", "Unknown")
            ts = det.get("timestamp", 0)
            conf = det.get("confidence", 0.0)
            # Each instance is a time window around the detection
            start_sec = max(0, ts - 0.5)
            end_sec = ts + 0.5
            label_instances[label].append({
                "Start": _format_vi_time(start_sec),
                "End": _format_vi_time(end_sec),
                "AdjustedStart": _format_vi_time(start_sec),
                "AdjustedEnd": _format_vi_time(end_sec),
                "Confidence": round(conf, 4),
            })

        # Build custom insight results
        results = []
        for idx, (label, instances) in enumerate(label_instances.items()):
            # Merge overlapping instances
            merged = _merge_time_instances(instances)
            results.append({
                "Type": label,
                "SubType": f"{label}_id",
                "Id": idx + 1,
                "Instances": merged,
            })

        custom_insights = {
            "Name": model_name,
            "DisplayName": model_name,
            "DisplayType": "CapsuleAndTags",
            "Results": results,
        }

        patch_body = [{
            "op": "replace" if replace else "add",
            "value": [custom_insights],
            "path": "/insights/customInsights",
        }]

        url = self._api_url(f"Videos/{video_id}/Index")
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.patch(
                    url,
                    json=patch_body,
                    headers={
                        **self._headers(),
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()
                logger.info(
                    "BYOM: Patched %d detection types (%d total) into video %s",
                    len(results),
                    sum(len(r["Instances"]) for r in results),
                    video_id,
                )
                return True
        except Exception as exc:
            logger.error("BYOM: Failed to patch insights for video %s: %s", video_id, exc)
            return False

    def get_video_thumbnails(self, video_id: str) -> list[dict]:
        """Get thumbnail/keyframe URLs for an indexed video."""
        url = self._api_url(f"Videos/{video_id}/Index")
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(url, headers=self._headers())
                resp.raise_for_status()
                index_data = resp.json()

            thumbnails = []
            for video in index_data.get("videos", [index_data]):
                for shot in video.get("insights", {}).get("shots", []):
                    for keyframe in shot.get("keyFrames", []):
                        for instance in keyframe.get("instances", []):
                            thumb_id = instance.get("thumbnailId", "")
                            if thumb_id:
                                thumbnails.append({
                                    "id": thumb_id,
                                    "start": instance.get("start", ""),
                                    "end": instance.get("end", ""),
                                    "url": self._api_url(
                                        f"Videos/{video_id}/Thumbnails/{thumb_id}"
                                    ),
                                })
            return thumbnails
        except Exception as exc:
            logger.warning("Failed to get thumbnails for %s: %s", video_id, exc)
            return []

    def health_check(self) -> bool:
        """Verify we can reach VI and authenticate."""
        try:
            result = self.list_videos(page_size=1)
            return "results" in result
        except Exception as exc:
            logger.warning("VI health check failed: %s", exc)
            return False


# ── Helper functions ─────────────────────────────────────────────────────

def _format_vi_time(seconds: float) -> str:
    """Format seconds as HH:MM:SS.FFFFFFF for VI API."""
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hrs:02d}:{mins:02d}:{secs:010.7f}"


def _merge_time_instances(instances: list[dict], gap_tolerance: float = 1.0) -> list[dict]:
    """Merge overlapping or near-adjacent time instances.

    Groups detections that are within gap_tolerance seconds of each other
    into single time ranges, averaging confidence scores.
    """
    if not instances:
        return instances

    def _parse_time(t: str) -> float:
        parts = t.split(":")
        return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])

    sorted_inst = sorted(instances, key=lambda x: _parse_time(x["Start"]))
    merged = [sorted_inst[0].copy()]

    for inst in sorted_inst[1:]:
        prev = merged[-1]
        prev_end = _parse_time(prev["End"])
        curr_start = _parse_time(inst["Start"])

        if curr_start <= prev_end + gap_tolerance:
            # Merge: extend end time, average confidence
            new_end = max(prev_end, _parse_time(inst["End"]))
            prev["End"] = _format_vi_time(new_end)
            prev["AdjustedEnd"] = prev["End"]
            prev["Confidence"] = round(
                (prev["Confidence"] + inst["Confidence"]) / 2, 4
            )
        else:
            merged.append(inst.copy())

    return merged

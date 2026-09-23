"""Google Ads asset creation — images and text.

Assets are account-level objects that ads and asset groups point at, so creating one is
a separate step from using it. Performance Max asset groups and Demand Gen ads both need
assets that already exist, which is why there was no way to build either: the only image
upload in this codebase lived inside extensions_manager.add_image_extension, bound to a
campaign link and not reachable on its own.

That older path is also wrong in ways worth not repeating here:
  * it hardcodes IMAGE_JPEG regardless of what was actually fetched;
  * it reads width and height from HTTP response headers, which do not carry image
    dimensions, so every asset it made recorded 0x0;
  * it base64-encodes the bytes and then .encode("utf-8") the result, sending base64 text
    where the API wants raw bytes.
None of that is needed. Asset.image_asset.data takes the raw bytes and Google derives the
mime type, file size and dimensions itself; this module reads them back afterwards rather
than guessing them up front.
"""

import base64
import binascii
import hashlib
from typing import Any, Dict, Optional

import httpx

from utils.logger import get_logger

logger = get_logger(__name__)

# Google's own limit for an image asset.
MAX_IMAGE_BYTES = 5 * 1024 * 1024

# Magic bytes, so the mime type comes from the file rather than from a URL's extension or
# a Content-Type header, both of which lie routinely.
_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]


def _sniff_mime(data: bytes) -> str:
    for prefix, mime in _MAGIC:
        if data.startswith(prefix):
            return mime
    raise ValueError(
        "unrecognised image format — Google Ads accepts PNG, JPEG and GIF"
    )


def _escape(value: str) -> str:
    """Escape a value for a GAQL string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class AssetManager:
    """Create and look up account-level assets."""

    def __init__(self, client):
        self.client = client

    # ------------------------------------------------------------------
    # Images
    # ------------------------------------------------------------------

    def _fetch_image(
        self,
        image_url: Optional[str] = None,
        image_base64: Optional[str] = None,
    ) -> bytes:
        """The image bytes, from whichever source was given.

        Both are supported because both happen: a harness that already stores creatives
        somewhere public passes a URL, and one that just generated an image has only the
        bytes. A URL is much the cheaper path — base64 of a 5MB image is ~6.7MB of text
        travelling through the model's context to get here.
        """
        if bool(image_url) == bool(image_base64):
            raise ValueError("pass exactly one of image_url or image_base64")

        if image_url:
            if not image_url.lower().startswith("https://"):
                raise ValueError("image_url must be https")
            try:
                response = httpx.get(image_url, timeout=30, follow_redirects=True)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise ValueError(f"could not fetch {image_url}: {exc}") from exc
            data = response.content
        else:
            try:
                # validate=True so a truncated or mistyped payload fails here, with a
                # message that says so, rather than as an opaque rejection from Google.
                data = base64.b64decode(image_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"image_base64 is not valid base64: {exc}") from exc

        if not data:
            raise ValueError("image is empty")
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError(
                f"image is {len(data) / 1024 / 1024:.1f}MB; Google's limit is 5MB"
            )
        _sniff_mime(data)
        return data

    def _find_asset_by_name(self, customer_id: str, name: str) -> Optional[Dict[str, Any]]:
        """An existing image asset with exactly this name, if the account has one."""
        ga_service = self.client.get_service("GoogleAdsService")
        query = (
            "SELECT asset.id, asset.name, asset.resource_name, "
            "asset.image_asset.full_size.width_pixels, "
            "asset.image_asset.full_size.height_pixels, "
            "asset.image_asset.file_size, asset.image_asset.mime_type "
            "FROM asset "
            f"WHERE asset.type = 'IMAGE' AND asset.name = '{_escape(name)}' "
            "LIMIT 1"
        )
        for row in ga_service.search(customer_id=customer_id, query=query):
            return self._describe(row.asset, reused=True)
        return None

    @staticmethod
    def _describe(asset, reused: bool) -> Dict[str, Any]:
        full_size = asset.image_asset.full_size
        return {
            "asset_id": str(asset.id),
            "resource_name": asset.resource_name,
            "name": asset.name,
            "width_pixels": full_size.width_pixels,
            "height_pixels": full_size.height_pixels,
            "file_size_bytes": asset.image_asset.file_size,
            "mime_type": asset.image_asset.mime_type.name
            if asset.image_asset.mime_type
            else None,
            "reused_existing": reused,
        }

    def upload_image_asset(
        self,
        customer_id: str,
        asset_name: str,
        image_url: Optional[str] = None,
        image_base64: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create an image asset, or return the identical one already in the account.

        Idempotency is by content, not by the name the caller chose: the stored name is
        the caller's name plus a short digest of the bytes. Uploading the same image twice
        returns the first asset; uploading a DIFFERENT image under a name already used
        makes a new asset rather than silently returning the old one, which name-only
        matching would do. Google assigns no stable identity of its own to an upload, so
        the digest has to be carried somewhere, and the name is the only writable field
        that survives.

        Returns the resource name and id, and the width and height Google derived — read
        back after creation rather than guessed, since the caller usually needs to know
        which aspect ratio slot the image qualifies for.
        """
        customer_id = str(customer_id).replace("-", "")
        data = self._fetch_image(image_url=image_url, image_base64=image_base64)
        digest = hashlib.sha256(data).hexdigest()[:12]
        stored_name = f"{asset_name} [{digest}]"

        existing = self._find_asset_by_name(customer_id, stored_name)
        if existing:
            logger.info(f"Reusing image asset {existing['resource_name']} for {stored_name}")
            return existing

        asset_service = self.client.get_service("AssetService")
        operation = self.client.get_type("AssetOperation")
        asset = operation.create
        asset.name = stored_name
        asset.type_ = self.client.enums.AssetTypeEnum.IMAGE
        # Raw bytes. Google derives mime type, file size and dimensions from them; setting
        # those fields here is what the extensions path got wrong.
        asset.image_asset.data = data

        response = asset_service.mutate_assets(
            customer_id=customer_id, operations=[operation]
        )
        resource_name = response.results[0].resource_name
        logger.info(f"Created image asset {resource_name} ({len(data)} bytes)")

        # Read back for the dimensions, which only exist server-side.
        created = self._find_asset_by_name(customer_id, stored_name)
        if created:
            created["reused_existing"] = False
            return created
        return {
            "asset_id": resource_name.rsplit("/", 1)[-1],
            "resource_name": resource_name,
            "name": stored_name,
            "width_pixels": None,
            "height_pixels": None,
            "file_size_bytes": len(data),
            "mime_type": _sniff_mime(data),
            "reused_existing": False,
        }

    # ------------------------------------------------------------------
    # Text
    # ------------------------------------------------------------------

    def create_text_asset(self, customer_id: str, text: str) -> Dict[str, Any]:
        """Create a text asset, or return the account's existing one with this text.

        Text assets are deduplicated by their text, which is the whole of their content —
        two assets with the same string are indistinguishable, so making a second is only
        clutter. Used for business names (<=25 chars, required by PMax asset groups), and
        usable for any other text field an asset group takes.
        """
        customer_id = str(customer_id).replace("-", "")
        text = (text or "").strip()
        if not text:
            raise ValueError("text is empty")

        ga_service = self.client.get_service("GoogleAdsService")
        query = (
            "SELECT asset.id, asset.resource_name, asset.text_asset.text "
            "FROM asset "
            f"WHERE asset.type = 'TEXT' AND asset.text_asset.text = '{_escape(text)}' "
            "LIMIT 1"
        )
        for row in ga_service.search(customer_id=customer_id, query=query):
            return {
                "asset_id": str(row.asset.id),
                "resource_name": row.asset.resource_name,
                "text": row.asset.text_asset.text,
                "reused_existing": True,
            }

        asset_service = self.client.get_service("AssetService")
        operation = self.client.get_type("AssetOperation")
        asset = operation.create
        asset.type_ = self.client.enums.AssetTypeEnum.TEXT
        asset.text_asset.text = text

        response = asset_service.mutate_assets(
            customer_id=customer_id, operations=[operation]
        )
        resource_name = response.results[0].resource_name
        logger.info(f"Created text asset {resource_name}")
        return {
            "asset_id": resource_name.rsplit("/", 1)[-1],
            "resource_name": resource_name,
            "text": text,
            "reused_existing": False,
        }

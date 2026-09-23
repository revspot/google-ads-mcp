"""MCP Tools - Account-level assets.

1. google_ads_upload_image_asset - Create an image asset from a URL or base64 bytes
2. google_ads_create_text_asset  - Create a text asset (business name and the like)

Assets exist at the account level and are referenced by ads and asset groups, so they
have to be created before anything that uses them. Performance Max asset groups and
Demand Gen ads both needed these and had no way to get them.
"""

from typing import Optional

from managers.asset_manager import AssetManager
from utils.auth_manager import get_auth_manager
from utils.error_handler import ErrorHandler
from utils.logger import get_audit_logger, get_logger, get_performance_logger

logger = get_logger(__name__)
performance_logger = get_performance_logger()
audit_logger = get_audit_logger()


def register_asset_tools(mcp):
    """Register asset tools on the shared FastMCP instance."""

    @mcp.tool()
    def google_ads_upload_image_asset(
        customer_id: str,
        asset_name: str,
        image_url: Optional[str] = None,
        image_base64: Optional[str] = None,
    ) -> str:
        """
        Upload an image to the account as a reusable image asset.

        Give exactly one of image_url or image_base64. Prefer image_url wherever the image
        already lives somewhere public — base64 of a large image is an enormous tool
        argument and costs far more than a fetch.

        Args:
            customer_id: Customer ID (without hyphens)
            asset_name: A name for the asset, e.g. "Spring sale hero"
            image_url: Public https URL of the image
            image_base64: The image bytes, base64-encoded

        Returns:
            The asset resource name and ID, plus the width and height Google derived.

        Formats: PNG, JPEG or GIF, at most 5MB.

        Sizes Google expects, by where the image is used:
          - landscape 1.91:1  (1200x628)   marketing image
          - square    1:1     (1200x1200)  square marketing image
          - portrait  4:5     (960x1200)   Demand Gen portrait
          - logo      1:1     (>=128x128, 1200x1200 preferred)

        Uploading the same image twice returns the existing asset rather than making a
        duplicate. Matching is on the image's content, so re-using a name with a different
        image creates a new asset instead of quietly returning the old one.
        """
        with performance_logger.track_operation(
            "upload_image_asset", customer_id=customer_id
        ):
            try:
                client = get_auth_manager().get_client()
                manager = AssetManager(client)

                result = manager.upload_image_asset(
                    customer_id=customer_id,
                    asset_name=asset_name,
                    image_url=image_url,
                    image_base64=image_base64,
                )

                audit_logger.log_api_call(
                    customer_id=customer_id,
                    operation="upload_image_asset",
                    resource_type="asset",
                    resource_id=result["asset_id"],
                    action="create",
                    details={"name": result["name"], "reused": result["reused_existing"]},
                )

                verb = "Reused existing" if result["reused_existing"] else "Created"
                dimensions = (
                    f"{result['width_pixels']}x{result['height_pixels']}"
                    if result["width_pixels"]
                    else "unknown"
                )
                output = f"# Image Asset\n\n"
                output += f"{verb} image asset.\n\n"
                output += f"- **Resource name**: `{result['resource_name']}`\n"
                output += f"- **Asset ID**: {result['asset_id']}\n"
                output += f"- **Name**: {result['name']}\n"
                output += f"- **Dimensions**: {dimensions}\n"
                output += f"- **Size**: {result['file_size_bytes']:,} bytes\n"
                output += f"- **Type**: {result['mime_type']}\n\n"
                output += "Pass the resource name to the tool that uses it.\n"
                return output

            except Exception as e:
                error_msg = ErrorHandler.handle_error(e, context="upload_image_asset")
                return f"❌ Failed to upload image asset: {error_msg}"

    @mcp.tool()
    def google_ads_create_text_asset(customer_id: str, text: str) -> str:
        """
        Create a text asset, e.g. a business name for a Performance Max asset group.

        Args:
            customer_id: Customer ID (without hyphens)
            text: The asset's text. A business name must be 25 characters or fewer.

        Returns:
            The asset resource name and ID.

        An account's text assets are deduplicated by their text, so calling this twice
        with the same string returns the same asset.

        Note this is the asset form, which asset groups reference. A Demand Gen
        multi-asset ad takes its business name as a plain string instead — pass the text
        to google_ads_create_demand_gen_ad directly, not this asset's resource name.
        """
        with performance_logger.track_operation(
            "create_text_asset", customer_id=customer_id
        ):
            try:
                client = get_auth_manager().get_client()
                manager = AssetManager(client)

                result = manager.create_text_asset(customer_id=customer_id, text=text)

                audit_logger.log_api_call(
                    customer_id=customer_id,
                    operation="create_text_asset",
                    resource_type="asset",
                    resource_id=result["asset_id"],
                    action="create",
                    details={"reused": result["reused_existing"]},
                )

                verb = "Reused existing" if result["reused_existing"] else "Created"
                output = f"# Text Asset\n\n"
                output += f"{verb} text asset.\n\n"
                output += f"- **Resource name**: `{result['resource_name']}`\n"
                output += f"- **Asset ID**: {result['asset_id']}\n"
                output += f"- **Text**: {result['text']}\n"
                return output

            except Exception as e:
                error_msg = ErrorHandler.handle_error(e, context="create_text_asset")
                return f"❌ Failed to create text asset: {error_msg}"

    logger.info("Asset tools registered (2 tools)")

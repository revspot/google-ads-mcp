"""MCP Tools - Demand Gen campaigns.

1. google_ads_create_demand_gen_campaign - Campaign and its budget
2. google_ads_create_demand_gen_ad_group - Ad group, optionally audience-targeted
3. google_ads_create_demand_gen_ad       - Multi-asset image ad

Build order is campaign, then ad group, then ad, because each names the one above it, and
the image assets the ad points at must exist before the ad does — see
google_ads_upload_image_asset.

Everything here is created PAUSED unless the caller says otherwise. These objects spend
money once enabled and the server holds one credential shared by every caller.
"""

import json
from typing import List, Optional, Union

from managers.demand_gen_manager import DemandGenManager
from utils.auth_manager import get_auth_manager
from utils.error_handler import ErrorHandler
from utils.logger import get_audit_logger, get_logger, get_performance_logger

logger = get_logger(__name__)
performance_logger = get_performance_logger()
audit_logger = get_audit_logger()


def _ids(value=None) -> Optional[List[str]]:
    """A list of identifiers, given as a JSON array or comma-separated.

    Safe to split on commas because none of the things this parses — numeric IDs and
    asset resource names — can contain one.
    """
    return _parse_list(value)


def _copy(value=None, field: str = "") -> Optional[List[str]]:
    """A list of ad copy, given as a JSON array or as a single string.

    NOT comma-separated. Ad copy contains commas as a matter of course — "One Agent, Full
    Funnel" is one headline, not two — and splitting on them silently cuts approved copy
    into fragments and can push the result past the five-headline limit. A plain string is
    therefore taken as ONE item; several items must come as a JSON array, which is also
    what create_asset_group already expects.
    """
    if value is None:
        return None
    parsed = _parse_list(value, split_on_commas=False)
    return parsed


def _parse_list(value=None, split_on_commas: bool = True) -> Optional[List[str]]:
    """JSON array if it looks like one, else a delimited or single value.

    Accepts an actual list too. MCP clients do not agree on what to do with a string
    argument whose content is JSON: some send it through untouched, some parse it and
    hand the tool a list, and the second kind fails a `str` annotation before any of this
    code runs. Taking both costs one isinstance check.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"looks like a JSON array but will not parse: {exc}") from exc
        if not isinstance(parsed, list):
            raise ValueError("expected a JSON array")
        return [str(item).strip() for item in parsed if str(item).strip()]
    if split_on_commas:
        return [item.strip() for item in text.split(",") if item.strip()]
    return [text]


def register_demand_gen_tools(mcp):
    """Register Demand Gen tools on the shared FastMCP instance."""

    @mcp.tool()
    def google_ads_create_demand_gen_campaign(
        customer_id: str,
        name: str,
        daily_budget: float,
        bidding_strategy: str = "MAXIMIZE_CONVERSIONS",
        target_cpa: Optional[float] = None,
        status: str = "PAUSED",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> str:
        """
        Create a Demand Gen campaign, with its own daily budget.

        Args:
            customer_id: Customer ID (without hyphens)
            name: Campaign name
            daily_budget: Daily budget in account currency, e.g. 50.00
            bidding_strategy: MAXIMIZE_CONVERSIONS, MAXIMIZE_CLICKS or TARGET_CPA
            target_cpa: Target CPA in account currency. Required for TARGET_CPA; an
                optional target for MAXIMIZE_CONVERSIONS.
            status: PAUSED (default) or ENABLED
            start_date: YYYY-MM-DD. Defaults to tomorrow.
            end_date: YYYY-MM-DD. Optional.

        Returns:
            Campaign ID and resource name.

        Locations, languages and audiences all go on the AD GROUP for Demand Gen, not
        here — a Demand Gen campaign rejects criteria at campaign level. Pass them to
        google_ads_create_demand_gen_ad_group.

        Created PAUSED by default. Enable it only once its ad group and ads exist and have
        been checked; an empty enabled campaign serves nothing but is live.
        """
        with performance_logger.track_operation(
            "create_demand_gen_campaign", customer_id=customer_id
        ):
            try:
                client = get_auth_manager().get_client()
                manager = DemandGenManager(client)

                result = manager.create_campaign(
                    customer_id=customer_id,
                    name=name,
                    daily_budget=daily_budget,
                    bidding_strategy=bidding_strategy,
                    target_cpa=target_cpa,
                    status=status,
                    start_date=start_date,
                    end_date=end_date,
                )

                audit_logger.log_api_call(
                    customer_id=customer_id,
                    operation="create_demand_gen_campaign",
                    resource_type="campaign",
                    resource_id=result["campaign_id"],
                    action="create",
                    details={"name": name, "status": result["status"]},
                )

                output = "# Demand Gen Campaign Created\n\n"
                output += f"- **Campaign ID**: {result['campaign_id']}\n"
                output += f"- **Resource name**: `{result['campaign_resource_name']}`\n"
                output += f"- **Name**: {result['name']}\n"
                output += f"- **Status**: {result['status']}\n"
                output += f"- **Bidding**: {result['bidding_strategy']}\n"
                output += f"- **Daily budget**: {result['daily_budget']}\n"
                output += f"- **Starts**: {result['start_date']}"
                output += f" — ends {result['end_date']}\n" if result["end_date"] else "\n"
                output += "\nNext: create an ad group with "
                output += "`google_ads_create_demand_gen_ad_group`.\n"
                return output

            except Exception as e:
                error_msg = ErrorHandler.handle_error(
                    e, context="create_demand_gen_campaign"
                )
                return f"❌ Failed to create Demand Gen campaign: {error_msg}"

    @mcp.tool()
    def google_ads_create_demand_gen_ad_group(
        customer_id: str,
        campaign_id: str,
        name: str,
        status: str = "PAUSED",
        audience_ids: Optional[Union[str, List[str]]] = None,
        location_ids: Optional[Union[str, List[str]]] = None,
        language_ids: Optional[Union[str, List[str]]] = None,
    ) -> str:
        """
        Create an ad group in a Demand Gen campaign.

        Args:
            customer_id: Customer ID (without hyphens)
            campaign_id: The Demand Gen campaign's ID
            name: Ad group name
            status: PAUSED (default) or ENABLED
            audience_ids: Comma-separated user list IDs to target. Get them from
                google_ads_list_user_lists.
            location_ids: Comma-separated geo target constant IDs, e.g. "2356" for India
            language_ids: Comma-separated language constant IDs, e.g. "1000" for English

        Returns:
            Ad group ID and resource name.

        Locations and languages are set HERE, not on the campaign: a Demand Gen campaign
        rejects criteria at campaign level, and the same criteria on the ad group are
        accepted.
        """
        with performance_logger.track_operation(
            "create_demand_gen_ad_group", customer_id=customer_id
        ):
            try:
                client = get_auth_manager().get_client()
                manager = DemandGenManager(client)

                result = manager.create_ad_group(
                    customer_id=customer_id,
                    campaign_id=campaign_id,
                    name=name,
                    status=status,
                    audience_ids=_ids(audience_ids),
                    location_ids=_ids(location_ids),
                    language_ids=_ids(language_ids),
                )

                audit_logger.log_api_call(
                    customer_id=customer_id,
                    operation="create_demand_gen_ad_group",
                    resource_type="ad_group",
                    resource_id=result["ad_group_id"],
                    action="create",
                    details={"campaign_id": str(campaign_id), "name": name},
                )

                output = "# Demand Gen Ad Group Created\n\n"
                output += f"- **Ad group ID**: {result['ad_group_id']}\n"
                output += f"- **Resource name**: `{result['ad_group_resource_name']}`\n"
                output += f"- **Name**: {result['name']}\n"
                output += f"- **Status**: {result['status']}\n"
                for label in ("locations", "languages", "audiences"):
                    if result.get(label):
                        output += f"- **{label.title()}**: {', '.join(result[label])}\n"
                output += "\nNext: upload images with `google_ads_upload_image_asset`, "
                output += "then create the ad with `google_ads_create_demand_gen_ad`.\n"
                return output

            except Exception as e:
                error_msg = ErrorHandler.handle_error(
                    e, context="create_demand_gen_ad_group"
                )
                return f"❌ Failed to create Demand Gen ad group: {error_msg}"

    @mcp.tool()
    def google_ads_create_demand_gen_ad(
        customer_id: str,
        ad_group_id: str,
        final_url: str,
        headlines: Union[str, List[str]],
        descriptions: Union[str, List[str]],
        business_name: str,
        logo_asset_resource_names: Union[str, List[str]],
        marketing_image_resource_names: Optional[Union[str, List[str]]] = None,
        square_image_resource_names: Optional[Union[str, List[str]]] = None,
        portrait_image_resource_names: Optional[Union[str, List[str]]] = None,
        call_to_action: Optional[str] = None,
        status: str = "PAUSED",
        ad_name: Optional[str] = None,
    ) -> str:
        """
        Create a Demand Gen multi-asset image ad.

        Args:
            customer_id: Customer ID (without hyphens)
            ad_group_id: The Demand Gen ad group's ID
            final_url: Landing page URL
            headlines: JSON array of up to 5 headlines, each at most 40 characters,
                e.g. '["One Agent, Full Funnel", "Book more meetings"]'. A plain
                string is taken as ONE headline — commas are not separators here,
                because ad copy contains them.
            descriptions: JSON array of up to 5 descriptions, each at most 90
                characters. Same rule: a plain string is one description.
            business_name: Advertiser name, at most 25 characters
            logo_asset_resource_names: Logo image asset resource names, as a JSON
                array or comma-separated
                (1:1, at least 128x128). At least one is required.
            marketing_image_resource_names: Landscape 1.91:1 image assets (JSON array or comma-separated)
            square_image_resource_names: Square 1:1 image assets (JSON array or comma-separated)
            portrait_image_resource_names: Portrait 4:5 image assets (JSON array or comma-separated)
            call_to_action: The button's DISPLAY TEXT, e.g. "Learn more", "Shop now",
                "Sign up", "Book now", "Get quote", "Subscribe", "Download", "Order now",
                "Contact us", "Apply now", "Visit site", "See more". Not the enum name —
                call_to_action_text is a text field, and "LEARN_MORE" is rejected as
                "Invalid call to action text".
            status: PAUSED (default) or ENABLED
            ad_name: Optional name for the ad

        Returns:
            The ad's resource name and what it was built from.

        At least one landscape or square marketing image is required, on top of the logo.
        Create the assets first with google_ads_upload_image_asset and pass the resource
        names it returns.

        business_name here is the text itself, not an asset resource name — the
        multi-asset ad carries the string directly. The text ASSET that
        google_ads_create_text_asset makes is for Performance Max asset groups.
        """
        with performance_logger.track_operation(
            "create_demand_gen_ad", customer_id=customer_id
        ):
            try:
                client = get_auth_manager().get_client()
                manager = DemandGenManager(client)

                result = manager.create_multi_asset_ad(
                    customer_id=customer_id,
                    ad_group_id=ad_group_id,
                    final_url=final_url,
                    headlines=_copy(headlines, "headlines") or [],
                    descriptions=_copy(descriptions, "descriptions") or [],
                    business_name=business_name,
                    logo_asset_resource_names=_ids(logo_asset_resource_names) or [],
                    marketing_image_resource_names=_ids(marketing_image_resource_names),
                    square_image_resource_names=_ids(square_image_resource_names),
                    portrait_image_resource_names=_ids(portrait_image_resource_names),
                    call_to_action=call_to_action,
                    status=status,
                    ad_name=ad_name,
                )

                audit_logger.log_api_call(
                    customer_id=customer_id,
                    operation="create_demand_gen_ad",
                    resource_type="ad_group_ad",
                    resource_id=result["ad_resource_name"],
                    action="create",
                    details={"ad_group_id": str(ad_group_id)},
                )

                counts = result["image_counts"]
                output = "# Demand Gen Ad Created\n\n"
                output += f"- **Resource name**: `{result['ad_resource_name']}`\n"
                output += f"- **Status**: {result['status']}\n"
                output += f"- **Business name**: {result['business_name']}\n"
                output += f"- **Final URL**: {result['final_url']}\n"
                if result["call_to_action"]:
                    output += f"- **CTA**: {result['call_to_action']}\n"
                output += f"- **Headlines**: {len(result['headlines'])}\n"
                output += f"- **Descriptions**: {len(result['descriptions'])}\n"
                output += (
                    f"- **Images**: {counts['logo']} logo, {counts['landscape']} landscape, "
                    f"{counts['square']} square, {counts['portrait']} portrait\n"
                )
                return output

            except Exception as e:
                error_msg = ErrorHandler.handle_error(e, context="create_demand_gen_ad")
                return f"❌ Failed to create Demand Gen ad: {error_msg}"

    logger.info("Demand Gen tools registered (3 tools)")

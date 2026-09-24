"""Demand Gen campaigns, ad groups and multi-asset image ads.

Three objects in a fixed order — campaign (with its own budget), ad group, ad — because
each names the one above it. The assets an ad points at have to exist first; see
managers/asset_manager.py.

Everything here creates PAUSED by default. These calls spend money the moment they are
enabled, the server holds one credential shared by every caller, and an ad that starts
serving because a tool defaulted to ENABLED is not a mistake anyone can take back.
"""

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

# What Demand Gen accepts. Named here rather than passed through so an unsupported
# strategy fails with a list of the real options instead of a Google error code.
#
# TARGET_SPEND and MAXIMIZE_CLICKS are the same strategy under two names — the Google Ads
# API calls the field target_spend, the UI calls it Maximize Clicks, and the Search tools
# in this codebase take the first while these took the second. Both are accepted
# everywhere so a caller moving between campaign types does not have to know which
# vocabulary a given tool was written against.
_ALIASES = {"TARGET_SPEND": "MAXIMIZE_CLICKS", "MAXIMISE_CLICKS": "MAXIMIZE_CLICKS",
            "MAXIMISE_CONVERSIONS": "MAXIMIZE_CONVERSIONS"}
BIDDING_STRATEGIES = ("MAXIMIZE_CONVERSIONS", "MAXIMIZE_CLICKS", "TARGET_CPA")


def _normalise_bidding(value: str) -> str:
    """The canonical name for a bidding strategy the caller asked for."""
    name = (value or "").strip().upper()
    return _ALIASES.get(name, name)


def _as_datetime(value: str, end_of_day: bool) -> str:
    """A YYYY-MM-DD date as the "yyyy-MM-dd HH:mm:ss" the API wants.

    Passed through unchanged if it already carries a time, so a caller who knows exactly
    when they want a campaign to start is not second-guessed.
    """
    value = (value or "").strip()
    if not value:
        raise ValueError("date is empty")
    if " " in value or "T" in value:
        return value.replace("T", " ")
    return f"{value} 23:59:59" if end_of_day else f"{value} 00:00:00"


class DemandGenManager:
    """Build Demand Gen campaigns and their image ads."""

    def __init__(self, client):
        self.client = client

    # ------------------------------------------------------------------
    # Campaign
    # ------------------------------------------------------------------

    def create_campaign(
        self,
        customer_id: str,
        name: str,
        daily_budget: float,
        bidding_strategy: str = "MAXIMIZE_CONVERSIONS",
        target_cpa: Optional[float] = None,
        status: str = "PAUSED",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a Demand Gen campaign and the budget it owns.

        Locations and languages are NOT set here. They are campaign criteria on Search and
        on most other channel types, but a Demand Gen campaign rejects them at campaign
        level — every location and language criterion comes back as an error code the v25
        library cannot even name ("The error code is not in this version", request_error:
        UNKNOWN, on operations.create.location). The same criteria applied to the ad group
        succeed. So they live on create_ad_group, along with audiences.
        """
        customer_id = str(customer_id).replace("-", "")
        strategy = _normalise_bidding(bidding_strategy)
        if strategy not in BIDDING_STRATEGIES:
            raise ValueError(
                f"bidding_strategy must be one of {', '.join(BIDDING_STRATEGIES)} "
                "(TARGET_SPEND is accepted as an alias for MAXIMIZE_CLICKS)"
            )
        if strategy == "TARGET_CPA" and not target_cpa:
            raise ValueError("target_cpa is required when bidding_strategy is TARGET_CPA")
        if daily_budget is None or daily_budget <= 0:
            raise ValueError("daily_budget must be greater than zero")

        # Budget first: a campaign cannot be created without one to point at.
        budget_service = self.client.get_service("CampaignBudgetService")
        budget_operation = self.client.get_type("CampaignBudgetOperation")
        budget = budget_operation.create
        budget.name = f"{name} budget"
        budget.amount_micros = int(round(daily_budget * 1_000_000))
        budget.delivery_method = self.client.enums.BudgetDeliveryMethodEnum.STANDARD
        # Not explicitly_shared: a budget made for one campaign should not silently become
        # a pool another campaign can be attached to later.
        budget.explicitly_shared = False
        budget_response = budget_service.mutate_campaign_budgets(
            customer_id=customer_id, operations=[budget_operation]
        )
        budget_resource_name = budget_response.results[0].resource_name

        campaign_service = self.client.get_service("CampaignService")
        operation = self.client.get_type("CampaignOperation")
        campaign = operation.create
        campaign.name = name
        campaign.advertising_channel_type = (
            self.client.enums.AdvertisingChannelTypeEnum.DEMAND_GEN
        )
        campaign.campaign_budget = budget_resource_name
        campaign.status = getattr(
            self.client.enums.CampaignStatusEnum, (status or "PAUSED").upper()
        )

        if strategy == "MAXIMIZE_CONVERSIONS":
            campaign.maximize_conversions.target_cpa_micros = (
                int(round(target_cpa * 1_000_000)) if target_cpa else 0
            )
        elif strategy == "MAXIMIZE_CLICKS":
            # Assignment, not CopyFrom: these are proto-plus messages and have no such
            # method — calling it raises "Unknown field for TargetSpend: CopyFrom" and
            # took Maximize Clicks down entirely. campaign_manager.py:267 had the right
            # form already. An empty TargetSpend selects the strategy with no CPC ceiling,
            # which is the right default for a campaign that starts paused.
            campaign.target_spend = self.client.get_type("TargetSpend")
        else:  # TARGET_CPA
            campaign.target_cpa.target_cpa_micros = int(round(target_cpa * 1_000_000))

        # start_date_time / end_date_time, not start_date / end_date: v25 renamed both and
        # the old names raise AttributeError rather than being ignored. Callers still pass
        # a plain YYYY-MM-DD, which is what anyone scheduling a campaign thinks in; the
        # time of day is filled in here — the whole of the first day, the whole of the last.
        campaign.start_date_time = _as_datetime(
            start_date or (date.today() + timedelta(days=1)).isoformat(), end_of_day=False
        )
        if end_date:
            campaign.end_date_time = _as_datetime(end_date, end_of_day=True)

        # Required on every campaign create since v18 — without it the API rejects the
        # whole request with "required field was not present". campaign_manager.py:215
        # has carried this since Search campaigns were written; these did not, which is
        # why no Demand Gen campaign could be created at all.
        campaign.contains_eu_political_advertising = (
            self.client.enums.EuPoliticalAdvertisingStatusEnum
            .DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        )

        response = campaign_service.mutate_campaigns(
            customer_id=customer_id, operations=[operation]
        )
        campaign_resource_name = response.results[0].resource_name
        campaign_id = campaign_resource_name.rsplit("/", 1)[-1]
        logger.info(f"Created Demand Gen campaign {campaign_resource_name}")

        return {
            "campaign_id": campaign_id,
            "campaign_resource_name": campaign_resource_name,
            "budget_resource_name": budget_resource_name,
            "name": name,
            "status": (status or "PAUSED").upper(),
            "bidding_strategy": strategy,
            "daily_budget": daily_budget,
            "start_date": campaign.start_date_time,
            "end_date": end_date,
        }

    def _add_ad_group_criteria(
        self,
        customer_id: str,
        ad_group_resource_name: str,
        location_ids: Optional[List[str]],
        language_ids: Optional[List[str]],
        audience_ids: Optional[List[str]],
    ) -> Dict[str, Any]:
        """Attach location, language and audience targeting to a Demand Gen ad group."""
        if not any([location_ids, language_ids, audience_ids]):
            return {"locations": [], "languages": [], "audiences": []}

        service = self.client.get_service("AdGroupCriterionService")
        user_list_service = self.client.get_service("UserListService")
        operations = []

        def _new():
            operation = self.client.get_type("AdGroupCriterionOperation")
            operation.create.ad_group = ad_group_resource_name
            return operation

        for location_id in location_ids or []:
            operation = _new()
            operation.create.location.geo_target_constant = (
                f"geoTargetConstants/{location_id}"
            )
            operations.append(operation)
        for language_id in language_ids or []:
            operation = _new()
            operation.create.language.language_constant = (
                f"languageConstants/{language_id}"
            )
            operations.append(operation)
        for audience_id in audience_ids or []:
            operation = _new()
            operation.create.user_list.user_list = user_list_service.user_list_path(
                customer_id, audience_id
            )
            operations.append(operation)

        service.mutate_ad_group_criteria(customer_id=customer_id, operations=operations)
        return {
            "locations": [str(x) for x in (location_ids or [])],
            "languages": [str(x) for x in (language_ids or [])],
            "audiences": [str(x) for x in (audience_ids or [])],
        }

    # ------------------------------------------------------------------
    # Ad group
    # ------------------------------------------------------------------

    def create_ad_group(
        self,
        customer_id: str,
        campaign_id: str,
        name: str,
        status: str = "PAUSED",
        audience_ids: Optional[List[str]] = None,
        location_ids: Optional[List[str]] = None,
        language_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Create a Demand Gen ad group and its targeting.

        Locations and languages are here rather than on the campaign because that is where
        a Demand Gen campaign accepts them — see create_campaign. audience_ids are user
        list ids, as google_ads_list_user_lists reports them.
        """
        customer_id = str(customer_id).replace("-", "")
        ad_group_service = self.client.get_service("AdGroupService")
        operation = self.client.get_type("AdGroupOperation")
        ad_group = operation.create
        ad_group.name = name
        ad_group.campaign = self.client.get_service("CampaignService").campaign_path(
            customer_id, campaign_id
        )
        ad_group.status = getattr(
            self.client.enums.AdGroupStatusEnum, (status or "PAUSED").upper()
        )

        response = ad_group_service.mutate_ad_groups(
            customer_id=customer_id, operations=[operation]
        )
        ad_group_resource_name = response.results[0].resource_name
        ad_group_id = ad_group_resource_name.rsplit("/", 1)[-1]
        logger.info(f"Created Demand Gen ad group {ad_group_resource_name}")

        targeting = self._add_ad_group_criteria(
            customer_id, ad_group_resource_name, location_ids, language_ids, audience_ids
        )

        return {
            "ad_group_id": ad_group_id,
            "ad_group_resource_name": ad_group_resource_name,
            "campaign_id": str(campaign_id),
            "name": name,
            "status": (status or "PAUSED").upper(),
            **targeting,
        }

    # ------------------------------------------------------------------
    # Ad
    # ------------------------------------------------------------------

    def create_multi_asset_ad(
        self,
        customer_id: str,
        ad_group_id: str,
        final_url: str,
        headlines: List[str],
        descriptions: List[str],
        business_name: str,
        logo_asset_resource_names: List[str],
        marketing_image_resource_names: Optional[List[str]] = None,
        square_image_resource_names: Optional[List[str]] = None,
        portrait_image_resource_names: Optional[List[str]] = None,
        call_to_action: Optional[str] = None,
        status: str = "PAUSED",
        ad_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a Demand Gen multi-asset image ad.

        business_name is a plain string here, not an asset reference — the multi-asset ad
        carries the text directly. The BUSINESS_NAME *asset* that create_text_asset makes
        is for Performance Max asset groups, which do take an asset. Same words, two
        different shapes, and passing one where the other belongs is a confusing failure.

        Limits are Google's and are checked here so an over-long headline is named by the
        field it came from rather than rejected as a policy error several steps later.
        """
        customer_id = str(customer_id).replace("-", "")
        headlines = [h.strip() for h in (headlines or []) if h and h.strip()]
        descriptions = [d.strip() for d in (descriptions or []) if d and d.strip()]

        if not headlines:
            raise ValueError("at least one headline is required")
        if not descriptions:
            raise ValueError("at least one description is required")
        if not business_name or not business_name.strip():
            raise ValueError("business_name is required")
        if not logo_asset_resource_names:
            raise ValueError("at least one logo image asset is required")
        if len(business_name.strip()) > 25:
            raise ValueError(
                f"business_name is {len(business_name.strip())} characters; the limit is 25"
            )
        for headline in headlines:
            if len(headline) > 40:
                raise ValueError(f"headline over 40 characters: {headline!r}")
        for description in descriptions:
            if len(description) > 90:
                raise ValueError(f"description over 90 characters: {description!r}")

        marketing_images = marketing_image_resource_names or []
        square_images = square_image_resource_names or []
        if not marketing_images and not square_images:
            raise ValueError(
                "at least one marketing image is required — landscape (1.91:1) or square (1:1)"
            )

        ad_service = self.client.get_service("AdGroupAdService")
        operation = self.client.get_type("AdGroupAdOperation")
        ad_group_ad = operation.create
        ad_group_ad.ad_group = self.client.get_service("AdGroupService").ad_group_path(
            customer_id, ad_group_id
        )
        ad_group_ad.status = getattr(
            self.client.enums.AdGroupAdStatusEnum, (status or "PAUSED").upper()
        )

        ad = ad_group_ad.ad
        ad.final_urls.append(final_url)
        if ad_name:
            ad.name = ad_name

        info = ad.demand_gen_multi_asset_ad
        info.business_name = business_name.strip()
        if call_to_action:
            info.call_to_action_text = call_to_action

        for headline in headlines[:5]:
            text_asset = self.client.get_type("AdTextAsset")
            text_asset.text = headline
            info.headlines.append(text_asset)
        for description in descriptions[:5]:
            text_asset = self.client.get_type("AdTextAsset")
            text_asset.text = description
            info.descriptions.append(text_asset)

        for resource_name in logo_asset_resource_names:
            image = self.client.get_type("AdImageAsset")
            image.asset = resource_name
            info.logo_images.append(image)
        for resource_name in marketing_images:
            image = self.client.get_type("AdImageAsset")
            image.asset = resource_name
            info.marketing_images.append(image)
        for resource_name in square_images:
            image = self.client.get_type("AdImageAsset")
            image.asset = resource_name
            info.square_marketing_images.append(image)
        for resource_name in portrait_image_resource_names or []:
            image = self.client.get_type("AdImageAsset")
            image.asset = resource_name
            info.portrait_marketing_images.append(image)

        response = ad_service.mutate_ad_group_ads(
            customer_id=customer_id, operations=[operation]
        )
        resource_name = response.results[0].resource_name
        logger.info(f"Created Demand Gen multi-asset ad {resource_name}")

        return {
            "ad_resource_name": resource_name,
            "ad_group_id": str(ad_group_id),
            "status": (status or "PAUSED").upper(),
            "headlines": headlines[:5],
            "descriptions": descriptions[:5],
            "business_name": business_name.strip(),
            "call_to_action": call_to_action,
            "final_url": final_url,
            "image_counts": {
                "logo": len(logo_asset_resource_names),
                "landscape": len(marketing_images),
                "square": len(square_images),
                "portrait": len(portrait_image_resource_names or []),
            },
        }

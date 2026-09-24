"""
Shopping & Performance Max Manager

Handles Shopping campaigns and Performance Max campaigns.

Shopping Campaigns:
- Product shopping campaigns
- Product partition management (product groups)
- Shopping feed status monitoring
- Shopping-specific performance metrics

Performance Max Campaigns:
- Performance Max campaign creation
- Asset group management
- Asset uploads (images, videos, text)
- Audience signals configuration
- Performance Max insights and reporting
"""

from typing import Dict, Any, List, Optional
from google.ads.googleads.client import GoogleAdsClient
from dataclasses import dataclass
from enum import Enum


class ShoppingCampaignPriority(str, Enum):
    """Shopping campaign priority levels."""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class AssetType(str, Enum):
    """Asset types for Performance Max."""
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    TEXT = "TEXT"
    HEADLINE = "HEADLINE"
    DESCRIPTION = "DESCRIPTION"


@dataclass
class ShoppingCampaignConfig:
    """Configuration for shopping campaign creation."""
    name: str
    merchant_center_id: str
    budget_amount: float
    priority: ShoppingCampaignPriority = ShoppingCampaignPriority.LOW
    target_roas: Optional[float] = None
    enable_local: bool = False


@dataclass
class PerformanceMaxCampaignConfig:
    """Configuration for Performance Max campaign creation."""
    name: str
    budget_amount: float
    # Conversion action IDs or resource names, NOT display names. An account may hold
    # several conversion actions sharing a name — Revspot Autospot has four called
    # "Submit lead form" — so a name does not identify one. This field was previously
    # typed as names and, worse, never read at all: the tool required it, the manager
    # ignored it, and the campaign optimised for the account default while the caller
    # believed otherwise.
    conversion_action_ids: Optional[List[str]] = None
    target_roas: Optional[float] = None
    target_cpa: Optional[float] = None
    status: str = "PAUSED"
    location_ids: Optional[List[str]] = None
    language_ids: Optional[List[str]] = None
    # Final URL expansion sends traffic to pages Google picks rather than the one given.
    # On by default in PMax, so opting out has to be possible for approved landing pages.
    opt_out_final_url_expansion: bool = False


class ShoppingPMaxManager:
    """Manager for Shopping and Performance Max campaigns."""

    def __init__(self, client: GoogleAdsClient):
        """Initialize the shopping/PMax manager.

        Args:
            client: Authenticated GoogleAdsClient instance
        """
        self.client = client

    def create_shopping_campaign(
        self,
        customer_id: str,
        config: ShoppingCampaignConfig
    ) -> Dict[str, Any]:
        """Create a Shopping campaign.

        Args:
            customer_id: Customer ID (without hyphens)
            config: Shopping campaign configuration

        Returns:
            Created campaign details
        """
        campaign_service = self.client.get_service("CampaignService")
        campaign_budget_service = self.client.get_service("CampaignBudgetService")

        # Create campaign budget
        budget_operation = self.client.get_type("CampaignBudgetOperation")
        budget = budget_operation.create
        budget.name = f"{config.name} Budget"
        budget.amount_micros = int(config.budget_amount * 1_000_000)
        budget.delivery_method = self.client.enums.BudgetDeliveryMethodEnum.STANDARD

        budget_response = campaign_budget_service.mutate_campaign_budgets(
            customer_id=customer_id,
            operations=[budget_operation]
        )
        budget_resource_name = budget_response.results[0].resource_name

        # Create shopping campaign
        campaign_operation = self.client.get_type("CampaignOperation")
        campaign = campaign_operation.create

        campaign.name = config.name
        campaign.advertising_channel_type = self.client.enums.AdvertisingChannelTypeEnum.SHOPPING
        campaign.status = self.client.enums.CampaignStatusEnum.PAUSED
        campaign.campaign_budget = budget_resource_name

        # Shopping settings
        campaign.shopping_setting.merchant_id = int(config.merchant_center_id)
        campaign.shopping_setting.sales_country = "US"
        campaign.shopping_setting.campaign_priority = {
            "LOW": 0,
            "MEDIUM": 1,
            "HIGH": 2
        }[config.priority.value]
        campaign.shopping_setting.enable_local = config.enable_local

        # Bidding strategy
        if config.target_roas:
            campaign.target_roas.target_roas = config.target_roas
        else:
            campaign.maximize_conversion_value.target_roas = 0

        # Create campaign
        response = campaign_service.mutate_campaigns(
            customer_id=customer_id,
            operations=[campaign_operation]
        )

        campaign_id = response.results[0].resource_name.split('/')[-1]

        return {
            'campaign_id': campaign_id,
            'campaign_name': config.name,
            'resource_name': response.results[0].resource_name,
            'merchant_center_id': config.merchant_center_id,
            'priority': config.priority.value,
            'budget': config.budget_amount
        }

    def create_product_group(
        self,
        customer_id: str,
        ad_group_id: str,
        product_condition: Optional[str] = None,
        product_type: Optional[str] = None,
        is_subdivision: bool = False
    ) -> Dict[str, Any]:
        """Create a product group (product partition) in a shopping ad group.

        Args:
            customer_id: Customer ID (without hyphens)
            ad_group_id: Shopping ad group ID
            product_condition: Product condition filter (NEW, USED, REFURBISHED)
            product_type: Product type filter
            is_subdivision: Whether this is a subdivision or unit

        Returns:
            Created product group details
        """
        ad_group_criterion_service = self.client.get_service("AdGroupCriterionService")

        criterion_operation = self.client.get_type("AdGroupCriterionOperation")
        criterion = criterion_operation.create

        criterion.ad_group = self.client.get_service("AdGroupService").ad_group_path(
            customer_id, ad_group_id
        )
        criterion.status = self.client.enums.AdGroupCriterionStatusEnum.ENABLED

        # Configure listing group
        criterion.listing_group.type_ = (
            self.client.enums.ListingGroupTypeEnum.SUBDIVISION if is_subdivision
            else self.client.enums.ListingGroupTypeEnum.UNIT
        )

        # Apply product dimensions
        if product_condition:
            dimension = self.client.get_type("ListingDimensionInfo")
            dimension.product_condition.condition = self.client.enums.ProductConditionEnum[product_condition]
            self.client.copy_from(criterion.listing_group.case_value.product_condition, dimension.product_condition)

        if product_type:
            dimension = self.client.get_type("ListingDimensionInfo")
            dimension.product_type.value = product_type
            self.client.copy_from(criterion.listing_group.case_value.product_type, dimension.product_type)

        # Set CPC bid for units (not subdivisions)
        if not is_subdivision:
            criterion.cpc_bid_micros = 1_000_000  # $1.00 default

        response = ad_group_criterion_service.mutate_ad_group_criteria(
            customer_id=customer_id,
            operations=[criterion_operation]
        )

        return {
            'resource_name': response.results[0].resource_name,
            'criterion_id': response.results[0].resource_name.split('~')[-1],
            'ad_group_id': ad_group_id,
            'type': 'subdivision' if is_subdivision else 'unit'
        }

    def get_shopping_feed_status(
        self,
        customer_id: str,
        merchant_center_id: str
    ) -> Dict[str, Any]:
        """Check Merchant Center feed status.

        Args:
            customer_id: Customer ID (without hyphens)
            merchant_center_id: Merchant Center ID

        Returns:
            Feed status information
        """
        ga_service = self.client.get_service("GoogleAdsService")

        query = f"""
            SELECT
                merchant_center_link.id,
                merchant_center_link.merchant_center_id,
                merchant_center_link.status
            FROM merchant_center_link
            WHERE merchant_center_link.merchant_center_id = {merchant_center_id}
        """

        response = ga_service.search(customer_id=customer_id, query=query)
        results = list(response)

        if not results:
            return {
                'status': 'NOT_LINKED',
                'message': 'Merchant Center account not linked',
                'merchant_center_id': merchant_center_id
            }

        link = results[0].merchant_center_link

        return {
            'status': link.status.name,
            'merchant_center_id': str(link.merchant_center_id),
            'link_id': str(link.id),
            'message': f'Merchant Center account is {link.status.name}'
        }

    def get_shopping_performance(
        self,
        customer_id: str,
        campaign_id: Optional[str] = None,
        date_range: str = "LAST_30_DAYS"
    ) -> Dict[str, Any]:
        """Get Shopping campaign performance metrics.

        Args:
            customer_id: Customer ID (without hyphens)
            campaign_id: Optional campaign ID filter
            date_range: Date range for metrics

        Returns:
            Shopping performance data
        """
        ga_service = self.client.get_service("GoogleAdsService")

        query = f"""
            SELECT
                campaign.id,
                campaign.name,
                metrics.impressions,
                metrics.clicks,
                metrics.ctr,
                metrics.cost_micros,
                metrics.conversions,
                metrics.conversions_value,
                metrics.cost_per_conversion,
                shopping_performance_view.click_type
            FROM shopping_performance_view
            WHERE segments.date DURING {date_range}
        """

        if campaign_id:
            query += f" AND campaign.id = {campaign_id}"

        query += " ORDER BY metrics.impressions DESC"

        response = ga_service.search(customer_id=customer_id, query=query)

        campaigns = []
        for row in response:
            campaigns.append({
                'campaign_id': str(row.campaign.id),
                'campaign_name': row.campaign.name,
                'impressions': row.metrics.impressions,
                'clicks': row.metrics.clicks,
                'ctr': row.metrics.ctr,
                'cost': row.metrics.cost_micros / 1_000_000,
                'conversions': row.metrics.conversions,
                'conversion_value': row.metrics.conversions_value,
                'cost_per_conversion': row.metrics.cost_per_conversion,
                'roas': (row.metrics.conversions_value / (row.metrics.cost_micros / 1_000_000))
                        if row.metrics.cost_micros > 0 else 0
            })

        return {
            'campaigns': campaigns,
            'total_campaigns': len(campaigns)
        }

    def create_performance_max_campaign(
        self,
        customer_id: str,
        config: PerformanceMaxCampaignConfig
    ) -> Dict[str, Any]:
        """Create a Performance Max campaign.

        Args:
            customer_id: Customer ID (without hyphens)
            config: Performance Max campaign configuration

        Returns:
            Created campaign details
        """
        campaign_service = self.client.get_service("CampaignService")
        campaign_budget_service = self.client.get_service("CampaignBudgetService")

        # Create campaign budget
        budget_operation = self.client.get_type("CampaignBudgetOperation")
        budget = budget_operation.create
        budget.name = f"{config.name} Budget"
        budget.amount_micros = int(config.budget_amount * 1_000_000)
        budget.delivery_method = self.client.enums.BudgetDeliveryMethodEnum.STANDARD

        budget_response = campaign_budget_service.mutate_campaign_budgets(
            customer_id=customer_id,
            operations=[budget_operation]
        )
        budget_resource_name = budget_response.results[0].resource_name

        # Create Performance Max campaign
        campaign_operation = self.client.get_type("CampaignOperation")
        campaign = campaign_operation.create

        campaign.name = config.name
        campaign.advertising_channel_type = self.client.enums.AdvertisingChannelTypeEnum.PERFORMANCE_MAX
        campaign.status = getattr(
            self.client.enums.CampaignStatusEnum, (config.status or "PAUSED").upper()
        )
        campaign.campaign_budget = budget_resource_name

        # Bidding strategy
        if config.target_roas:
            campaign.maximize_conversion_value.target_roas = config.target_roas
        elif config.target_cpa:
            campaign.maximize_conversions.target_cpa_micros = int(config.target_cpa * 1_000_000)
        else:
            campaign.maximize_conversions.target_cpa_micros = 0

        # Required on every campaign create since v18; missing here while Search campaigns
        # have carried it since they were written (campaign_manager.py:215).
        campaign.contains_eu_political_advertising = (
            self.client.enums.EuPoliticalAdvertisingStatusEnum
            .DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        )

        # Optimise for the conversion actions the caller named, by ID or resource name.
        if config.conversion_action_ids:
            conversion_action_service = self.client.get_service("ConversionActionService")
            for identifier in config.conversion_action_ids:
                identifier = str(identifier).strip()
                resource_name = (
                    identifier
                    if identifier.startswith("customers/")
                    else conversion_action_service.conversion_action_path(
                        customer_id, identifier
                    )
                )
                campaign.selective_optimization.conversion_actions.append(resource_name)

        # url_expansion_opt_out no longer exists on Campaign — in v25 the control is an
        # asset automation setting, so opting out means opting OUT of the automation.
        if config.opt_out_final_url_expansion:
            setting = self.client.get_type("AssetAutomationSetting")
            setting.asset_automation_type = (
                self.client.enums.AssetAutomationTypeEnum
                .FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION
            )
            setting.asset_automation_status = (
                self.client.enums.AssetAutomationStatusEnum.OPTED_OUT
            )
            campaign.asset_automation_settings.append(setting)

        # Create campaign
        response = campaign_service.mutate_campaigns(
            customer_id=customer_id,
            operations=[campaign_operation]
        )

        campaign_resource_name = response.results[0].resource_name
        campaign_id = campaign_resource_name.split('/')[-1]

        # Locations and languages are campaign criteria, added after the campaign exists.
        criteria = self._add_campaign_criteria(
            customer_id, campaign_resource_name, config.location_ids, config.language_ids
        )

        return {
            'campaign_id': campaign_id,
            'campaign_name': config.name,
            'resource_name': campaign_resource_name,
            'budget': config.budget_amount,
            'status': (config.status or 'PAUSED').upper(),
            'bidding_strategy': 'TARGET_ROAS' if config.target_roas else 'MAXIMIZE_CONVERSIONS',
            'conversion_actions': list(config.conversion_action_ids or []),
            'final_url_expansion': not config.opt_out_final_url_expansion,
            **criteria,
        }

    def _add_campaign_criteria(
        self,
        customer_id: str,
        campaign_resource_name: str,
        location_ids: Optional[List[str]],
        language_ids: Optional[List[str]]
    ) -> Dict[str, Any]:
        """Attach location and language targeting to a campaign."""
        if not location_ids and not language_ids:
            return {'locations': [], 'languages': []}

        service = self.client.get_service("CampaignCriterionService")
        operations = []
        for location_id in location_ids or []:
            operation = self.client.get_type("CampaignCriterionOperation")
            criterion = operation.create
            criterion.campaign = campaign_resource_name
            criterion.location.geo_target_constant = f"geoTargetConstants/{location_id}"
            operations.append(operation)
        for language_id in language_ids or []:
            operation = self.client.get_type("CampaignCriterionOperation")
            criterion = operation.create
            criterion.campaign = campaign_resource_name
            criterion.language.language_constant = f"languageConstants/{language_id}"
            operations.append(operation)

        service.mutate_campaign_criteria(customer_id=customer_id, operations=operations)
        return {
            'locations': list(location_ids or []),
            'languages': list(language_ids or []),
        }

    def create_asset_group(
        self,
        customer_id: str,
        campaign_id: str,
        asset_group_name: str,
        final_urls: List[str],
        headlines: Optional[List[str]] = None,
        descriptions: Optional[List[str]] = None,
        long_headline: Optional[str] = None,
        existing_assets: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, Any]:
        """Create an asset group for Performance Max campaign with text assets.

        PMax requires text assets at creation time. This method creates the
        asset group and all text assets atomically in a single mutate request.

        Args:
            customer_id: Customer ID (without hyphens)
            campaign_id: Performance Max campaign ID
            asset_group_name: Name for the asset group
            final_urls: List of final URLs
            headlines: List of headlines (3-15, max 30 chars each)
            descriptions: List of descriptions (2-5, max 90 chars each)
            long_headline: Single long headline (max 90 chars)
            existing_assets: List of dicts with 'resource_name' and 'field_type'
                           to link existing assets (e.g. images) to the new group

        Returns:
            Created asset group details
        """
        googleads_service = self.client.get_service("GoogleAdsService")

        mutate_operations = []

        # Temp resource name for the asset group
        asset_group_temp_id = "-1"
        asset_group_resource_name = googleads_service.asset_group_path(
            customer_id, asset_group_temp_id
        )

        # 1. Create asset group operation
        asset_group_op = self.client.get_type("MutateOperation")
        asset_group = asset_group_op.asset_group_operation.create
        asset_group.name = asset_group_name
        asset_group.campaign = self.client.get_service("CampaignService").campaign_path(
            customer_id, campaign_id
        )
        asset_group.status = self.client.enums.AssetGroupStatusEnum.PAUSED
        asset_group.final_urls.extend(final_urls)
        asset_group.resource_name = asset_group_resource_name
        mutate_operations.append(asset_group_op)

        # IMPORTANT: Google Ads API requires all AssetOperations BEFORE all
        # AssetGroupAssetOperations. Interleaving causes premature validation.
        # See: https://developers.google.com/google-ads/api/performance-max/assets
        asset_create_operations = []
        asset_link_operations = []

        next_temp_id = -2
        created_assets = []

        if headlines:
            for headline in headlines[:15]:
                temp_path = googleads_service.asset_path(customer_id, str(next_temp_id))

                # Create asset
                asset_op = self.client.get_type("MutateOperation")
                asset = asset_op.asset_operation.create
                asset.text_asset.text = headline
                asset.resource_name = temp_path
                asset_create_operations.append(asset_op)

                # Link asset to asset group (added later)
                link_op = self.client.get_type("MutateOperation")
                link = link_op.asset_group_asset_operation.create
                link.asset = temp_path
                link.asset_group = asset_group_resource_name
                link.field_type = self.client.enums.AssetFieldTypeEnum.HEADLINE
                asset_link_operations.append(link_op)

                created_assets.append({'type': 'HEADLINE', 'text': headline})
                next_temp_id -= 1

        if long_headline:
            temp_path = googleads_service.asset_path(customer_id, str(next_temp_id))

            asset_op = self.client.get_type("MutateOperation")
            asset = asset_op.asset_operation.create
            asset.text_asset.text = long_headline
            asset.resource_name = temp_path
            asset_create_operations.append(asset_op)

            link_op = self.client.get_type("MutateOperation")
            link = link_op.asset_group_asset_operation.create
            link.asset = temp_path
            link.asset_group = asset_group_resource_name
            link.field_type = self.client.enums.AssetFieldTypeEnum.LONG_HEADLINE
            asset_link_operations.append(link_op)

            created_assets.append({'type': 'LONG_HEADLINE', 'text': long_headline})
            next_temp_id -= 1

        if descriptions:
            for description in descriptions[:5]:
                temp_path = googleads_service.asset_path(customer_id, str(next_temp_id))

                asset_op = self.client.get_type("MutateOperation")
                asset = asset_op.asset_operation.create
                asset.text_asset.text = description
                asset.resource_name = temp_path
                asset_create_operations.append(asset_op)

                link_op = self.client.get_type("MutateOperation")
                link = link_op.asset_group_asset_operation.create
                link.asset = temp_path
                link.asset_group = asset_group_resource_name
                link.field_type = self.client.enums.AssetFieldTypeEnum.DESCRIPTION
                asset_link_operations.append(link_op)

                created_assets.append({'type': 'DESCRIPTION', 'text': description})
                next_temp_id -= 1

        # Link existing assets (e.g. images, logos, videos) - these go in link ops
        if existing_assets:
            for existing in existing_assets:
                link_op = self.client.get_type("MutateOperation")
                link = link_op.asset_group_asset_operation.create
                link.asset = existing['resource_name']
                link.asset_group = asset_group_resource_name
                link.field_type = self.client.enums.AssetFieldTypeEnum[existing['field_type']]
                asset_link_operations.append(link_op)
                created_assets.append({
                    'type': existing['field_type'],
                    'text': f"[existing: {existing['resource_name'].split('/')[-1]}]"
                })

        # Order: 1) asset group, 2) all asset creates, 3) all asset links
        mutate_operations.extend(asset_create_operations)
        mutate_operations.extend(asset_link_operations)

        # Execute all operations atomically
        response = googleads_service.mutate(
            customer_id=customer_id,
            mutate_operations=mutate_operations
        )

        # Extract the real asset group ID from the first result
        asset_group_result = response.mutate_operation_responses[0]
        real_resource_name = asset_group_result.asset_group_result.resource_name
        asset_group_id = real_resource_name.split('/')[-1]

        return {
            'asset_group_id': asset_group_id,
            'asset_group_name': asset_group_name,
            'campaign_id': campaign_id,
            'resource_name': real_resource_name,
            'final_urls': final_urls,
            'assets_created': len(created_assets),
            'assets': created_assets
        }

    def upload_pmax_text_asset(
        self,
        customer_id: str,
        asset_group_id: str,
        headlines: Optional[List[str]] = None,
        descriptions: Optional[List[str]] = None,
        long_headlines: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Upload text assets to a Performance Max asset group.

        Args:
            customer_id: Customer ID (without hyphens)
            asset_group_id: Asset group ID
            headlines: List of headlines (max 30 chars each)
            descriptions: List of descriptions (max 90 chars each)
            long_headlines: List of long headlines (max 90 chars each)

        Returns:
            Upload result
        """
        asset_service = self.client.get_service("AssetService")
        asset_group_asset_service = self.client.get_service("AssetGroupAssetService")

        operations = []
        created_assets = []

        # Create headline assets
        if headlines:
            for headline in headlines[:15]:
                asset_operation = self.client.get_type("AssetOperation")
                asset = asset_operation.create
                asset.text_asset.text = headline
                asset.type_ = self.client.enums.AssetTypeEnum.TEXT

                asset_response = asset_service.mutate_assets(
                    customer_id=customer_id,
                    operations=[asset_operation]
                )
                asset_resource_name = asset_response.results[0].resource_name

                aga_operation = self.client.get_type("AssetGroupAssetOperation")
                aga = aga_operation.create
                aga.asset = asset_resource_name
                aga.asset_group = self.client.get_service("AssetGroupService").asset_group_path(
                    customer_id, asset_group_id
                )
                aga.field_type = self.client.enums.AssetFieldTypeEnum.HEADLINE

                operations.append(aga_operation)
                created_assets.append({'type': 'HEADLINE', 'text': headline})

        # Create description assets
        if descriptions:
            for description in descriptions[:5]:
                asset_operation = self.client.get_type("AssetOperation")
                asset = asset_operation.create
                asset.text_asset.text = description
                asset.type_ = self.client.enums.AssetTypeEnum.TEXT

                asset_response = asset_service.mutate_assets(
                    customer_id=customer_id,
                    operations=[asset_operation]
                )
                asset_resource_name = asset_response.results[0].resource_name

                aga_operation = self.client.get_type("AssetGroupAssetOperation")
                aga = aga_operation.create
                aga.asset = asset_resource_name
                aga.asset_group = self.client.get_service("AssetGroupService").asset_group_path(
                    customer_id, asset_group_id
                )
                aga.field_type = self.client.enums.AssetFieldTypeEnum.DESCRIPTION

                operations.append(aga_operation)
                created_assets.append({'type': 'DESCRIPTION', 'text': description})

        # Create long headline assets
        if long_headlines:
            for long_headline in long_headlines[:5]:
                asset_operation = self.client.get_type("AssetOperation")
                asset = asset_operation.create
                asset.text_asset.text = long_headline
                asset.type_ = self.client.enums.AssetTypeEnum.TEXT

                asset_response = asset_service.mutate_assets(
                    customer_id=customer_id,
                    operations=[asset_operation]
                )
                asset_resource_name = asset_response.results[0].resource_name

                aga_operation = self.client.get_type("AssetGroupAssetOperation")
                aga = aga_operation.create
                aga.asset = asset_resource_name
                aga.asset_group = self.client.get_service("AssetGroupService").asset_group_path(
                    customer_id, asset_group_id
                )
                aga.field_type = self.client.enums.AssetFieldTypeEnum.LONG_HEADLINE

                operations.append(aga_operation)
                created_assets.append({'type': 'LONG_HEADLINE', 'text': long_headline})

        if not operations:
            return {
                'asset_group_id': asset_group_id,
                'total_assets': 0,
                'headlines': 0,
                'descriptions': 0,
                'long_headlines': 0,
                'assets': []
            }

        # Link all assets to asset group
        response = asset_group_asset_service.mutate_asset_group_assets(
            customer_id=customer_id,
            operations=operations
        )

        return {
            'asset_group_id': asset_group_id,
            'total_assets': len(created_assets),
            'headlines': len(headlines) if headlines else 0,
            'descriptions': len(descriptions) if descriptions else 0,
            'long_headlines': len(long_headlines) if long_headlines else 0,
            'assets': created_assets
        }

    def set_audience_signals(
        self,
        customer_id: str,
        asset_group_id: str,
        audience_segments: List[str]
    ) -> Dict[str, Any]:
        """Configure audience signals for a Performance Max asset group.

        Args:
            customer_id: Customer ID (without hyphens)
            asset_group_id: Asset group ID
            audience_segments: List of audience segment resource names

        Returns:
            Configuration result
        """
        asset_group_signal_service = self.client.get_service("AssetGroupSignalService")

        operations = []

        for segment_resource_name in audience_segments:
            signal_operation = self.client.get_type("AssetGroupSignalOperation")
            signal = signal_operation.create

            signal.asset_group = self.client.get_service("AssetGroupService").asset_group_path(
                customer_id, asset_group_id
            )
            signal.audience.audience = segment_resource_name

            operations.append(signal_operation)

        response = asset_group_signal_service.mutate_asset_group_signals(
            customer_id=customer_id,
            operations=operations
        )

        return {
            'asset_group_id': asset_group_id,
            'audience_signals_added': len(audience_segments),
            'resource_names': [r.resource_name for r in response.results]
        }

    def add_search_themes(
        self,
        customer_id: str,
        asset_group_id: str,
        search_themes: List[str]
    ) -> Dict[str, Any]:
        """Add search themes to a Performance Max asset group.

        Search themes tell Google's AI what topics/keywords are relevant
        for this asset group. Up to 25 per asset group.

        Args:
            customer_id: Customer ID (without hyphens)
            asset_group_id: Asset group ID
            search_themes: List of search theme strings (max 25)

        Returns:
            Result with added search themes
        """
        asset_group_signal_service = self.client.get_service("AssetGroupSignalService")
        asset_group_service = self.client.get_service("AssetGroupService")

        operations = []

        for theme_text in search_themes[:25]:
            signal_operation = self.client.get_type("AssetGroupSignalOperation")
            signal = signal_operation.create

            signal.asset_group = asset_group_service.asset_group_path(
                customer_id, asset_group_id
            )
            signal.search_theme.text = theme_text

            operations.append(signal_operation)

        response = asset_group_signal_service.mutate_asset_group_signals(
            customer_id=customer_id,
            operations=operations
        )

        return {
            'asset_group_id': asset_group_id,
            'search_themes_added': len(search_themes[:25]),
            'themes': search_themes[:25],
            'resource_names': [r.resource_name for r in response.results]
        }

    def get_pmax_insights(
        self,
        customer_id: str,
        campaign_id: str,
        date_range: str = "LAST_30_DAYS"
    ) -> Dict[str, Any]:
        """Get Performance Max campaign insights.

        Args:
            customer_id: Customer ID (without hyphens)
            campaign_id: Performance Max campaign ID
            date_range: Date range for metrics

        Returns:
            Performance Max insights
        """
        ga_service = self.client.get_service("GoogleAdsService")

        # Get campaign performance
        campaign_query = f"""
            SELECT
                campaign.id,
                campaign.name,
                metrics.impressions,
                metrics.clicks,
                metrics.ctr,
                metrics.cost_micros,
                metrics.conversions,
                metrics.conversions_value,
                metrics.all_conversions,
                metrics.all_conversions_value
            FROM campaign
            WHERE campaign.id = {campaign_id}
              AND segments.date DURING {date_range}
        """

        campaign_response = ga_service.search(customer_id=customer_id, query=campaign_query)
        campaign_results = list(campaign_response)

        if not campaign_results:
            return {'error': 'Campaign not found or no data available'}

        row = campaign_results[0]

        # Get asset group performance
        asset_group_query = f"""
            SELECT
                asset_group.id,
                asset_group.name,
                asset_group.status,
                metrics.impressions,
                metrics.clicks,
                metrics.conversions
            FROM asset_group
            WHERE campaign.id = {campaign_id}
              AND segments.date DURING {date_range}
        """

        asset_group_response = ga_service.search(customer_id=customer_id, query=asset_group_query)

        asset_groups = []
        for ag_row in asset_group_response:
            asset_groups.append({
                'asset_group_id': str(ag_row.asset_group.id),
                'asset_group_name': ag_row.asset_group.name,
                'status': ag_row.asset_group.status.name,
                'impressions': ag_row.metrics.impressions,
                'clicks': ag_row.metrics.clicks,
                'conversions': ag_row.metrics.conversions
            })

        cost = row.metrics.cost_micros / 1_000_000
        roas = (row.metrics.conversions_value / cost) if cost > 0 else 0

        return {
            'campaign_id': str(row.campaign.id),
            'campaign_name': row.campaign.name,
            'metrics': {
                'impressions': row.metrics.impressions,
                'clicks': row.metrics.clicks,
                'ctr': row.metrics.ctr,
                'cost': cost,
                'conversions': row.metrics.conversions,
                'conversion_value': row.metrics.conversions_value,
                'all_conversions': row.metrics.all_conversions,
                'all_conversions_value': row.metrics.all_conversions_value,
                'roas': roas
            },
            'asset_groups': asset_groups,
            'total_asset_groups': len(asset_groups)
        }

"""A2.3 — every enumerated column is TEXT with a named CHECK; this module is
the single source of the value lists. `ENUM_REGISTRY` maps every (table, column)
pair that carries a CHECK ... IN (...) constraint in migrations/sql/0001_initial.sql
to the StrEnum whose values must equal that CHECK list — asserted by
tests/test_enums_match_ddl.py, which parses the DDL with `parse_ddl_enum_checks`
below and compares. Extending an enum later = forward-only migration that drops
and recreates the constraint (A2.3)."""
from enum import StrEnum
import re
from pathlib import Path


class RoleCode(StrEnum):
    """membership.role_codes TEXT[] <@ ARRAY[...] (§3) — not a simple CHECK IN
    list so it is not in ENUM_REGISTRY, but the sixteen values are copied
    verbatim from ck_membership_roles_known in the DDL."""
    PHARMACY_ADMIN = "PharmacyAdmin"
    PHARMACY_BUYER = "PharmacyBuyer"
    PHARMACY_RECEIVER = "PharmacyReceiver"
    VENDOR_ADMIN = "VendorAdmin"
    VENDOR_ORDER_DESK = "VendorOrderDesk"
    VENDOR_PICKER = "VendorPicker"
    VENDOR_FINANCE = "VendorFinance"
    COURIER = "Courier"
    DISPATCHER = "Dispatcher"
    OPS_REVIEWER = "OpsReviewer"
    INDEX_PHARMACIST = "IndexPharmacist"
    COMPLIANCE_OFFICER = "ComplianceOfficer"
    PLATFORM_ADMIN = "PlatformAdmin"
    SUPPORT_AGENT = "SupportAgent"
    PLATFORM_FINANCE = "PlatformFinance"


PLATFORM_ROLES = frozenset(
    {RoleCode.OPS_REVIEWER, RoleCode.INDEX_PHARMACIST, RoleCode.COMPLIANCE_OFFICER,
     RoleCode.PLATFORM_ADMIN, RoleCode.SUPPORT_AGENT, RoleCode.PLATFORM_FINANCE}
)


class ConfigScopeType(StrEnum):
    GLOBAL = "GLOBAL"
    REGION = "REGION"
    VENDOR = "VENDOR"


class ConfigValueType(StrEnum):
    INT = "INT"
    DECIMAL = "DECIMAL"
    BOOL = "BOOL"
    ENUM = "ENUM"
    TEXT = "TEXT"


class AuditActionCode(StrEnum):
    CREDIT_LIMIT_OVERRIDE = "CREDIT_LIMIT_OVERRIDE"
    MOV_WAIVER = "MOV_WAIVER"
    SOURCING_ATTESTATION_CHANGE = "SOURCING_ATTESTATION_CHANGE"
    PATIENT_DATA_ACCESS = "PATIENT_DATA_ACCESS"
    DATA_SUBJECT_REQUEST_HANDLED = "DATA_SUBJECT_REQUEST_HANDLED"
    CONFIG_PARAMETER_CHANGE = "CONFIG_PARAMETER_CHANGE"
    ROLE_MEMBERSHIP_CHANGE = "ROLE_MEMBERSHIP_CHANGE"
    LICENCE_DECISION = "LICENCE_DECISION"
    PRICE_DEVIATION_RESOLUTION = "PRICE_DEVIATION_RESOLUTION"
    ALLOCATION_MANUAL_OVERRIDE = "ALLOCATION_MANUAL_OVERRIDE"
    FEE_SCHEDULE_CHANGE = "FEE_SCHEDULE_CHANGE"
    COURIER_DEVICE_REBIND = "COURIER_DEVICE_REBIND"
    INVOICE_WRITTEN_OFF = "INVOICE_WRITTEN_OFF"
    CONSENT_WITHDRAWN = "CONSENT_WITHDRAWN"
    COMPLIANCE_HALT = "COMPLIANCE_HALT"
    ORDER_LINE_QTY_CORRECTION = "ORDER_LINE_QTY_CORRECTION"
    MODE_SWITCH_FLIPPED = "MODE_SWITCH_FLIPPED"
    MODE_SWITCH_ROLLED_BACK = "MODE_SWITCH_ROLLED_BACK"
    RANKING_AUDIT_RESULT = "RANKING_AUDIT_RESULT"
    SCHEMA_REJECTED = "SCHEMA_REJECTED"
    PRICELIST_PUBLISHED = "PRICELIST_PUBLISHED"


class TermsCode(StrEnum):
    PHARMACY_TERMS = "PHARMACY_TERMS"
    VENDOR_TERMS = "VENDOR_TERMS"
    PMS_LINK_TERMS = "PMS_LINK_TERMS"


class OrganisationType(StrEnum):
    PHARMACY = "PHARMACY"
    VENDOR = "VENDOR"
    TRANSPORTER = "TRANSPORTER"


class OrganisationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    CLOSED = "CLOSED"


class Locale(StrEnum):
    PT = "pt"
    AR = "ar"


class MembershipStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class Surface(StrEnum):
    PH = "PH"
    VN = "VN"
    VW = "VW"
    CR = "CR"
    OP = "OP"


class PharmacyLicenceType(StrEnum):
    A = "A"
    B = "B"
    C = "C"
    POSTO_DE_VENDA = "POSTO_DE_VENDA"
    HEALTH_UNIT = "HEALTH_UNIT"


class PharmacyStatus(StrEnum):
    ONBOARDING = "ONBOARDING"
    ACTIVE = "ACTIVE"
    LICENCE_EXPIRING = "LICENCE_EXPIRING"
    SUSPENDED = "SUSPENDED"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"


class SuspensionCause(StrEnum):
    LICENCE_EXPIRED = "LICENCE_EXPIRED"
    HEALTH_SCORE = "HEALTH_SCORE"
    MANUAL_INCIDENT = "MANUAL_INCIDENT"


class AllocationStrategy(StrEnum):
    FEWEST_VENDORS = "FEWEST_VENDORS"
    FASTEST_DISPATCH = "FASTEST_DISPATCH"
    BEST_TERMS = "BEST_TERMS"


class VendorType(StrEnum):
    IMPORTER_WHOLESALER = "IMPORTER_WHOLESALER"
    DISTRIBUTOR = "DISTRIBUTOR"


class DeliveryMode(StrEnum):
    VENDOR_OWN_FLEET = "VENDOR_OWN_FLEET"
    LICENSED_TRANSPORTER = "LICENSED_TRANSPORTER"
    PLATFORM_COORDINATED_COURIER = "PLATFORM_COORDINATED_COURIER"


class AcceptanceMode(StrEnum):
    MANUAL_CONFIRM = "MANUAL_CONFIRM"
    AUTO_ACCEPT_FULL = "AUTO_ACCEPT_FULL"


class VendorStatus(StrEnum):
    ONBOARDING = "ONBOARDING"
    ACTIVE = "ACTIVE"
    LICENCE_EXPIRING = "LICENCE_EXPIRING"
    SUSPENDED = "SUSPENDED"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"


class TransporterPoolType(StrEnum):
    LICENSED_CARRIER = "LICENSED_CARRIER"
    PLATFORM_COORDINATED_POOL = "PLATFORM_COORDINATED_POOL"


class TransporterStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class LicenceHolderType(StrEnum):
    VENDOR = "VENDOR"
    PHARMACY = "PHARMACY"
    TRANSPORTER = "TRANSPORTER"


class LicenceType(StrEnum):
    WHOLESALE_ALVARA = "WHOLESALE_ALVARA"
    RETAIL_A = "RETAIL_A"
    RETAIL_B = "RETAIL_B"
    RETAIL_C = "RETAIL_C"
    POSTO_DE_VENDA = "POSTO_DE_VENDA"
    TRANSPORTER_LICENCE = "TRANSPORTER_LICENCE"


class LicenceStatus(StrEnum):
    SUBMITTED = "SUBMITTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    VALID = "VALID"
    EXPIRING_SOON = "EXPIRING_SOON"
    REJECTED = "REJECTED"
    RENEWED = "RENEWED"
    EXPIRED = "EXPIRED"


class VerificationSubjectType(StrEnum):
    VENDOR_ONBOARDING = "VENDOR_ONBOARDING"
    PHARMACY_ONBOARDING = "PHARMACY_ONBOARDING"
    LICENCE_RENEWAL = "LICENCE_RENEWAL"
    CONFLICT_CHECK = "CONFLICT_CHECK"


class VerificationDecision(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    RESUBMISSION_REQUESTED = "RESUBMISSION_REQUESTED"


class VendorAgreementStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    TERMINATED = "TERMINATED"


class ConsentSubjectType(StrEnum):
    USER = "USER"
    PHARMACY_ACCOUNT = "PHARMACY_ACCOUNT"
    VENDOR_ACCOUNT = "VENDOR_ACCOUNT"


class DataSubjectRequestType(StrEnum):
    ACCESS = "ACCESS"
    EXPORT = "EXPORT"
    ERASURE = "ERASURE"


class DataSubjectRequestStatus(StrEnum):
    RECEIVED = "RECEIVED"
    IN_PROGRESS = "IN_PROGRESS"
    FULFILLED = "FULFILLED"
    REJECTED = "REJECTED"


class AimStatus(StrEnum):
    AUTHORISED = "AUTHORISED"
    NOT_AUTHORISED = "NOT_AUTHORISED"
    PENDING = "PENDING"


class ReviewStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"


class AliasType(StrEnum):
    COLLOQUIAL = "COLLOQUIAL"
    MISSPELLING = "MISSPELLING"
    BRAND = "BRAND"
    ABBREVIATION = "ABBREVIATION"


class AliasStatus(StrEnum):
    PROPOSED = "PROPOSED"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class SubstitutionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class ExternalRefEntityType(StrEnum):
    INDEX_PRODUCT = "INDEX_PRODUCT"
    SUBSTITUTION = "SUBSTITUTION"
    PRODUCT_ALIAS = "PRODUCT_ALIAS"
    PHARMACY_ACCOUNT = "PHARMACY_ACCOUNT"
    REQUEST = "REQUEST"
    ORDER = "ORDER"
    RECEIPT = "RECEIPT"


class ExternalSystem(StrEnum):
    INDEX = "INDEX"
    PMS = "PMS"


class MappingSource(StrEnum):
    PUSHED_BY_SYSTEM = "PUSHED_BY_SYSTEM"
    MATCHED_ON_SYNC = "MATCHED_ON_SYNC"
    MANUAL = "MANUAL"


class FreshnessState(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    WITHDRAWN = "WITHDRAWN"


class ColumnMappingStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"


class PriceListVersionStatus(StrEnum):
    UPLOADED = "UPLOADED"
    MAPPING_NEEDED = "MAPPING_NEEDED"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    SCHEDULED = "SCHEDULED"
    LIVE = "LIVE"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"


class PriceListRowOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    WARNING = "WARNING"
    PENDING_REVIEW = "PENDING_REVIEW"


class PriceListRowOutcomeReason(StrEnum):
    UNMATCHED_PRODUCT = "UNMATCHED_PRODUCT"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    REGULATED_PRICE_DEVIATION = "REGULATED_PRICE_DEVIATION"
    DISCOUNT_ON_REGULATED = "DISCOUNT_ON_REGULATED"
    NOT_AUTHORISED = "NOT_AUTHORISED"
    MISSING_REQUIRED = "MISSING_REQUIRED"
    INVALID_NUMBER = "INVALID_NUMBER"
    DUPLICATE_ROW = "DUPLICATE_ROW"
    PACK_SIZE_MISMATCH = "PACK_SIZE_MISMATCH"
    PVP_DEVIATION_WARNING = "PVP_DEVIATION_WARNING"
    PRODUCT_NOT_PUBLISHED = "PRODUCT_NOT_PUBLISHED"


class RequestMode(StrEnum):
    CATALOGUE = "CATALOGUE"
    RFQ = "RFQ"
    ASSISTED = "ASSISTED"


class RequestChannel(StrEnum):
    APP = "APP"
    WHATSAPP_TEXT = "WHATSAPP_TEXT"
    VOICE_NOTE = "VOICE_NOTE"
    PHOTO = "PHOTO"
    FILE = "FILE"
    PHONE_CALL = "PHONE_CALL"
    PMS = "PMS"


class RequestStatus(StrEnum):
    DRAFT = "DRAFT"
    AWAITING_ADMIN_APPROVAL = "AWAITING_ADMIN_APPROVAL"
    NORMALIZING = "NORMALIZING"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    IN_FULFILMENT = "IN_FULFILMENT"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class LineKind(StrEnum):
    CATALOGUE = "CATALOGUE"
    RFQ = "RFQ"


class MatchStatus(StrEnum):
    AUTO = "AUTO"
    ASK = "ASK"
    UNRESOLVED = "UNRESOLVED"
    RESOLVED = "RESOLVED"


class QuotationStatus(StrEnum):
    INVITED = "INVITED"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"


class OrderStatus(StrEnum):
    PENDING_ACCEPTANCE = "PENDING_ACCEPTANCE"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    DISPATCHED = "DISPATCHED"
    DELIVERED_PENDING_RECEIPT = "DELIVERED_PENDING_RECEIPT"
    RECEIPT_ACCEPTED = "RECEIPT_ACCEPTED"
    DISPUTED = "DISPUTED"
    RETURN_IN_PROGRESS = "RETURN_IN_PROGRESS"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"


class OrderCancelReason(StrEnum):
    PHARMACY = "PHARMACY"
    ACCEPTANCE_TIMEOUT = "ACCEPTANCE_TIMEOUT"
    DISPATCH_TIMEOUT = "DISPATCH_TIMEOUT"
    DELIVERY_EXHAUSTED = "DELIVERY_EXHAUSTED"
    LICENCE_REVOKED = "LICENCE_REVOKED"
    REGULATED_PRICE_INCIDENT = "REGULATED_PRICE_INCIDENT"
    COUNTERFEIT_SUSPECTED = "COUNTERFEIT_SUSPECTED"
    NOTHING_TO_DISPATCH = "NOTHING_TO_DISPATCH"


class PaymentTerms(StrEnum):
    UPFRONT = "UPFRONT"
    COD = "COD"
    CREDIT_N_DAYS = "CREDIT_N_DAYS"


class PriceSource(StrEnum):
    PRICE_REFERENCE = "PRICE_REFERENCE"
    VENDOR_OFFER = "VENDOR_OFFER"
    QUOTATION = "QUOTATION"


class OrderLineFulfilmentStatus(StrEnum):
    LINE_PENDING = "LINE_PENDING"
    LINE_FULL = "LINE_FULL"
    LINE_SHORT = "LINE_SHORT"
    LINE_REROUTED = "LINE_REROUTED"
    LINE_DROPPED = "LINE_DROPPED"


class ShortReason(StrEnum):
    PARTIAL_ACCEPTANCE = "PARTIAL_ACCEPTANCE"
    QTY_CORRECTION = "QTY_CORRECTION"
    LICENCE_LAPSE = "LICENCE_LAPSE"
    COMPLIANCE_HALT = "COMPLIANCE_HALT"


class AllocationReasonCode(StrEnum):
    RANKING_DEFAULT = "RANKING_DEFAULT"
    PHARMACY_OVERRIDE = "PHARMACY_OVERRIDE"
    AUTO_REROUTE = "AUTO_REROUTE"
    MANUAL_OPS = "MANUAL_OPS"


class OpsOverrideCode(StrEnum):
    PHARMACY_REQUESTED_BY_PHONE = "PHARMACY_REQUESTED_BY_PHONE"
    OFFER_DATA_ERROR = "OFFER_DATA_ERROR"
    COMPLIANCE_INSTRUCTION = "COMPLIANCE_INSTRUCTION"


class DemandGapCause(StrEnum):
    NO_FRESH_OFFER = "NO_FRESH_OFFER"
    RFQ_UNANSWERED = "RFQ_UNANSWERED"
    LINE_DROPPED_AFTER_SHORT = "LINE_DROPPED_AFTER_SHORT"
    PRODUCT_NOT_IN_INDEX = "PRODUCT_NOT_IN_INDEX"


class DeliveryJobStatus(StrEnum):
    CREATED = "CREATED"
    ASSIGNED = "ASSIGNED"
    IN_TRANSIT = "IN_TRANSIT"
    DELIVERED = "DELIVERED"
    FAILED_RETRY_PENDING = "FAILED_RETRY_PENDING"
    RETURNED_TO_VENDOR = "RETURNED_TO_VENDOR"
    CANCELLED = "CANCELLED"


class DeliveryAttemptOutcome(StrEnum):
    DELIVERED = "DELIVERED"
    CLOSED = "CLOSED"
    REFUSED = "REFUSED"
    WRONG_ADDRESS = "WRONG_ADDRESS"
    NO_AUTHORISED_RECEIVER = "NO_AUTHORISED_RECEIVER"


class ReceiptLineReasonCode(StrEnum):
    WRONG_ITEM = "WRONG_ITEM"
    DAMAGED = "DAMAGED"
    SHORT = "SHORT"
    EXPIRED = "EXPIRED"
    NEAR_EXPIRY = "NEAR_EXPIRY"


class DisputeType(StrEnum):
    RECEIPT_DISCREPANCY = "RECEIPT_DISCREPANCY"
    REGULATED_PRICE_INCIDENT = "REGULATED_PRICE_INCIDENT"
    FREE_PRICE_MISMATCH = "FREE_PRICE_MISMATCH"


class DisputeRaisedBy(StrEnum):
    PHARMACYRECEIVER = "PharmacyReceiver"
    PHARMACYADMIN = "PharmacyAdmin"
    COMPLIANCEOFFICER = "ComplianceOfficer"
    SYSTEM = "SYSTEM"


class DisputeStatus(StrEnum):
    OPEN = "OPEN"
    UNDER_REVIEW = "UNDER_REVIEW"
    ESCALATED = "ESCALATED"
    RESOLVED = "RESOLVED"


class DisputeOutcome(StrEnum):
    CREDIT_NOTE = "CREDIT_NOTE"
    REPLACEMENT = "REPLACEMENT"
    REJECTED = "REJECTED"
    ESCALATED_TO_COMPLIANCE = "ESCALATED_TO_COMPLIANCE"


class ReturnStatus(StrEnum):
    REQUESTED = "REQUESTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    GOODS_IN_TRANSIT = "GOODS_IN_TRANSIT"
    RECEIVED_BY_VENDOR = "RECEIVED_BY_VENDOR"
    CLOSED = "CLOSED"


class ReturnOrigin(StrEnum):
    PHARMACY_RMA = "PHARMACY_RMA"
    DISPUTE_RESOLUTION = "DISPUTE_RESOLUTION"
    DELIVERY_EXHAUSTED = "DELIVERY_EXHAUSTED"


class ReturnLineReasonCode(StrEnum):
    DAMAGED = "DAMAGED"
    EXPIRED = "EXPIRED"
    NEAR_EXPIRY = "NEAR_EXPIRY"
    WRONG_ITEM = "WRONG_ITEM"
    OTHER = "OTHER"


class TraceabilityEventType(StrEnum):
    PICKED = "PICKED"
    DISPATCHED = "DISPATCHED"
    RECEIVED = "RECEIVED"
    RETURNED = "RETURNED"


class EtaBasis(StrEnum):
    SLA_BOUNDS = "SLA_BOUNDS"
    VENDOR_HISTORY = "VENDOR_HISTORY"
    VENDOR_PROMISE = "VENDOR_PROMISE"
    COURIER_LIVE = "COURIER_LIVE"


class EtaStatus(StrEnum):
    PROVISIONAL = "PROVISIONAL"
    COMMITTED = "COMMITTED"
    IN_TRANSIT_LIVE = "IN_TRANSIT_LIVE"
    REALISED = "REALISED"
    MISSED = "MISSED"
    VOID = "VOID"


class SlaPolicyType(StrEnum):
    QUOTATION = "QUOTATION"
    ACCEPTANCE = "ACCEPTANCE"
    DISPATCH = "DISPATCH"
    DISPUTE_RESOLUTION = "DISPUTE_RESOLUTION"
    REROUTE_ASK = "REROUTE_ASK"
    CONFIRMATION = "CONFIRMATION"
    ADMIN_APPROVAL = "ADMIN_APPROVAL"
    HUMAN_HANDOVER_RESPONSE = "HUMAN_HANDOVER_RESPONSE"


class SlaTimerStatus(StrEnum):
    RUNNING = "RUNNING"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class CreditFacilityStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class LedgerEntryType(StrEnum):
    INVOICE = "INVOICE"
    PAYMENT = "PAYMENT"
    CREDIT_NOTE = "CREDIT_NOTE"
    ADJUSTMENT = "ADJUSTMENT"


class LedgerReferenceType(StrEnum):
    INVOICE = "invoice"
    PAYMENT = "payment"
    CREDIT_NOTE = "credit_note"
    INVOICE_WRITE_OFF = "invoice_write_off"


class InvoiceStatus(StrEnum):
    UPLOADED = "UPLOADED"
    PRICE_MATCH_OK = "PRICE_MATCH_OK"
    PRICE_DEVIATION_FLAGGED = "PRICE_DEVIATION_FLAGGED"
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    WRITTEN_OFF = "WRITTEN_OFF"


class PriceMatchFlag(StrEnum):
    OK = "OK"
    DEVIATION_ABOVE = "DEVIATION_ABOVE"
    DEVIATION_BELOW = "DEVIATION_BELOW"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PENDING = "PENDING"


class InvoiceLineMatchResult(StrEnum):
    OK = "OK"
    DEVIATION_ABOVE = "DEVIATION_ABOVE"
    DEVIATION_BELOW = "DEVIATION_BELOW"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class PaymentMethod(StrEnum):
    MOBILE_MONEY = "MOBILE_MONEY"
    BANK_TRANSFER = "BANK_TRANSFER"
    CASH_ON_DELIVERY = "CASH_ON_DELIVERY"


class PaymentCollectedBy(StrEnum):
    VENDOR_DIRECT = "VENDOR_DIRECT"
    PLATFORM_COLLECTED = "PLATFORM_COLLECTED"


class FeeScheduleType(StrEnum):
    FLAT_PER_ORDER = "FLAT_PER_ORDER"
    PER_DELIVERY_JOB = "PER_DELIVERY_JOB"
    SUBSCRIPTION_MONTHLY = "SUBSCRIPTION_MONTHLY"
    PERCENT_OF_NET_DELIVERED = "PERCENT_OF_NET_DELIVERED"
    VENDOR_SHARE = "VENDOR_SHARE"
    PHARMACY_SERVICE_FEE = "PHARMACY_SERVICE_FEE"


class FeePayer(StrEnum):
    VENDOR = "VENDOR"
    PHARMACY = "PHARMACY"


class FeeBase(StrEnum):
    NET_DELIVERED_VALUE = "NET_DELIVERED_VALUE"
    DECLARED_MARGIN = "DECLARED_MARGIN"


class FeeAppliesTo(StrEnum):
    ALL_ORDERS = "ALL_ORDERS"
    INCREMENTAL_ORDERS_ONLY = "INCREMENTAL_ORDERS_ONLY"


class FeeScopeType(StrEnum):
    GLOBAL = "GLOBAL"
    REGION = "REGION"
    VENDOR = "VENDOR"
    PHARMACY = "PHARMACY"


class FeeEarningEvent(StrEnum):
    RECEIPT_ACCEPTED = "RECEIPT_ACCEPTED"
    ORDER_CLOSED = "ORDER_CLOSED"
    DELIVERY_DELIVERED = "DELIVERY_DELIVERED"
    PERIOD_END = "PERIOD_END"


class FeeLegalStatus(StrEnum):
    OWNER_ACCEPTED = "OWNER_ACCEPTED"
    STANDARD = "STANDARD"


class FeeEventAttribution(StrEnum):
    INCREMENTAL = "INCREMENTAL"
    PRE_EXISTING = "PRE_EXISTING"


class FeeEventStatus(StrEnum):
    ACCRUED = "ACCRUED"
    INVOICED = "INVOICED"
    REVERSED = "REVERSED"


class PlatformInvoiceStatus(StrEnum):
    ISSUED = "ISSUED"
    SETTLED = "SETTLED"


class ModeSwitchKey(StrEnum):
    VENDOR_MODE = "VENDOR_MODE"
    FEE_MODEL = "FEE_MODEL"
    PRIMARY_INTERFACE = "PRIMARY_INTERFACE"


class ModeScopeType(StrEnum):
    GLOBAL = "GLOBAL"
    REGION = "REGION"
    PHARMACY = "PHARMACY"
    VENDOR = "VENDOR"


class ModeSwitchStatus(StrEnum):
    STEADY = "STEADY"
    PROPOSED = "PROPOSED"
    GATE_CHECKING = "GATE_CHECKING"
    READY = "READY"
    FLIPPED = "FLIPPED"
    ROLLED_BACK = "ROLLED_BACK"


class AuditResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class AuditTriggeredBy(StrEnum):
    SCHEDULE = "SCHEDULE"
    MANUAL = "MANUAL"
    GATE = "GATE"


class PmsLinkStatus(StrEnum):
    PENDING_CONSENT = "PENDING_CONSENT"
    LINKED = "LINKED"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"


class ProductKeyMode(StrEnum):
    INDEX_ID = "INDEX_ID"
    MAPPED = "MAPPED"


class ActiveHandler(StrEnum):
    BOT = "BOT"
    ESCALATING = "ESCALATING"
    HUMAN = "HUMAN"


class TicketStatus(StrEnum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class SenderType(StrEnum):
    PHARMACY = "PHARMACY"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"


class ThreadMessageChannel(StrEnum):
    APP = "APP"
    WHATSAPP_TEXT = "WHATSAPP_TEXT"
    PHONE_SUMMARY = "PHONE_SUMMARY"


class NotificationChannel(StrEnum):
    PUSH = "PUSH"
    SMS = "SMS"
    WHATSAPP = "WHATSAPP"
    IN_APP = "IN_APP"
    EMAIL = "EMAIL"
    PMS_WEBHOOK = "PMS_WEBHOOK"


class NotificationStatus(StrEnum):
    QUEUED = "QUEUED"
    SENT = "SENT"
    FAILED = "FAILED"
    READ = "READ"


ENUM_REGISTRY: dict[tuple[str, str], type[StrEnum]] = {
    ("config_parameter", "scope_type"): ConfigScopeType,
    ("config_parameter", "value_type"): ConfigValueType,
    ("audit_event", "action_code"): AuditActionCode,
    ("terms_version", "code"): TermsCode,
    ("organisation", "type"): OrganisationType,
    ("organisation", "status"): OrganisationStatus,
    ("app_user", "locale"): Locale,
    ("membership", "status"): MembershipStatus,
    ("auth_session", "surface"): Surface,
    ("pharmacy_account", "licence_type"): PharmacyLicenceType,
    ("pharmacy_account", "status"): PharmacyStatus,
    ("pharmacy_account", "suspension_cause"): SuspensionCause,
    ("pharmacy_account", "default_allocation_strategy"): AllocationStrategy,
    ("vendor_account", "vendor_type"): VendorType,
    ("vendor_account", "delivery_mode"): DeliveryMode,
    ("vendor_account", "acceptance_mode"): AcceptanceMode,
    ("vendor_account", "locale"): Locale,
    ("vendor_account", "status"): VendorStatus,
    ("vendor_account", "suspension_cause"): SuspensionCause,
    ("transporter_account", "pool_type"): TransporterPoolType,
    ("transporter_account", "status"): TransporterStatus,
    ("licence", "holder_type"): LicenceHolderType,
    ("licence", "type"): LicenceType,
    ("licence", "status"): LicenceStatus,
    ("verification_case", "subject_type"): VerificationSubjectType,
    ("verification_case", "decision"): VerificationDecision,
    ("vendor_agreement", "status"): VendorAgreementStatus,
    ("consent_record", "subject_type"): ConsentSubjectType,
    ("data_subject_request", "type"): DataSubjectRequestType,
    ("data_subject_request", "status"): DataSubjectRequestStatus,
    ("index_product", "aim_status"): AimStatus,
    ("index_product", "review_status"): ReviewStatus,
    ("product_alias", "alias_type"): AliasType,
    ("product_alias", "status"): AliasStatus,
    ("substitution", "status"): SubstitutionStatus,
    ("external_ref", "entity_type"): ExternalRefEntityType,
    ("external_ref", "system"): ExternalSystem,
    ("external_ref", "mapping_source"): MappingSource,
    ("vendor_offer", "freshness_state"): FreshnessState,
    ("vendor_column_mapping", "status"): ColumnMappingStatus,
    ("price_list_version", "status"): PriceListVersionStatus,
    ("price_list_row", "outcome"): PriceListRowOutcome,
    ("price_list_row", "outcome_reason"): PriceListRowOutcomeReason,
    ("request", "mode"): RequestMode,
    ("request", "channel"): RequestChannel,
    ("request", "status"): RequestStatus,
    ("request", "allocation_strategy"): AllocationStrategy,
    ("request_line", "line_kind"): LineKind,
    ("request_line", "match_status"): MatchStatus,
    ("quotation", "status"): QuotationStatus,
    ("order", "status"): OrderStatus,
    ("order", "cancel_reason"): OrderCancelReason,
    ("order", "payment_terms"): PaymentTerms,
    ("order", "delivery_mode"): DeliveryMode,
    ("order_line", "price_source"): PriceSource,
    ("order_line", "fulfilment_status"): OrderLineFulfilmentStatus,
    ("order_line", "short_reason"): ShortReason,
    ("allocation", "reason_code"): AllocationReasonCode,
    ("allocation", "ops_override_code"): OpsOverrideCode,
    ("allocation", "strategy"): AllocationStrategy,
    ("demand_gap", "cause"): DemandGapCause,
    ("delivery_job", "performed_by"): DeliveryMode,
    ("delivery_job", "status"): DeliveryJobStatus,
    ("delivery_attempt", "outcome"): DeliveryAttemptOutcome,
    ("receipt_line", "reason_code"): ReceiptLineReasonCode,
    ("dispute", "type"): DisputeType,
    ("dispute", "raised_by"): DisputeRaisedBy,
    ("dispute", "status"): DisputeStatus,
    ("dispute", "outcome"): DisputeOutcome,
    ("return", "status"): ReturnStatus,
    ("return", "origin"): ReturnOrigin,
    ("return_line", "reason_code"): ReturnLineReasonCode,
    ("traceability_event", "event_type"): TraceabilityEventType,
    ("eta_estimate", "basis"): EtaBasis,
    ("eta_estimate", "status"): EtaStatus,
    ("sla_timer", "policy_type"): SlaPolicyType,
    ("sla_timer", "status"): SlaTimerStatus,
    ("credit_facility", "status"): CreditFacilityStatus,
    ("ledger_entry", "entry_type"): LedgerEntryType,
    ("ledger_entry", "reference_type"): LedgerReferenceType,
    ("invoice", "status"): InvoiceStatus,
    ("invoice", "price_match_flag"): PriceMatchFlag,
    ("invoice_line", "match_result"): InvoiceLineMatchResult,
    ("payment", "method"): PaymentMethod,
    ("payment", "collected_by"): PaymentCollectedBy,
    ("fee_schedule", "type"): FeeScheduleType,
    ("fee_schedule", "payer"): FeePayer,
    ("fee_schedule", "base"): FeeBase,
    ("fee_schedule", "applies_to"): FeeAppliesTo,
    ("fee_schedule", "scope_type"): FeeScopeType,
    ("fee_schedule", "earning_event"): FeeEarningEvent,
    ("fee_schedule", "legal_status"): FeeLegalStatus,
    ("fee_event", "attribution"): FeeEventAttribution,
    ("fee_event", "status"): FeeEventStatus,
    ("platform_invoice", "status"): PlatformInvoiceStatus,
    ("mode_switch", "switch_key"): ModeSwitchKey,
    ("mode_switch", "scope_type"): ModeScopeType,
    ("mode_switch", "status"): ModeSwitchStatus,
    ("ranking_isolation_audit", "result"): AuditResult,
    ("ranking_isolation_audit", "triggered_by"): AuditTriggeredBy,
    ("pms_link", "status"): PmsLinkStatus,
    ("pms_link", "product_key_mode"): ProductKeyMode,
    ("support_thread", "active_handler"): ActiveHandler,
    ("ticket", "status"): TicketStatus,
    ("thread_message", "sender_type"): SenderType,
    ("thread_message", "channel"): ThreadMessageChannel,
    ("notification", "channel"): NotificationChannel,
    ("notification", "status"): NotificationStatus,
}


_TABLE_RE = re.compile(r'CREATE TABLE\s+"?(\w+)"?\s*\((.*?)\n\);', re.DOTALL)
_COL_CHECK_RE = re.compile(r"^\s*(\w+)\s+TEXT[^\n]*?CHECK\s*\(\s*\1\s+IN\s*\(([^)]*)\)", re.MULTILINE)


def parse_ddl_enum_checks(sql_text: str) -> list[tuple[str, str, list[str]]]:
    """Parses every `<col> TEXT ... CHECK (<col> IN ('A', 'B', ...))` in a table
    body out of the given DDL text. Returns (table, column, values) triples in the
    order they appear — the same parse that generated ENUM_REGISTRY above."""
    out = []
    for tm in _TABLE_RE.finditer(sql_text):
        table, body = tm.group(1), tm.group(2)
        for cm in _COL_CHECK_RE.finditer(body):
            col, vals_raw = cm.group(1), cm.group(2)
            vals_clean = re.sub(r"--[^\n']*\n", "\n", vals_raw)
            values = re.findall(r"'([^']*)'", vals_clean)
            out.append((table, col, values))
    return out

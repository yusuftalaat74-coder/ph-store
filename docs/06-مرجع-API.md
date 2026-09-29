# PH Store — مرجع الـ API الكامل

**201 عملية** · 180 مسار · مولَّد من `openapi.json` الحيّ بعد تشغيل الخادم والتحقق منه

كل مسار تحت `/v1` يتطلب رأس:

```
authorization: Bearer <access_token>
```

## الدخول

```
POST /v1/auth/login
{"phone":"+258840000011","password":"rova-demo","surface":"PH"}
```

قيم `surface` — مفروضة على مستوى قاعدة البيانات:

```
PH  صيدلية       VN  مورد        VW  مستودع
CR  مندوب توصيل   OP  تشغيل المنصة
```

قيم `allocation_strategy`:

```
FEWEST_VENDORS | FASTEST_DISPATCH | BEST_TERMS
```

قيم `mode`:

```
CATALOGUE | RFQ | ASSISTED
```

قنوات الاستقبال (`channel`):

```
APP | WHATSAPP_TEXT | VOICE_NOTE | PHOTO | FILE | PHONE_CALL | PMS
```

## الصحة والجاهزية

### `GET /healthz`

Healthz

الاستجابات: `200`

### `GET /readyz`

Readyz

الاستجابات: `200`

## الإدارة

### `POST /v1/admin/jobs/tick`

Run Jobs Tick

رؤوس:

```
  authorization
```

الجسم:

```
  job: string|null
```

الاستجابات: `200` `422`

## المساعد الذكي

### `POST /v1/assistant/ask`

Ask

رؤوس:

```
  authorization
```

الجسم:

```
  question: string (required)
  pharmacy_id: string|null
```

الاستجابات: `200` `422`

### `GET /v1/assistant/capabilities`

Capabilities

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/assistant/order-patterns`

Order Patterns

معاملات الاستعلام:

```
  pharmacy_id: string|null
  window_days: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/assistant/replenishment`

Replenishment

معاملات الاستعلام:

```
  pharmacy_id: string|null
  window_days: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/assistant/savings`

Savings

معاملات الاستعلام:

```
  pharmacy_id: string|null
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/assistant/stock-risks`

Stock Risks

معاملات الاستعلام:

```
  pharmacy_id: string|null
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/assistant/summary`

Summary

معاملات الاستعلام:

```
  pharmacy_id: string|null
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## المصادقة

### `POST /v1/auth/login`

Login

الجسم:

```
  phone: string (required)
  password: string (required)
  surface: string (required)  — PH | VN | VW | CR | OP
```

الاستجابات: `200` `422`

### `POST /v1/auth/logout`

Logout

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/auth/me`

Me

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/auth/select-membership`

Select Membership

رؤوس:

```
  authorization
```

الجسم:

```
  membership_id: string (required)
```

الاستجابات: `200` `422`

## تسجيل الصيدلية ذاتياً والمراجعة

صيدلية بتعمل حسابها من الموبايل، وبتتصفح وتملى السلة فوراً؛ الطلب (`checkout`)
بيرجع `403 PHARMACY_NOT_ACTIVE` لحد ما فريق المراجعة يوافق.

```
POST /v1/auth/signup                                              عام · multipart · 201 بتوكنات · 409 PHONE_ALREADY_REGISTERED · 429 RATE_LIMITED
GET  /v1/pharmacies/me/review-status                              الصيدلية · حالة الحساب والرخصة وسبب الرفض
POST /v1/pharmacies/me/licence                                    PharmacyAdmin · multipart licence_file · أول رفع أو إعادة إرسال بعد الرفض
GET  /v1/licences/{licence_id}/document                           المراجعين + PharmacyAdmin صاحب الرخصة · الملف نفسه
GET  /v1/onboarding-review/pharmacies?status=ONBOARDING|REJECTED|ALL   المراجعين · الأقدم أولاً
POST /v1/onboarding-review/pharmacies/{pharmacy_id}/approve       المراجعين · {licence_number, issue_date, expiry_date, licence_type?, notes?}
POST /v1/onboarding-review/pharmacies/{pharmacy_id}/reject        المراجعين · {reason}
POST /v1/onboarding-review/pharmacies/{pharmacy_id}/licence-document   المراجعين · إرفاق الرخصة نيابةً عن الصيدلية
```

المراجعين = `PlatformAdmin` · `OpsReviewer` · `ComplianceOfficer`.

## الفهرس الدوائي

### `GET /v1/catalogue/products/{product_id}`

Get Product

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/catalogue/search`

Search

معاملات الاستعلام:

```
  q: string (required)
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## الموافقات

### `GET /v1/consents`

List Consents

معاملات الاستعلام:

```
  subject_id: string|null
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/consents/{consent_id}/withdraw`

Withdraw Consent

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## الانكشاف الائتماني

### `GET /v1/credit-exposure/summary`

Exposure Summary

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## تسهيلات الائتمان

### `POST /v1/credit-facilities`

Create Facility

رؤوس:

```
  authorization
```

الجسم:

```
  vendor_id: string (required)
  pharmacy_id: string (required)
  limit_amount: number|string (required)
  opening_balance: number|string
  terms_days: integer|null
  mov_waived: boolean
```

الاستجابات: `201` `422`

### `GET /v1/credit-facilities`

List Facilities

معاملات الاستعلام:

```
  vendor_id: string|null
  pharmacy_id: string|null
  status: string|null
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/credit-facilities/{facility_id}`

Get Facility

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `PATCH /v1/credit-facilities/{facility_id}`

Patch Facility

رؤوس:

```
  authorization
```

الجسم:

```
  limit_amount: number|string|null
  terms_days: integer|null
  mov_waived: boolean|null
  reason: string (required)
```

الاستجابات: `200` `422`

### `POST /v1/credit-facilities/{facility_id}/adjustments`

Post Adjustment

رؤوس:

```
  authorization
```

الجسم:

```
  amount: number|string (required)
  reason: string (required)
```

الاستجابات: `201` `422`

### `POST /v1/credit-facilities/{facility_id}/gate-check`

Gate Check

رؤوس:

```
  authorization
```

الجسم:

```
  order_value: number|string (required)
```

الاستجابات: `200` `422`

### `GET /v1/credit-facilities/{facility_id}/ledger`

Facility Ledger

معاملات الاستعلام:

```
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/credit-facilities/{facility_id}/reinstate`

Reinstate Facility

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/credit-facilities/{facility_id}/statement`

Facility Statement

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/credit-facilities/{facility_id}/suspend`

Suspend Facility

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## إشعارات الدائن

### `POST /v1/credit-notes`

Issue Credit Note

رؤوس:

```
  authorization
```

الجسم:

```
  order_id: string (required)
  amount: number|string (required)
  return_id: string|null
  dispute_id: string|null
```

الاستجابات: `201` `422`

### `GET /v1/credit-notes`

List Credit Notes

معاملات الاستعلام:

```
  order_id: string|null
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## مهام التوصيل

### `GET /v1/delivery-jobs/{job_id}`

Get Delivery Job

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/delivery-jobs/{job_id}/assign`

Assign Delivery Job

رؤوس:

```
  authorization
```

الجسم:

```
  courier_user_id: string (required)
  transporter_id: string|null
```

الاستجابات: `200` `422`

### `POST /v1/delivery-jobs/{job_id}/attempts`

Record Delivery Attempt

رؤوس:

```
  authorization
```

الجسم:

```
  outcome: string (required)
  proof_signature_or_code: string|null
  proof_photo_ref: string|null
```

الاستجابات: `200` `422`

### `POST /v1/delivery-jobs/{job_id}/reschedule`

Reschedule Delivery Job

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/delivery-jobs/{job_id}/start`

Start Delivery Job

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## فجوات الطلب

### `GET /v1/demand-gaps`

List Demand Gaps

معاملات الاستعلام:

```
  cause: string|null
  region_code: string|null
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/demand-gaps/summary`

Demand Gap Summary

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## النزاعات

### `POST /v1/disputes`

Open Dispute

رؤوس:

```
  authorization
```

الجسم:

```
  order_id: string (required)
  type: string (required)  — RECEIPT_DISCREPANCY | REGULATED_PRICE_INCIDENT | FREE_PRICE_MISMATCH
  receipt_line_id: string|null
  invoice_line_id: string|null
  notes: string|null
```

الاستجابات: `201` `422`

### `GET /v1/disputes`

List Disputes

معاملات الاستعلام:

```
  status: string|null
  type: string|null  — RECEIPT_DISCREPANCY | REGULATED_PRICE_INCIDENT | FREE_PRICE_MISMATCH
  order_id: string|null
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/disputes/{dispute_id}`

Get Dispute

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/disputes/{dispute_id}/assign`

Assign Dispute

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/disputes/{dispute_id}/escalate`

Escalate Dispute

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/disputes/{dispute_id}/resolve`

Resolve Dispute

رؤوس:

```
  authorization
```

الجسم:

```
  outcome: string (required)  — CREDIT_NOTE | REPLACEMENT | REJECTED | ESCALATED_TO_COMPLIANCE
  notes: string|null
```

الاستجابات: `200` `422`

## الاستقبال متعدد القنوات

### `POST /v1/intake/file`

Intake File

رؤوس:

```
  authorization
```

نوع المحتوى: `multipart/form-data`

الجسم:

```
  file: string (required)
  pharmacy_id: string|null
  mode: string  — CATALOGUE | RFQ | ASSISTED
  allocation_strategy: string  — FEWEST_VENDORS | FASTEST_DISPATCH | BEST_TERMS
```

الاستجابات: `201` `422`

### `POST /v1/intake/phone-call`

Intake Phone Call

رؤوس:

```
  authorization
```

الجسم:

```
  pharmacy_id: string (required)
  mode: string  — CATALOGUE | RFQ | ASSISTED
  allocation_strategy: string  — FEWEST_VENDORS | FASTEST_DISPATCH | BEST_TERMS
  call_summary: string (required)
  lines: array|null
```

الاستجابات: `201` `422`

### `POST /v1/intake/photo`

Intake Photo

رؤوس:

```
  authorization
```

نوع المحتوى: `multipart/form-data`

الجسم:

```
  file: string (required)
  pharmacy_id: string|null
  mode: string  — CATALOGUE | RFQ | ASSISTED
  allocation_strategy: string  — FEWEST_VENDORS | FASTEST_DISPATCH | BEST_TERMS
```

الاستجابات: `201` `422`

### `POST /v1/intake/pms`

Intake Pms

رؤوس:

```
  Idempotency-Key
  authorization
```

الجسم:

```
  pharmacy_id: string|null
  mode: string  — CATALOGUE | RFQ | ASSISTED
  allocation_strategy: string  — FEWEST_VENDORS | FASTEST_DISPATCH | BEST_TERMS
  lines: array (required)
```

الاستجابات: `201` `422`

### `GET /v1/intake/queue`

Transcription Queue

معاملات الاستعلام:

```
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/intake/text`

Intake Text

رؤوس:

```
  authorization
```

الجسم:

```
  pharmacy_id: string|null
  channel: string  — APP | WHATSAPP_TEXT
  mode: string  — CATALOGUE | RFQ | ASSISTED
  allocation_strategy: string  — FEWEST_VENDORS | FASTEST_DISPATCH | BEST_TERMS
  body: string|null
  lines: array|null
```

الاستجابات: `201` `422`

### `POST /v1/intake/voice`

Intake Voice

رؤوس:

```
  authorization
```

نوع المحتوى: `multipart/form-data`

الجسم:

```
  file: string (required)
  pharmacy_id: string|null
  mode: string  — CATALOGUE | RFQ | ASSISTED
  allocation_strategy: string  — FEWEST_VENDORS | FASTEST_DISPATCH | BEST_TERMS
```

الاستجابات: `201` `422`

### `GET /v1/intake/{request_id}`

Normalisation View

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/intake/{request_id}/transcribe`

Transcribe

رؤوس:

```
  authorization
```

الجسم:

```
  lines: array (required)
```

الاستجابات: `200` `422`

## الفواتير

### `GET /v1/invoices`

List Invoices

معاملات الاستعلام:

```
  status: string|null
  price_match_flag: string|null
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/invoices/{invoice_id}`

Get Invoice

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/invoices/{invoice_id}/write-off`

Write Off Invoice

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## التراخيص

### `POST /v1/licences`

Create Licence

رؤوس:

```
  authorization
```

الجسم:

```
  holder_type: string (required)  — VENDOR | PHARMACY | TRANSPORTER
  holder_id: string (required)
  type: string (required)  — WHOLESALE_ALVARA | RETAIL_A | RETAIL_B | RETAIL_C | POSTO_DE_VENDA | TRANSPORTER_LICENCE
  number: string (required)
  issuer: string (required)
  issue_date: string (required)
  expiry_date: string (required)
  document_ref: string (required)
```

الاستجابات: `201` `422`

### `GET /v1/licences`

List Licences

معاملات الاستعلام:

```
  holder_type: string|null  — PHARMACY | VENDOR | TRANSPORTER
  holder_id: string|null
  status: string|null
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/licences/{licence_id}`

Get Licence

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/licences/{licence_id}/approve`

Approve Licence

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/licences/{licence_id}/open-review`

Open Licence Review

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/licences/{licence_id}/reject`

Reject Licence

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/licences/{licence_id}/renew`

Renew Licence

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/licences/{licence_id}/transitions`

Licence Transitions

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## العضويات

### `POST /v1/memberships`

Create Membership

رؤوس:

```
  authorization
```

الجسم:

```
  user_id: string (required)
  organisation_id: string|null
  role_codes: array (required)
```

الاستجابات: `201` `422`

### `GET /v1/memberships`

List Memberships

معاملات الاستعلام:

```
  user_id: string|null
  organisation_id: string|null
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/memberships/{membership_id}/revoke`

Revoke Membership

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## الإشعارات

### `GET /v1/notifications`

List Notifications

معاملات الاستعلام:

```
  status: string|null  — QUEUED | SENT | FAILED | READ
  unread_only: boolean
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/notifications/dispatch`

Dispatch Queued

معاملات الاستعلام:

```
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/notifications/read-all`

Mark All Read

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/notifications/unread-count`

Unread Count

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/notifications/{notification_id}`

Get Notification

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/notifications/{notification_id}/read`

Mark Read

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## إدارة الإشعارات

### `GET /v1/notifications-admin/queue`

Admin Queue

معاملات الاستعلام:

```
  channel: string|null  — PUSH | SMS | WHATSAPP | IN_APP | EMAIL | PMS_WEBHOOK
  status: string|null  — QUEUED | SENT | FAILED | READ
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/notifications-admin/summary`

Admin Summary

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## كونسول العمليات

### `GET /v1/ops/audit-events`

Audit Events

معاملات الاستعلام:

```
  action_code: string|null
  subject_id: string|null
  actor_user_id: string|null
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/ops/config`

List Config

معاملات الاستعلام:

```
  key_prefix: string|null
  scope_type: string|null  — GLOBAL | REGION | VENDOR
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/ops/config/{key}`

Get Config

معاملات الاستعلام:

```
  vendor_id: string|null
  region_code: string|null
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `PUT /v1/ops/config/{key}`

Put Config

رؤوس:

```
  authorization
```

الجسم:

```
  value: string (required)
  scope_type: string  — GLOBAL | REGION | VENDOR
  scope_id: string|null
  reason: string (required)
```

الاستجابات: `200` `422`

### `GET /v1/ops/dashboard`

Dashboard

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/ops/data-subject-requests`

List Dsr

معاملات الاستعلام:

```
  status: string|null
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/ops/data-subject-requests`

Create Dsr

رؤوس:

```
  authorization
```

الجسم:

```
  subject_user_id: string (required)
  type: string (required)  — ACCESS | EXPORT | ERASURE
```

الاستجابات: `201` `422`

### `POST /v1/ops/data-subject-requests/{request_id}/decide`

Decide Dsr

رؤوس:

```
  authorization
```

الجسم:

```
  status: string (required)  — IN_PROGRESS | FULFILLED | REJECTED
  result_ref: string|null
```

الاستجابات: `200` `422`

### `GET /v1/ops/machines`

List Machines

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/ops/mode-switches`

Propose Switch

رؤوس:

```
  authorization
```

الجسم:

```
  switch_key: string (required)  — VENDOR_MODE | FEE_MODEL | PRIMARY_INTERFACE
  proposed_value: string (required)
  scope_type: string  — GLOBAL | REGION | PHARMACY | VENDOR
  scope_id: string|null
```

الاستجابات: `201` `422`

### `GET /v1/ops/mode-switches`

List Switches

معاملات الاستعلام:

```
  switch_key: string|null  — VENDOR_MODE | FEE_MODEL | PRIMARY_INTERFACE
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/ops/mode-switches/{switch_id}`

Get Switch

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/ops/mode-switches/{switch_id}/check-gates`

Check Gates

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/ops/mode-switches/{switch_id}/flip`

Flip Switch

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/ops/mode-switches/{switch_id}/gates-failed`

Gates Failed

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/ops/mode-switches/{switch_id}/gates-passed`

Gates Passed

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/ops/mode-switches/{switch_id}/rollback`

Rollback Switch

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/ops/orders`

List Orders

معاملات الاستعلام:

```
  status: string|null
  vendor_id: string|null
  pharmacy_id: string|null
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/ops/orders/{order_id}`

Order Detail

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/ops/pms-links`

List Pms Links

معاملات الاستعلام:

```
  status: string|null
  pharmacy_id: string|null
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/ops/queues/licences-expiring`

Licences Expiring

معاملات الاستعلام:

```
  days: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/ops/queues/overdue-timers`

Overdue Timers

معاملات الاستعلام:

```
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/ops/queues/verification`

Verification Queue

معاملات الاستعلام:

```
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/ops/ranking-audits`

Ranking Audits

معاملات الاستعلام:

```
  result: string|null  — PASS | FAIL
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/ops/ranking-audits/latest`

Latest Ranking Audit

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/ops/sla-timers`

List Timers

معاملات الاستعلام:

```
  policy_type: string|null
  status: string|null
  subject_id: string|null
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/ops/state-transitions`

State Transitions

معاملات الاستعلام:

```
  machine: string|null
  subject_id: string|null
  subject_type: string|null
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/ops/traceability/{order_id}`

Traceability

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/ops/vendor-scores`

Vendor Scores

معاملات الاستعلام:

```
  vendor_id: string|null
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/ops/vendors/{vendor_id}/health`

Vendor Health

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## بنود الأمر

### `POST /v1/order-lines/{line_id}/pick`

Pick Order Line

رؤوس:

```
  authorization
```

الجسم:

```
  batch_number: string (required)
  lot_number: string (required)
  expiry_date: string (required)
  seal_ids: array (required)
  near_expiry_ack: boolean
```

الاستجابات: `200` `422`

### `POST /v1/order-lines/{line_id}/reroute-decision`

Reroute Decision

رؤوس:

```
  authorization
```

الجسم:

```
  action: string (required)
  substitution_id: string|null
```

الاستجابات: `200` `422`

## الأوامر

### `GET /v1/orders/{order_id}`

Get Order

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/orders/{order_id}/accept`

Accept Order

رؤوس:

```
  authorization
```

الجسم:

```
  lines: array (required)
  promised_dispatch_at: string|null
```

الاستجابات: `200` `422`

### `POST /v1/orders/{order_id}/compliance-halt`

Compliance Halt

رؤوس:

```
  authorization
```

الجسم:

```
  reason: string (required)
```

الاستجابات: `200` `422`

### `POST /v1/orders/{order_id}/dispatch`

Dispatch Order

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/orders/{order_id}/invoice`

Upload Invoice

رؤوس:

```
  authorization
```

الجسم:

```
  vendor_invoice_number: string (required)
  lines: array (required)
  document_ref: string|null
```

الاستجابات: `200` `422`

### `POST /v1/orders/{order_id}/receipt`

Receipt

رؤوس:

```
  authorization
```

الجسم:

```
  lines: array (required)
  channel_ref: string|null
```

الاستجابات: `200` `422`

### `POST /v1/orders/{order_id}/reject`

Reject Order

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## المنشآت

### `POST /v1/organisations`

Create Organisation

رؤوس:

```
  authorization
```

الجسم:

```
  tax_id: string (required)
  legal_name: string (required)
  type: string (required)  — PHARMACY | VENDOR | TRANSPORTER
  country: string
```

الاستجابات: `201` `422`

### `GET /v1/organisations`

List Organisations

معاملات الاستعلام:

```
  type: string|null  — PHARMACY | VENDOR | TRANSPORTER
  status: string|null
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/organisations/{organisation_id}`

Get Organisation

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## المدفوعات

### `POST /v1/payments`

Create Payment

رؤوس:

```
  authorization
```

الجسم:

```
  pharmacy_id: string (required)
  vendor_id: string (required)
  method: string (required)
  amount: number|string (required)
  external_ref: string|null
  collected_by: string
  allocations: array (required)
```

الاستجابات: `200` `422`

### `GET /v1/payments`

List Payments

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## الصيدليات

### `POST /v1/pharmacies`

Create Pharmacy

رؤوس:

```
  authorization
```

الجسم:

```
  organisation_id: string (required)
  region_code: string (required)
  licence_type: string (required)  — A | B | C | POSTO_DE_VENDA | HEALTH_UNIT
  trade_name: string (required)
  address: string (required)
  latitude: number (required)
  longitude: number (required)
  auto_reroute: boolean
  default_allocation_strategy: string|null
```

الاستجابات: `201` `422`

### `GET /v1/pharmacies`

List Pharmacies

معاملات الاستعلام:

```
  status: string|null
  region_code: string|null
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/pharmacies/{pharmacy_id}`

Get Pharmacy

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `PATCH /v1/pharmacies/{pharmacy_id}`

Patch Pharmacy

رؤوس:

```
  authorization
```

الجسم:

```
  trade_name: string|null
  address: string|null
  auto_reroute: boolean|null
  buyer_approval_threshold: number|null
  default_allocation_strategy: string|null
```

الاستجابات: `200` `422`

### `POST /v1/pharmacies/{pharmacy_id}/approve`

Approve Pharmacy

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/pharmacies/{pharmacy_id}/close`

Close Pharmacy

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/pharmacies/{pharmacy_id}/reinstate`

Reinstate Pharmacy

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/pharmacies/{pharmacy_id}/reject`

Reject Pharmacy

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/pharmacies/{pharmacy_id}/resubmit`

Resubmit Pharmacy

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/pharmacies/{pharmacy_id}/suspend`

Suspend Pharmacy

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## صفوف قوائم الأسعار

### `POST /v1/price-list-rows/{row_id}/resolve`

Resolve Price List Row

رؤوس:

```
  authorization
```

الجسم:

```
  index_product_id: string|null
  propose_alias: string|null
```

الاستجابات: `200` `422`

## قوائم الأسعار

### `GET /v1/price-lists/template`

Get Template

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/price-lists/{version_id}`

Get Price List

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/price-lists/{version_id}/discard`

Discard Price List

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `PUT /v1/price-lists/{version_id}/mapping`

Save Price List Mapping

رؤوس:

```
  authorization
```

الجسم:

```
  mapping: object (required)
  sheet_selector: string|null
```

الاستجابات: `200` `422`

### `GET /v1/price-lists/{version_id}/preview`

Preview Price List

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/price-lists/{version_id}/publish`

Publish Price List

رؤوس:

```
  authorization
```

الجسم:

```
  effective_from: string|null
```

الاستجابات: `200` `422`

### `GET /v1/price-lists/{version_id}/report`

Download Report

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/price-lists/{version_id}/rows`

List Price List Rows

معاملات الاستعلام:

```
  outcome: string|null
  limit: integer|null
  cursor: string|null
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## الأقاليم

### `GET /v1/regions`

List Regions

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## بنود الطلب

### `DELETE /v1/request-lines/{line_id}`

Drop Line

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/request-lines/{line_id}/allocate`

Allocate Remainder Line

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/request-lines/{line_id}/resolve`

Resolve Line

رؤوس:

```
  authorization
```

الجسم:

```
  index_product_id: string (required)
```

الاستجابات: `200` `422`

## الطلبات

### `POST /v1/requests`

Create Request

رؤوس:

```
  authorization
```

الجسم:

```
  mode: string  — CATALOGUE | RFQ | ASSISTED
  allocation_strategy: string  — FEWEST_VENDORS | FASTEST_DISPATCH | BEST_TERMS
```

الاستجابات: `200` `422`

### `GET /v1/requests`

List Requests

معاملات الاستعلام:

```
  status: string|null
  channel: string|null  — APP | WHATSAPP_TEXT | VOICE_NOTE | PHOTO | FILE | PHONE_CALL | PMS
  pharmacy_id: string|null
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/requests/{request_id}`

Get Request

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/requests/{request_id}/abandon`

Abandon Request

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/requests/{request_id}/admin-approve`

Admin Approve Request

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/requests/{request_id}/admin-reject`

Admin Reject Request

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/requests/{request_id}/cancel`

Cancel Request

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/requests/{request_id}/checkout`

Checkout Endpoint

رؤوس:

```
  Idempotency-Key
  authorization
```

الجسم:

```
  payment_overrides: object
```

الاستجابات: `200` `422`

### `POST /v1/requests/{request_id}/confirm`

Confirm Request

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/requests/{request_id}/lines`

Add Line

رؤوس:

```
  authorization
```

الجسم:

```
  index_product_id: string (required)
  qty_requested: integer (required)
```

الاستجابات: `200` `422`

### `POST /v1/requests/{request_id}/normalization-complete`

Normalization Complete

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/requests/{request_id}/request-changes`

Request Changes

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/requests/{request_id}/submit`

Submit Request

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/requests/{request_id}/transitions`

Request Transitions

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## المرتجعات

### `POST /v1/returns`

Open Return

رؤوس:

```
  authorization
```

الجسم:

```
  order_id: string (required)
  origin: string  — PHARMACY_RMA | DISPUTE_RESOLUTION | DELIVERY_EXHAUSTED
  lines: array (required)
```

الاستجابات: `201` `422`

### `GET /v1/returns`

List Returns

معاملات الاستعلام:

```
  status: string|null
  order_id: string|null
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/returns/{return_id}`

Get Return

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/returns/{return_id}/approve`

Approve Return

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/returns/{return_id}/receive`

Receive Return

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/returns/{return_id}/reject`

Reject Return

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/returns/{return_id}/ship`

Ship Return

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## خدمة العملاء

### `GET /v1/support/threads`

List Threads

معاملات الاستعلام:

```
  active_handler: string|null  — BOT | ESCALATING | HUMAN
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/support/threads/me`

My Thread

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/support/threads/me/escalate`

Escalate

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/support/threads/me/messages`

Pharmacy Message

رؤوس:

```
  authorization
```

الجسم:

```
  body: string (required)
  channel: string  — APP | WHATSAPP_TEXT | PHONE_SUMMARY
```

الاستجابات: `201` `422`

### `GET /v1/support/threads/{thread_id}`

Get Thread

معاملات الاستعلام:

```
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/support/threads/{thread_id}/messages`

Agent Message

رؤوس:

```
  authorization
```

الجسم:

```
  body: string (required)
  channel: string  — APP | WHATSAPP_TEXT | PHONE_SUMMARY
```

الاستجابات: `201` `422`

### `POST /v1/support/threads/{thread_id}/take-over`

Take Over

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/support/tickets`

List Tickets

معاملات الاستعلام:

```
  status: string|null  — OPEN | IN_PROGRESS | RESOLVED | CLOSED
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/support/tickets/{ticket_id}`

Get Ticket

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `PATCH /v1/support/tickets/{ticket_id}`

Patch Ticket

رؤوس:

```
  authorization
```

الجسم:

```
  related_order_id: string|null
  requires_vendor_relay: boolean|null
```

الاستجابات: `200` `422`

### `POST /v1/support/tickets/{ticket_id}/close`

Close Ticket

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/support/tickets/{ticket_id}/resolve`

Resolve Ticket

رؤوس:

```
  authorization
```

الجسم:

```
  resolution_note: string (required)
```

الاستجابات: `200` `422`

## الشروط

### `GET /v1/terms`

List Terms

معاملات الاستعلام:

```
  code: string|null  — PHARMACY_TERMS | VENDOR_TERMS | PMS_LINK_TERMS
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/terms/accept`

Accept Terms

رؤوس:

```
  authorization
```

الجسم:

```
  terms_version_id: string (required)
  subject_type: string (required)  — USER | PHARMACY_ACCOUNT | VENDOR_ACCOUNT
  subject_id: string (required)
  purpose: string
  channel_ref: string|null
```

الاستجابات: `201` `422`

### `GET /v1/terms/{code}/current`

Current Terms

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## المستخدمون

### `POST /v1/users`

Create User

رؤوس:

```
  authorization
```

الجسم:

```
  phone: string (required)
  name: string (required)
  password: string (required)
  locale: string  — pt | ar
```

الاستجابات: `201` `422`

### `GET /v1/users/{user_id}`

Get User

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## اتفاقيات الموردين

### `POST /v1/vendor-agreements`

Create Agreement

رؤوس:

```
  authorization
```

الجسم:

```
  vendor_id: string (required)
  document_ref: string (required)
  founding_supplier: boolean
  multi_vendor_clause_ack: boolean
  exclusivity_until: string|null
  preferential_rate_until: string|null
  badge_until: string|null
```

الاستجابات: `201` `422`

### `GET /v1/vendor-agreements`

List Agreements

معاملات الاستعلام:

```
  vendor_id: string|null
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## الموردون

### `POST /v1/vendors`

Create Vendor

رؤوس:

```
  authorization
```

الجسم:

```
  organisation_id: string (required)
  region_code: string (required)
  trade_name: string (required)
  vendor_type: string (required)  — IMPORTER_WHOLESALER | DISTRIBUTOR
  delivery_mode: string (required)  — VENDOR_OWN_FLEET | LICENSED_TRANSPORTER | PLATFORM_COORDINATED_COURIER
  mov_amount: number (required)
  acceptance_mode: string  — MANUAL_CONFIRM | AUTO_ACCEPT_FULL
  sourcing_attestation: boolean
  locale: string  — pt | ar
```

الاستجابات: `201` `422`

### `GET /v1/vendors`

List Vendors

معاملات الاستعلام:

```
  status: string|null
  region_code: string|null
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/vendors/{vendor_id}`

Get Vendor

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `PATCH /v1/vendors/{vendor_id}`

Patch Vendor

رؤوس:

```
  authorization
```

الجسم:

```
  trade_name: string|null
  delivery_mode: string|null
  mov_amount: number|null
  acceptance_mode: string|null
  locale: string|null
```

الاستجابات: `200` `422`

### `POST /v1/vendors/{vendor_id}/approve`

Approve Vendor

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/vendors/{vendor_id}/attestation`

Set Attestation

رؤوس:

```
  authorization
```

الجسم:

```
  sourcing_attestation: boolean (required)
```

الاستجابات: `200` `422`

### `POST /v1/vendors/{vendor_id}/close`

Close Vendor

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/vendors/{vendor_id}/column-mappings`

List Column Mappings

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/vendors/{vendor_id}/price-lists`

Upload Price List

رؤوس:

```
  authorization
```

نوع المحتوى: `multipart/form-data`

الجسم:

```
  file: string (required)
  partial_update: boolean
  effective_from: string|null
```

الاستجابات: `200` `422`

### `GET /v1/vendors/{vendor_id}/price-lists`

List Price Lists

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/vendors/{vendor_id}/reinstate`

Reinstate Vendor

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/vendors/{vendor_id}/reject`

Reject Vendor

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/vendors/{vendor_id}/resubmit`

Resubmit Vendor

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/vendors/{vendor_id}/suspend`

Suspend Vendor

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

## ملفات التحقق

### `POST /v1/verification-cases`

Create Case

رؤوس:

```
  authorization
```

الجسم:

```
  subject_type: string (required)  — VENDOR_ONBOARDING | PHARMACY_ONBOARDING | LICENCE_RENEWAL | CONFLICT_CHECK
  organisation_id: string (required)
  licence_id: string|null
```

الاستجابات: `201` `422`

### `GET /v1/verification-cases`

List Cases

معاملات الاستعلام:

```
  decision: string|null
  subject_type: string|null  — VENDOR_ONBOARDING | PHARMACY_ONBOARDING | LICENCE_RENEWAL | CONFLICT_CHECK
  limit: integer
```

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `GET /v1/verification-cases/{case_id}`

Get Case

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/verification-cases/{case_id}/assign`

Assign Case

رؤوس:

```
  authorization
```

الاستجابات: `200` `422`

### `POST /v1/verification-cases/{case_id}/decide`

Decide Case

رؤوس:

```
  authorization
```

الجسم:

```
  decision: string (required)  — APPROVED | REJECTED | RESUBMISSION_REQUESTED
  decision_notes: string|null
```

الاستجابات: `200` `422`

## تشغيل الخادم محلياً

```
export ROVA_DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5432/phstore_dev"
export ROVA_JWT_SECRET="dev-secret-at-least-32-bytes-long-ok"

python3 -m alembic upgrade head
python3 -m rova.cli seed
python3 -m uvicorn --factory rova.main:create_app --port 8099
python3 -m pytest -q
```

كلمة مرور كل الحسابات المبذورة:

```
rova-demo
```

## الحسابات المبذورة

```
+258840000001  mem_ahmed_admin        VN
+258840000002  mem_ahmed_desk         VN
+258840000003  mem_ahmed_finance      VN
+258840000004  mem_ahmed_picker       VN
+258840000005  mem_ahmed_dispatcher   VN
+258840000006  mem_ahmed_courier      CR
+258840000010  mem_baixa_admin        PH
+258840000011  mem_baixa_buyer        PH
+258840000012  mem_baixa_receiver     PH
+258840000090  mem_platform_admin     OP
+258840000091  mem_compliance         OP
+258840000092  mem_ops                OP
+258840000093  mem_index_pharmacist   OP
+258840000094  mem_support            OP
+258840000095  mem_platform_finance   OP
```

## نتيجة التحقق — ٢٣ سبتمبر ٢٠٢٦

قاعدة بيانات نضيفة من الصفر، ترحيلات، بذر، الاختبارات كاملة، ثم خادم حيّ:

```
67   جدول (66 + alembic_version)
49   صف في config_parameter
17   آلة حالة · 135 انتقال
332  اختبار — كلها نجحت
201  عملية API حيّة
0/30 قراءات بائتة بعد الكتابة
```

تدفّق جرى فعلياً على الخادم الحيّ — استقبال واتساب:

```
POST /v1/intake/text
{"body": "Amoxicilina 500 mg 21\nParacetamol 500\nqqqzzz nada"}

→ request RQ-2026-000001 · NORMALIZING · WHATSAPP_TEXT
   سطر 1  ASK  0.79  candidates: [idx_amoxicilina_500]
   سطر 2  ASK  0.61  candidates: [idx_paracetamol_500]
   سطر 3  UNRESOLVED   index_product_id: null
```

المطابِق لم يخمّن ولا سطر واحد. السطر الثالث خرج `UNRESOLVED` بلا منتج،
والسطران الأولان خرجا `ASK` بمرشّحين لينتقي الصيدلي — وهذا بالضبط ما يمنع
`NORMALIZATION_COMPLETE` من المرور قبل أن يقرّر إنسان.

## العيب الذي أُصلح

قيمة `enum` غير صحيحة في جسم الطلب كانت تمرّ من طبقة التحقق وتصطدم بقيد
`CHECK` في قاعدة البيانات، فيرجع:

```
500  {"error":{"code":"INTERNAL","message":"internal error"}}
```

أُصلح في المواضع الثلاثة بتعريف القيم كـ `Literal` في مخططات `pydantic`:

```
POST /v1/auth/login   surface
POST /v1/requests     mode
POST /v1/requests     allocation_strategy
```

الآن ترجع `422 VALIDATION_ERROR` بالحقل المرفوض، ولكل واحدة اختبار انحدار.

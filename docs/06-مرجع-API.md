# ROVA — مرجع الـ API الكامل

**45 عملية** · 43 مسار · مولَّد من `openapi.json` الحيّ بعد تشغيل الخادم والتحقق منه

كل مسار تحت `/v1` يتطلب رأس:

```
authorization: Bearer <access_token>
```

## الدخول

```
POST /v1/auth/login
{"phone":"+258840000011","password":"rova-demo","surface":"PH"}
```

قيم `surface` المسموح بها — مفروضة على مستوى قاعدة البيانات نفسها:

```
PH  صيدلية       VN  مورد        VW  مستودع
CR  مندوب توصيل   OP  تشغيل المنصة
```

قيم `allocation_strategy` في إنشاء الطلب:

```
FEWEST_VENDORS | FASTEST_DISPATCH | BEST_TERMS
```

قيم `mode`:

```
CATALOGUE | RFQ | ASSISTED
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

## المصادقة

### `POST /v1/auth/login`

Login

الجسم:

```
  phone: string (required)
  password: string (required)
  surface: string (required)
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

## بنود الطلب

### `POST /v1/request-lines/{line_id}/allocate`

Allocate Remainder Line

رؤوس:

```
  authorization
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
  mode: string
  allocation_strategy: string
```

الاستجابات: `200` `422`

### `GET /v1/requests/{request_id}`

Get Request

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

## الموردون

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

## تشغيل الخادم محلياً

```
export ROVA_DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5432/rova_dev"
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

```
67   جدول في قاعدة البيانات (66 + alembic_version)
49   صف في config_parameter
230  اختبار — كلها نجحت في 17.33 ثانية
45   عملية API حيّة
0/30 قراءات بائتة بعد الكتابة
```

تدفّق كامل جرى فعلياً على الخادم الحيّ:

```
login → create request → add line → checkout
→ order ord_…GAKY / RQ-2026-000003-V01
   goods_total    "850.00"
   platform_fees  [{ "label_pt": "taxa de serviço", "amount": "150.00" }]
```

البضاعة والرسوم رجعا منفصلين تماماً في الاستجابة، والمبالغ نصوص لا أرقام عائمة.

## عيب مفتوح — يجب إصلاحه

قيمة `enum` غير صحيحة في جسم الطلب تمرّ من طبقة التحقق وتصطدم بقيد
`CHECK` في قاعدة البيانات، فيرجع الخادم:

```
500  {"error":{"code":"INTERNAL","message":"internal error"}}
```

بدل ما يرجع `422 VALIDATION_ERROR` بالحقل المرفوض. تكرّر في موضعين أثناء التحقق:

```
POST /v1/auth/login   {"surface": "PHARMACY_APP"}
POST /v1/requests     {"allocation_strategy": "BEST_PRICE"}
```

الأثر: مطوّر الواجهة يستلم `500` مبهم بدل رسالة تقول أي قيمة مرفوضة.
الإصلاح: تعريف القيم المسموحة كـ `Literal` في مخططات `pydantic` بحيث
تتطابق مع قيود `CHECK` في الترحيلات.

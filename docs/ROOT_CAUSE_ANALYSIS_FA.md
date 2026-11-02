# ماژول تحلیل علل احتمالی خطا (RCA) و پیمایش زمانی

این قابلیت به **همین پروژهٔ RootRewind** اضافه شده است، نه یک برنامهٔ جداگانه.

## هدف

یک خطا را انتخاب کنید و ببینید در چند دقیقهٔ قبل از آن، چه هشدارها، خطاهای دیگر و تغییراتی در سرویس‌های مرتبط ثبت شده‌اند. اگر دادهٔ Trace دارای `trace_id` و شناسهٔ والد `parent_span_id` باشد، ارتباط صریح سرویس‌ها استخراج می‌شود. گزارش قابل پیمایش HTML امکان مشاهدهٔ وضعیت *مشاهده‌شده در لاگ‌ها* در نقاط زمانی متفاوت را فراهم می‌کند.

**محدودیت مهم:** این بخش تنها شواهد و فرضیه‌های قابل بررسی تولید می‌کند؛ از روی لاگ به‌تنهایی نمی‌توان «علت قطعی» را اثبات کرد. امتیازها صرفاً روش مرتب‌سازی قواعد اکتشافی هستند و احتمال آماری محسوب نمی‌شوند. این ماژول بازپخش دستوربه‌دستور یا وضعیت حافظهٔ واقعی برنامه نیست.

## اجرای نمونه در ویندوز

محل نصب پیش‌فرض:

`D:\Github_Projects\time-travel-debugger`

در PowerShell:

```powershell
cd D:\Github_Projects\time-travel-debugger

# فهرست خطاهای قابل انتخاب؛ شماره‌ها از 1 شروع می‌شوند
.\.venv\Scripts\ttd.exe incidents examples\incident-rca.jsonl

# تحلیل خطای دوم با بررسی 10 دقیقه قبل و 2 دقیقه بعد
.\.venv\Scripts\ttd.exe investigate examples\incident-rca.jsonl --error-index 2 --minutes 10 --after-minutes 2 --output incident-demo.html --json-output incident-demo.json

# مشاهده گزارش در مرورگر
Start-Process .\incident-demo.html
```

برای انتخاب **آخرین خطای ثبت‌شده پیش از یک ساعت**:

```powershell
.\.venv\Scripts\ttd.exe investigate examples\incident-rca.jsonl --at 2026-10-15T09:53:22Z --minutes 15 --output incident-asof.html
```

برای بررسی یک Trace مشخص:

```powershell
.\.venv\Scripts\ttd.exe investigate examples\incident-rca.jsonl --trace-id trace-incident-1 --minutes 10 --output trace-incident.html
```

برای خروجی قابل‌پردازش توسط برنامه‌های دیگر:

```powershell
.\.venv\Scripts\ttd.exe investigate examples\incident-rca.jsonl --error-index 2 --format json > incident-analysis.json
```

## طرز استفاده از گزارش

- در بخش **Travel through time** با اسلایدر یا دکمه‌های قبلی/بعدی در رویدادها حرکت کنید.
- در بخش **Ranked precursor evidence** روی `View` بزنید تا به زمان شاهد انتخاب‌شده بروید.
- در **Explicit service relationships** فقط ارتباط‌هایی که شناسهٔ Trace و رابطهٔ والد/فرزند دارند نمایش داده می‌شوند؛ مجاورت دو پیام مستقل به معنای وابستگی نیست.
- پیام‌هایی که *بعد* از خطای منتخب ثبت شده‌اند برای فهم پیامدها نشان داده می‌شوند، **اما برای پیشنهاد علت احتمالی استفاده نمی‌شوند**.

## قواعد شفاف رتبه‌بندی

به‌طور تقریبی، خطا یا هشدار قبلی در همان Trace بالاتر از خطای قبلی در همان سرویس قرار می‌گیرد؛ رویداد تغییر پیکربندی/استقرار یا خطای مستقل صرفاً نزدیک در زمان، شاهد ضعیف‌تری است. هنگام نبود شواهد کافی، خروجی تصریح می‌کند که علت مشخص نیست. هر شاهد به شمارهٔ رویداد، زمان، سرویس و متن اصلی لاگ ارجاع دارد.

## دادهٔ موردنیاز

لاگ‌های JSONL، CSV، syslog، logfmt و دیگر فرمت‌های قابل خواندن در پروژه. برای تحلیل دقیق‌تر، هنگام ثبت لاگ این موارد مفیدند: `timestamp` با منطقهٔ زمانی، `level`، `service`، `trace_id`، `span_id`، `parent_span_id` و اطلاعات استقرار و تغییر نسخه. همگام‌سازی ساعت ماشین‌ها و کامل‌بودن ثبت وقایع برای درست‌بودن ترتیب زمانی ضروری است.

## استفاده با Loki

ابتدا با دستور `ttd loki`، لاگ‌ها را به JSONL تبدیل کنید، سپس همان فایل خروجی را به `ttd incidents` یا `ttd investigate` بدهید. اتصال زنده به محیط واقعی و سنجش عملکرد در مقیاس بزرگ هنوز تأیید نشده است.

## به‌روزرسانی نصب قبلی

اگر نسخهٔ قدیمی در مسیر پیش‌فرض نصب است، از فایل `UPGRADE_EXISTING_PROJECT.ps1` موجود در پوشهٔ استخراج‌شدهٔ نسخهٔ جدید استفاده کنید. این اسکریپت فقط فایل‌های نسخهٔ جدید را کپی می‌کند و پیش از جایگزینی، یک نسخهٔ پشتیبان از فایل‌های مشترک می‌سازد؛ `.venv`، `.git`، `.env` و فایل‌های ثبت لاگ شخصی را دست‌کاری نمی‌کند. اگر فایل‌های اصلی پروژه را قبلاً تغییر داده‌اید، نسخهٔ پشتیبان را پیش از ادامه بررسی کنید.

```powershell
powershell -ExecutionPolicy Bypass -File .\UPGRADE_EXISTING_PROJECT.ps1
cd D:\Github_Projects\time-travel-debugger
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
```

اگر مسیر هنوز ساخته نشده است، نصب اولیه را با `INSTALL_TO_DEFAULT_PATH.ps1` انجام دهید.

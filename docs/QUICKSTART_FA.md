# راه‌اندازی سریع RootRewind

مسیر پیش‌فرض پروژه:

`D:\Github_Projects\time-travel-debugger`

۱. فایل ZIP را باز کنید. پوشهٔ `time-travel-debugger` باید به همین نام باقی بماند.

۲. می‌توانید محتویات پروژه را مستقیماً در مسیر بالا قرار دهید یا از اسکریپت `INSTALL_TO_DEFAULT_PATH.ps1` استفاده کنید. این اسکریپت پوشهٔ خالی موجود را می‌پذیرد، اما روی پوشهٔ دارای فایل بازنویسی انجام نمی‌دهد.

۳. PowerShell را باز کنید و این دستور را اجرا کنید:

```powershell
cd D:\Github_Projects\time-travel-debugger
powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1
```

۴. برای بررسی نصب و اجرای نمونه:

```powershell
.\.venv\Scripts\rootrewind.exe --help
.\.venv\Scripts\rootrewind.exe trace examples\demo.jsonl --at 2024-10-26T14:30:01Z
.\.venv\Scripts\rootrewind.exe query examples\demo.jsonl "level=ERROR"
.\.venv\Scripts\rootrewind.exe report examples\demo.jsonl --output demo-report.html
```

۵. برای رابط تعاملی یا اتصال به MCP یا EVTX، وابستگی‌های اختیاری را نصب کنید:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[all,dev]"
```

**نکته:** هیچ پروژه‌ای بدون Python و نصب کتابخانه‌های موردنیاز اجرا نمی‌شود. به همین دلیل کدها، پوشه‌های منبع، فایل‌های نصب، تست‌ها و مثال‌ها در بسته وجود دارند، اما محیط مجازی `.venv` داخل ZIP قرار داده نشده است.


## تحلیل تاریخی علت احتمالی خطا (نسخهٔ ۱٫۱)

```powershell
.\.venv\Scripts\rootrewind.exe incidents examples\incident-rca.jsonl
.\.venv\Scripts\rootrewind.exe investigate examples\incident-rca.jsonl --error-index 2 --minutes 10 --output incident-demo.html
Start-Process .\incident-demo.html
```

برای جزئیات و نصب روی نسخهٔ قبلی، `docs/ROOT_CAUSE_ANALYSIS_FA.md` را بخوانید.

## نصب از PyPI پس از انتشار رسمی

پس از اینکه پروژه در PyPI منتشر شد، دیگران می‌توانند این دستور را اجرا کنند:

```powershell
py -3 -m pip install rootrewind
ttd --help
```

نام توزیع PyPI با نام پوشه/مخزن GitHub فرق دارد. شناسهٔ import و دستور خط فرمان همچنان `ttd` است. جزئیات ثبت و انتشار در `docs/PUBLISHING_FA.md` آمده است.
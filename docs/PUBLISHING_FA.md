# راهنمای نصب، GitHub و انتشار در PyPI

## نام‌های پروژه

- نام محصول: **RootRewind** (`rootrewind`)
- نویسنده / نگه‌دارنده: `sarahsalary`
- ایمیل عمومی نویسنده در PyPI: `saraasaalari@yahoo.com`
- مخزن GitHub: `https://github.com/sarahsalary/time-travel-debugger`
- پوشه در رایانهٔ شما: `D:\Github_Projects\time-travel-debugger`
- نام توزیع پیشنهادی در PyPI: `rootrewind` (تا زمان انتشار اول رزرو نمی‌شود)
- نام پکیج برای import: `ttd`
- دستور خط فرمان پس از نصب: `rootrewind` (نام قدیمی `ttd` نیز کار می‌کند)

**توجه:** نام `time-travel-debugger` از قبل در PyPI متعلق به پروژهٔ دیگری است. نام **RootRewind** را برای این پروژه انتخاب کرده‌ایم. جست‌وجوی عمومی نام تازه، به‌تنهایی آزادبودن قانونی یا فنی آن را تضمین نمی‌کند؛ هنگام انتشار واقعی باید امکان ثبت آن تأیید شود. اگر نام انتخابی قابل ثبت نبود، پیش از انتشار مقادیر `project.name`، مستندات و نام توزیع را با هم اصلاح کنید.

**حریم خصوصی:** بنا به درخواست صاحب پروژه، نشانی `saraasaalari@yahoo.com` عمداً در اطلاعات نویسنده و نگه‌دارنده قرار دارد و بعد از انتشار در صفحهٔ PyPI و کد عمومی قابل مشاهده خواهد بود.

## ۱. نصب اولیه در ویندوز

اگر پوشهٔ مقصد خالی است، ZIP را باز کنید و از محل پوشهٔ بازشده اجرا کنید:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\INSTALL_TO_DEFAULT_PATH.ps1
```

یا تمام **محتوای داخل پوشهٔ** `time-travel-debugger` فایل ZIP را مستقیماً به مسیر زیر انتقال دهید (دقت کنید پوشهٔ تو در تو ایجاد نشود):

```text
D:\Github_Projects\time-travel-debugger
```

حالا در PowerShell اجرا کنید:

```powershell
cd D:\Github_Projects\time-travel-debugger
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_windows.ps1
.\.venv\Scripts\rootrewind.exe --help
.\scripts\run_rca_demo.bat
```

برای ابزارهای اختیاری، از محیط مجازی همان پروژه استفاده کنید:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[all,dev]"
```

## ۲. انتشار منبع در GitHub

ابتدا در `https://github.com/new` مخزن عمومی `time-travel-debugger` زیر حساب `sarahsalary` را بدون README/License پیش‌ساخته ایجاد کنید.

قبل از انتشار، مطمئن شوید تنظیمات نام و ایمیل Git مخصوص شماست؛ ایمیل نویسنده طبق درخواست شما داخل پروژه عمومی است؛ برای ایمیلِ ثبت commitها، در صورت تمایل، نشانی GitHub noreply را به‌صورت جداگانه تنظیم کنید. سپس:

```powershell
cd D:\Github_Projects\time-travel-debugger
git init -b main
git status --short
git add .
git diff --cached --stat
git diff --cached --check
git commit -m "Release RootRewind v1.1.2"
git remote add origin https://github.com/sarahsalary/time-travel-debugger.git
git push -u origin main
```

قبل از `git commit` محتوا را چک کنید؛ خصوصاً `.env`، کلیدها و داده‌های واقعی را نباید منتشر کنید. برای حساب‌های GitHub که از پیش مخزن ایجاد کرده‌اند، دستورات `git init` و `git remote add` را با وضعیت فعلی هماهنگ کنید.

## ۳. ساخت توزیع و تست نصب از فایل Wheel

```powershell
cd D:\Github_Projects\time-travel-debugger
.\.venv\Scripts\python.exe -m pip install --upgrade build twine
.\.venv\Scripts\python.exe -m build
.\.venv\Scripts\python.exe -m twine check dist\*
```

آرشیوهای `dist/*.whl` و `dist/*.tar.gz` ساخته می‌شوند. برای بررسی مستقل نصب فایل Wheel، یک محیط مجازی تمیز بسازید و Wheel را نصب کنید (برای اجرای آنلاین به اینترنت برای نصب وابستگی‌ها نیاز است).

## ۴. انتشار امن و دستی با GitHub Actions (پیشنهادی)

در PyPI و TestPyPI وارد حساب خود شوید. به `https://pypi.org/manage/account/publishing/` و `https://test.pypi.org/manage/account/publishing/` بروید. برای پروژهٔ جدید، **Pending Trusted Publisher** بسازید. مقدارهای هر دو سرویس:

| فیلد | PyPI | TestPyPI |
|---|---|---|
| Project name | `rootrewind` | `rootrewind` |
| GitHub owner | `sarahsalary` | `sarahsalary` |
| Repository | `time-travel-debugger` | `time-travel-debugger` |
| Workflow filename | `publish.yml` | `publish.yml` |
| Environment | `pypi` | `testpypi` |

در تنظیمات مخزن GitHub نیز دو Environment به نام‌های `pypi` و `testpypi` بسازید، ترجیحاً برای `pypi` تأیید دستی فعال کنید. فایل workflow در `.github/workflows/publish.yml` است و **فقط از طریق اجرای دستی** GitHub Actions فعال می‌شود، نه با هر Push.

در GitHub: `Actions` > `Publish to PyPI / TestPyPI` > `Run workflow` > ابتدا `testpypi` و پس از بازبینی `pypi` را انتخاب کنید. برای هر مخزن و نام پروژه، ناشر مورد اعتماد باید دقیقاً مطابق این اطلاعات ثبت شده باشد. در صورت اشغال‌شدن نام پکیج، قبل از اولین انتشار همهٔ مشخصات نام را هماهنگ کنید.

**نکته:** TestPyPI حساب مجزا از PyPI می‌خواهد. فایل‌هایی که یک‌بار با نسخهٔ مشخص در PyPI منتشر شدند، قابل جایگزینی نیستند؛ برای نسخهٔ بعد عدد version را بالا ببرید.

**تفاوت PyPI و GitHub:** GitHub کد منبع را میزبانی می‌کند و PyPI بستهٔ قابل نصب از طریق pip را منتشر می‌کند. این پروژه به PayPal مرتبط نیست.

**راهنمای رسمی:** https://docs.pypi.org/trusted-publishers/ و https://packaging.python.org/en/latest/tutorials/packaging-projects/

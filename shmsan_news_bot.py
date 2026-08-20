"""
shmsan_news_bot.py
──────────────────────────────────────────────────────────────────────
نسخة مُهيَّأة لموقع "شمسان نيوز" (shmsan-news-hub) من بوت الجنوب فويس
الأصلي — نفس المنطق والوظائف حرفياً، مع تعديل طبقة الكتابة بـ Supabase
لتطابق مخطط جدول posts الفعلي بشمسان نيوز (category_id/cover_image/
is_featured/is_opinion/seo_title/seo_description بدل category/image_url/
featured/author/meta_title/meta_description بالجنوب فويس).
يسحب أخبار آخر 24 ساعة من روابط RSS محددة، يعيد صياغتها عبر Gemini،
وينشرها تلقائياً في جدول posts.
🆕 وضع "1" (استخراج كامل) يجمع الآن 3 مصادر حية: عدن تايم + فيد الجزيرة نت
العام (مفلتَر تلقائياً لأخبار اليمن فقط عبر RSS_ALJAZEERA_YEMEN_URL) +
المساء برس.

المتطلبات:
    pip install requests pillow beautifulsoup4

طريقة الاستخدام:
    1. عبّئ SUPABASE_SERVICE_KEY بالأسفل بمفتاح service_role الفعلي
       (لوحة Supabase → Project Settings → API Keys). عمود source_url
       بجدول posts مُضاف مسبقاً (طُبِّق مباشرة على قاعدة بيانات شمسان
       نيوز)، فما تحتاج تنفيذ أي SQL يدوياً.
    2. عبّئ قسم RSS_FEED_CATEGORIES بمسارات ملفاتك (تحت شمسان_bot).
    3. شغّل: python shmsan_news_bot.py
       سيعرض تحليلاً أولاً (كم خبر جديد وجد) ثم يطلب كتابة 'تأكيد' قبل النشر الفعلي.
"""

from __future__ import annotations

import difflib
import html
import io
import json
import logging
import math
import os
import random
import re
import string
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup, Tag

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    Image = None
    ImageDraw = None
    ImageFont = None

# تستخدم لمطابقة صور الحظر المرجعية فقط؛ لا علاقة لها بمصدر الخبر أو نطاق الصورة.
try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover
    cv2 = None
    np = None

try:
    import arabic_reshaper
    from bidi.algorithm import get_display as _bidi_get_display
except ImportError:  # pragma: no cover
    arabic_reshaper = None
    _bidi_get_display = None

# ══════════════════════════════════════════════════════════════════════
#  ⚙️  الإعدادات — عدّل هنا فقط
# ══════════════════════════════════════════════════════════════════════

# روابط/ملفات RSS التي تريد السحب منها — كل ملف مربوط بقسمه الصحيح مباشرة
# (كل ملف = لقطة ثابتة من قسم واحد محدد بموقع "عدن تايم"، فما فيه حاجة
# لتصنيف يدوي أو تخمين بالذكاء الاصطناعي — القسم معروف مسبقاً لكل ملف).
RSS_FEED_CATEGORIES = {
    "/storage/emulated/0/Download/shmsan_bot/aden-tm-akhbar-wataqarir.xml": "أخبار وتقارير",
    "/storage/emulated/0/Download/shmsan_bot/aden-tm-akhbar-aden.xml": "أخبار عدن",
    "/storage/emulated/0/Download/shmsan_bot/aden-tm-riyada.xml": "رياضة",
    "/storage/emulated/0/Download/shmsan_bot/aden-tm-sarf.xml": "أسعار العملات والذهب",
    "/storage/emulated/0/Download/shmsan_bot/aden-tm-kitabat.xml": "آراء واتجاهات",
}

# (للتوافق فقط — الكود الفعلي يستخدم RSS_FEED_CATEGORIES بالأعلى)
RSS_FEEDS = list(RSS_FEED_CATEGORIES.keys())

# رابط RSS "المساء" الإضافي — مصدر منفصل عن ملفات XML المحلية بالأعلى
# (يُنسب تلقائياً لقسم "أخبار وتقارير" عند السحب منه)
RSS_MASA_URL = "https://masa-press.net/category/اهم-الاخبار/feed/"
RSS_MASA_CATEGORY = "أخبار وتقارير"

# رابط RSS الحي لموقع عدن تايم (مباشر من الإنترنت، وليس ملف XML محلي ثابت) —
# يُستخدم حصراً بوضع "1" (استخراج الخبر كاملاً): البوت يسحب روابط الأخبار من
# هذا الفيد، ثم يفتح كل رابط فعلياً عبر extract_article ليجلب النص الكامل
# من صفحة الخبر نفسها بدل الاكتفاء بملخص الفيد.
RSS_ADEN_TM_FULL_URL = "https://www.aden-tm.net/feed"
RSS_ADEN_TM_FULL_CATEGORY = "أخبار وتقارير"

# رابط RSS الحي العام لموقع الجزيرة نت (aljazeera.net) — نفس منطق عدن تايم
# حرفياً: البوت يسحب كل الأخبار من هذا الفيد (عام، كل الأقسام)، ثم يفتح كل
# رابط فعلياً عبر extract_article ليجلب النص الكامل من صفحة الخبر نفسها.
# ⚠️ لا يوجد فيد RSS مخصص لقسم "اليمن" وحده بموقع الجزيرة — فقط فيد عام،
# فالفلترة لقسم اليمن تحديداً تتم لاحقاً بفحص كل خبر بعد فتح صفحته (شوف
# apply_full_extraction ودالة _detect_aljazeera_where): أي خبر لا يحمل
# "اليمن" ضمن تصنيف الجغرافيا الفعلي بصفحته (وسم <meta name="where">) يُستبعد
# تماماً من النشر، تماماً كما تُستبعد أخبار عدن تايم ذات القسم غير المعروف.
RSS_ALJAZEERA_YEMEN_URL = "https://www.aljazeera.net/aljazeerarss/a7c186be-1baa-4bd4-9d80-a84db769f779/73d0e1b4-532f-45ef-b135-bfdff8b8cab9"
RSS_ALJAZEERA_YEMEN_CATEGORY = "أخبار اليمن"

# كلمات محظورة — أي خبر من ملفات XML المحلية يحتوي إحداها (بالعنوان أو النص)
# يُتجاوز بالكامل: لا يُرسل لـ Gemini، ولا تُعاد صياغته، ولا يُنشر.
# لا تُطبَّق هذه الفلترة على مصدر RSS المساء (RSS_MASA_URL) — مسموح بدونها.
BLOCKED_KEYWORDS = ["حوثي", "إيران", "مواقيت الأذان", "مليشيا", "مليشيات", "المليشيا"]

# 🚫 عبارات/نشرات متكررة تُستبعد نهائياً بأي صيغة كتابة (اختلاف الهمزات/
# التشكيل/المسافات/التطويل)، كل عبارة كمجموعة كلمات مفتاحية (لازم توجد كلها
# معاً بعد تطبيع النص). ⚠️ بخلاف BLOCKED_KEYWORDS، هذا الحظر يُطبَّق على كل
# الأخبار من كل الأقسام وكل المصادر بلا استثناء (حتى RSS المساء) — أي خبر
# يطابق إحدى هذه المجموعات لا يُنشر نهائياً مهما كان قسمه أو مصدره.
BLOCKED_BULLETIN_PHRASE_GROUPS = [
    ["تصفح", "العدد", "الالكتروني", "عدن", "تايم", "الورقيه"],  # إعلان تصفح العدد الإلكتروني لعدن تايم الورقية
    ["اقلاع", "رحلات", "طيران"],  # مواعيد إقلاع رحلات طيران اليمنية
    ["اسعار", "صرف", "الريال"],  # أسعار صرف الريال اليمني
    ["نشره", "اسعار", "الذهب"],  # النشرة اليومية لأسعار الذهب
    ["نشره", "اسعار", "صرف", "العملات"],  # نشرة أسعار صرف العملات الأجنبية
    ["توقعات", "حاله", "الطقس"],  # توقعات حالة الطقس
    ["مواقيت", "الاذان"],  # مواقيت الأذان
    ["مستجدات", "كهرباء"],  # مستجدات كهرباء عدن
    ["اعتراض", "حوثي", "سعوديه"],  # اعتراض صواريخ حوثية باتجاه السعودية
]

_BLOCKED_PHRASE_DIACRITICS_RE = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670\u06D6-\u06ED]")


def _normalize_ar_for_blocking(text: str) -> str:
    t = text or ""
    t = t.replace("ـ", "")  # إزالة التطويل
    t = _BLOCKED_PHRASE_DIACRITICS_RE.sub("", t)  # إزالة التشكيل
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ى", "ي").replace("ة", "ه")
    t = re.sub(r"\s+", " ", t)
    return t

# ملف يخزّن روابط الأخبار التي اخترت منعها نهائياً عبر choose_excluded_items
# (رقم الخبر أثناء التشغيل). يُقرأ في بداية كل تشغيل جديد ليُستبعد أي خبر
# رابطه موجود هنا تلقائياً، حتى لو لم يُنشر أبداً بجدول posts (وبالتالي لا
# يظهر ضمن existing_urls). هذا مستقل تماماً عن Supabase.
# BOT_DATA_DIR: مجلد بيانات محلي قابل للتهيئة عبر متغير بيئة — يعمل على
# Termux (مرّر المسار القديم كمتغير بيئة لو أردت الإبقاء عليه) وعلى Render
# (حيث لا يوجد قرص دائم أصلاً، فهذا المجلد يُعاد إنشاؤه فارغاً كل تشغيلة —
# هذا متوقع ولا يؤثر على منع التكرار الفعلي، المعتمد على Supabase مباشرة).
BASE_DIR = os.environ.get(
    "BOT_DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "shmsan_data"),
)
os.makedirs(BASE_DIR, exist_ok=True)

BLOCKED_LINKS_FILE = os.path.join(BASE_DIR, "blocked_links.json")

# يتتبّع الأخبار المجدولة (status=scheduled) اللي لسا ما اتأكدنا من نشرها
# فعلياً ولا أرسلنا رابطها لتيليجرام بعد — يُفحص هذا الملف بأول كل تشغيلة
# جديدة للسكربت (شوف check_and_notify_scheduled_posts)
PENDING_SCHEDULED_FILE = os.path.join(BASE_DIR, "pending_scheduled.json")

# بيانات Supabase الخاصة بموقع شمسان نيوز
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError(
        "⛔ متغيرات البيئة SUPABASE_URL / SUPABASE_SERVICE_KEY غير مضبوطة. "
        "أضفهما من لوحة Render (Environment) قبل التشغيل."
    )
# ⚠️ لازم يكون هذا تحديداً service_role key (مو anon/publishable)، لأن جدول
# posts بمشروع شمسان نيوز عليه RLS مفعّل (rls_enabled=true) بدون Policy تسمح
# بالإدخال العام. تجده بلوحة Supabase: Project Settings → API Keys → service_role
# (سرّي — لا تشاركه إلا بالبوت نفسه). بدونه ستفشل كل عمليات sb_insert بخطأ 401/403.

# اختر رقم القسم عند التشغيل (لا يوجد جدول categories بهذا الموقع — القسم نص مباشر)
# يُستخدم فقط في الوضع اليدوي (لو اخترت N عند سؤال "تفعيل التلقائي؟")
CATEGORY_OPTIONS = {
    "1": "أخبار وتقارير",
    "2": "شؤون دولية",
    "3": "آراء واتجاهات",
    "4": "رياضة",
    "5": "أسعار العملات والذهب",
    "6": "أخبار عدن",
    "7": "أخبار اليمن",
    "8": "أخبار محلية",
    "9": "منوعات",
}

# أي خبر يُنشر بأحد هذه الأقسام يُعلَّم تلقائياً ⭐ مميز (featured=true) ليظهر
# بسلايدر الصفحة الرئيسية — تطابقاً مع Home.tsx (posts.filter(p => p.featured))
FEATURED_SLIDER_CATEGORIES = {"أخبار وتقارير", "أسعار العملات والذهب"}

# مسارات ملفات أخبار جاهزة (اختياري) — ملف نصي فيه عدة أخبار مفصولة بعناوين مرقّمة
# مثال شكل الملف:
#   1 - عنوان الخبر الأول
#   نص الخبر الأول...
#
#   2 - عنوان الخبر الثاني
#   نص الخبر الثاني...
NEWS_FILES = [
    # "/path/to/اخبار.txt",
]

# النافذة الزمنية: أخبار آخر كم ساعة تُسحب
HOURS_WINDOW = 24

# الأقسام المستثناة من إعادة الصياغة الكاملة عبر Gemini — تُنشر بنص مقالها
# الأصلي حرفياً (بدون أي تعديل) مع نسب المقال لكاتبه (حقل author)، لأنها
# مقالات رأي منسوبة لكتّاب بأسمائهم ولا يجوز التعديل على متنها. العنوان فقط
# يُعاد صياغته عبر Gemini (rewrite_title_only) ليتماشى مع الهوية التحريرية،
# دون المساس بأي حرف من نص المقال نفسه.
# ⚠️ يعتمد الاستثناء على القسم الأصلي لملف RSS المصدر (RSS_FEED_CATEGORIES)
# ويُطبَّق في الوضعين التلقائي واليدوي معاً.
NO_REWRITE_CATEGORIES = {"آراء واتجاهات"}

# ⚠️ طريقة التعامل مع Gemini لكل الفيدات وكل الأقسام (ما عدا مقالات الرأي
# أعلاه، اللي تبقى بمنطقها الثابت دائماً) أصبحت سؤالاً تفاعلياً عند كل
# تشغيل عبر choose_gemini_mode() (انظر أسفل الملف): 1) عنوان+متن عبر Gemini
# (الافتراضي) 2) عنوان فقط عبر Gemini 3) بدون Gemini إطلاقاً (حرفي كما استُخرج).

# الأقسام التي تُنشر أخبارها دائماً بدون صورة (يُترك حقل image_url فارغاً
# ولا تُجلب/تُضغط/تُرفع أي صورة لها، حتى لو توفّر رابط صورة بالمصدر).
NO_IMAGE_CATEGORIES = {"أسعار العملات والذهب"}

# اسم افتراضي يُستخدم لو ملف الـ RSS ما فيه اسم كاتب صريح لمقال رأي
DEFAULT_OPINION_AUTHOR = "كتّاب عدن تايم"

# ⚠️ لا يوجد عمود "source" بجدول posts بمشروع شمسان نيوز (كان موجوداً بالجنوب
# فويس فقط)، فتم حذف SOURCE_LABEL واستخدامه بالكامل — لا يوجد مكان نضعه فيه.

# 🗂️ جدول شمسان نيوز يخزّن القسم كـ category_id (UUID) عبر جدول categories
# منفصل، وليس نصاً حراً كما بالجنوب فويس. هذا القاموس يحوّل اسم القسم (نفس
# الأسماء المستخدمة أعلاه بكل الثوابت: RSS_FEED_CATEGORIES/CATEGORY_OPTIONS/
# FEATURED_SLIDER_CATEGORIES/NO_IMAGE_CATEGORIES/NO_REWRITE_CATEGORIES) إلى
# اسم القسم الموجود فعلياً بجدول categories بشمسان نيوز، فقط للأسماء التي
# لا تطابق حرفياً (كل الأسماء الأخرى مطابقة 1:1 فتُترك كما هي بدون تحويل).
# ⚠️ لا يوجد بشمسان نيوز قسم مخصص لـ"أسعار العملات والذهب" (الموقع يعرضها
# كـ widget أسعار منفصل بالواجهة وليس كأخبار قسم) — لذلك حوّلتها افتراضياً
# لقسم "شمسان اليوم" (القسم المحلي/الإخباري الرئيسي). لو تبي قسماً مختلفاً
# غيّر القيمة هنا فقط.
CATEGORY_NAME_REMAP = {
    "أسعار العملات والذهب": "شمسان اليوم",
    "أخبار عدن": "أخبار وتقارير",
    "أخبار محلية": "أخبار وتقارير",
}

_CATEGORY_ID_CACHE: dict[str, str] = {}


def get_category_id(category_name: str) -> Optional[str]:
    """يرجّع id القسم من جدول categories بشمسان نيوز حسب الاسم (مع تطبيق
    CATEGORY_NAME_REMAP أولاً لو الاسم غير مطابق مباشرة). يُحمَّل جدول
    categories كاملاً مرة واحدة فقط بأول استدعاء ويُخزَّن بالذاكرة."""
    if not _CATEGORY_ID_CACHE:
        url = f"{SUPABASE_URL}/rest/v1/categories"
        try:
            r = requests.get(url, headers=sb_headers(), params={"select": "id,name"},
                              timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                for row in r.json():
                    _CATEGORY_ID_CACHE[row["name"]] = row["id"]
            else:
                log.error(f"❌ تعذّر جلب جدول categories [{r.status_code}]: {r.text[:200]}")
        except requests.RequestException as e:
            log.error(f"❌ فشل الاتصال أثناء جلب جدول categories: {e}")

    lookup_name = CATEGORY_NAME_REMAP.get(category_name, category_name)
    category_id = _CATEGORY_ID_CACHE.get(lookup_name)
    if not category_id:
        log.error(f"❌ القسم «{category_name}» (بحث عن «{lookup_name}») غير موجود بجدول "
                  "categories بشمسان نيوز — سيُتخطى الخبر بدل نشره بدون قسم.")
    return category_id


TABLE_NAME = "posts"
MAX_RETRIES = 6
MAX_BACKOFF = 60
REQUEST_TIMEOUT = 60

# ══════════════════════════════════════════════════════════════════════
#  🖼️  إعدادات معالجة ورفع صور الأخبار (Supabase Storage)
# ══════════════════════════════════════════════════════════════════════

SUPABASE_IMAGE_BUCKET = "media"   # اسم الـ bucket العام في Supabase Storage (شمسان نيوز)
IMAGE_MAX_DIMENSION = 1200              # أقصى عرض/ارتفاع بالبكسل
IMAGE_TARGET_MAX_BYTES = 100 * 1024     # 100 كيلوبايت
IMAGE_START_QUALITY = 85                # جودة WebP الابتدائية
IMAGE_MIN_QUALITY = 30                  # أدنى جودة مسموحة أثناء الضغط التدريجي
IMAGE_QUALITY_STEP = 5                  # مقدار تقليل الجودة بكل محاولة

# 🖼️² نسخة مربّعة إضافية (1:1) من نفس الصورة — تُستخدم بـ NewsArticle schema
# (image array) لجوجل، وأصبحت الآن أيضاً تُملأ فعلياً في عمود thumbnail_image
# (بعد تحديث الموقع بتاريخ 2026-07-31) وتُعرض في كل بطاقات القوائم بالرئيسية/
# الأقسام/الأخبار ذات الصلة بدل الصورة الكاملة.
# ⚠️ تكلفة إضافية حقيقية على مساحة Supabase المجانية (1GB): بمتوسط ~100KB
# للصورة الأصلية + ~50KB لهذي النسخة = ~150KB/خبر بدل 100KB. يعني نقصان سقف
# عدد الأخبار المخزّنة من ~10,000 إلى ~7,000 صورة تقريباً قبل امتلاء الـ 1GB.
# بالمقابل: توفير كبير جداً على Database/Storage Egress، لأن كل زيارة للرئيسية
# أو قسم تحمّل عشرات الصور دفعة وحدة — التوفير هناك أكبر بكثير من كلفة
# المساحة هنا. إن حبيت توقفها لاحقاً لتوفير المساحة، خلي القيمة False فقط
# (المقالات القديمة برضو ترجع تلقائياً للصورة الكاملة بالواجهة عند غياب
# thumbnail_image، فإيقافها لا يكسر شي).
GENERATE_SQUARE_IMAGE_VARIANT = False
IMAGE_SQUARE_DIMENSION = 600            # مربّع 600×600 (كافي لأي thumbnail/schema)
IMAGE_SQUARE_TARGET_MAX_BYTES = 50 * 1024  # 50 كيلوبايت — أصغر من الأصلية عمداً
IMAGE_SQUARE_START_QUALITY = 80
IMAGE_SQUARE_MIN_QUALITY = 30
IMAGE_SQUARE_QUALITY_STEP = 5

# ══════════════════════════════════════════════════════════════════════
#  🖋️  العلامة المائية (شعار الجنوب فويس فوق الصورة) — اختيارية لكل خبر
# ══════════════════════════════════════════════════════════════════════

# مسار ملف الشعار (PNG شفاف) على الجهاز — نفس sail-logo.png المستخدم
# بالضبط بأداة العلامة المائية اليدوية بالموقع (imageWatermark.ts). لو الملف
# غير موجود بهذا المسار، تُتجاوز العلامة المائية تلقائياً بدون إيقاف البوت.
# شعار العلامة المائية — ضع sail-logo.png بجذر المستودع نفسه (بجانب هذا
# الملف) وادفعه لـGit حتى يعمل على Render؛ لو غير موجود، ينشر بدون شعار
# (تعامل آمن، شوف get_post_image_url).
WATERMARK_LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sail-logo.png")

# ⚠️ نفس القيم الأربعة حرفياً بملف src/lib/imageWatermark.ts بالموقع —
# أي تعديل هنا لازم ينعكس هناك وإلا صارت الصورة المائية شكلها مختلف حسب
# مصدر النشر (يدوي عبر Admin Panel أو آلي عبر البوت).
WATERMARK_OG_WIDTH = 1200
WATERMARK_OG_HEIGHT = 630
WATERMARK_LOGO_SIZE_PERCENT = 0.12   # عرض الشعار = 12% من عرض الصورة
WATERMARK_PADDING_PERCENT = 0.03     # الهامش من الحواف = 3% من عرض الصورة
WATERMARK_LOGO_OPACITY = 0.85        # شفافية الشعار (0-1)

# ══════════════════════════════════════════════════════════════════════
#  📰  تصميم شريط العنوان فوق صورة الخبر (بديل/إضافة للعلامة المائية)
# ══════════════════════════════════════════════════════════════════════

# فعّلها لتوليد صورة مصممة (صورة + شريط منحني فيه عنوان الخبر) بدل الصورة
# العادية أو العلامة المائية البسيطة. تحتاج تثبيت: pip install arabic-reshaper python-bidi
HEADLINE_DESIGN_ENABLED = True

# خط عربي Bold يدعم العربية (Amiri-Bold / Cairo-Bold / Tajawal-Bold...)
# نفس مبدأ WATERMARK_LOGO_PATH: ضع مجلد fonts/Amiri-Bold.ttf بجذر المستودع
# وادفعه لـGit حتى يعمل على Render (لو غير موجود: تصميم صورة العنوان
# يُتخطى فقط، بدون توقف السكربت — شوف السطر أدناه في الكود الأصلي).
HEADLINE_FONT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fonts", "Amiri-Bold.ttf"
)
HEADLINE_SITE_NAME = "شمسان نيوز"

# 🎨 الهوية اللونية الفعلية لموقع aljnoubvoice.com (كحلي الهيدر + أحمر «عاجل»)
# — نفس الألوان المستخرجة حرفياً من الموقع. أسلوب القنوات العالمية: خط
# علوي مستقيم + تدرّج تعتيم ناعم فوق الصورة بدل الموجة، وفاصل رفيع خفيف
# بين كتلة الشعار وكتلة العنوان بدل خط عمودي ثقيل بين الشعار والاسم.
HEADLINE_BAND_COLOR_TOP = (15, 23, 42, 255)      # كحلي الهيدر (أعلى الشريط)
HEADLINE_BAND_COLOR_BOTTOM = (26, 43, 73, 255)   # كحلي أفتح قليلاً (أسفل الشريط)
HEADLINE_CURVE_COLOR = (195, 16, 45, 255)        # أحمر «عاجل» — الخط المستقيم العلوي للشريط
HEADLINE_TEXT_COLOR = (248, 248, 246, 255)       # أبيض للعنوان (وضوح على الكحلي)
HEADLINE_SITE_COLOR = (196, 20, 46, 255)         # أحمر ثابت (بدون تدرّج) لاسم الموقع
HEADLINE_DIVIDER_COLOR = (255, 255, 255, 60)     # فاصل رفيع خفيف بين كتلة الشعار وكتلة العنوان
HEADLINE_ACCENT_COLOR = (195, 16, 45, 255)       # أحمر — الشريط العمودي الصغير (kicker) جنب العنوان
HEADLINE_LINE_THICKNESS = 3                      # سمك الخط المستقيم العلوي
HEADLINE_FADE_H = 90                             # ارتفاع تدرّج التعتيم فوق الشريط (يذوب داخل الصورة)
HEADLINE_FONT_SIZE = 54
HEADLINE_FONT_MIN_SIZE = 30       # أصغر حجم خط مسموح قبل تكبير الشريط بدل قصّ النص
HEADLINE_MAX_LINES = 2            # عدد الأسطر "المفضّل" — العنوان الطويل يتجاوزه بدل أن يُقصّ
HEADLINE_MIN_PHOTO_VISIBLE = 90   # أقل ارتفاع من الصورة الأصلية يبقى ظاهراً فوق الشريط دائماً

# ══════════════════════════════════════════════════════════════════════
#  🚫🖼️  فحص شعار المصدر بالصورة (مطابقة محلية بدون أي API خارجي)
# ══════════════════════════════════════════════════════════════════════


# مجلد تحفظ فيه صور كاملة عليها شعار عدن تايم/المساء برس (الصورة كاملة كما
# تُنشر، وليس مقصوصة). أي صورة (jpg/jpeg/png/webp) تضعها هنا تُقارَن تلقائياً
# بكل صورة خبر جديدة (كاملة، مقابل كاملة) قبل رفعها. لو ما فيه أي صورة
# بالمجلد، الفحص يُتجاوز تلقائياً وتُنشر الصور عادي (بدون توقف البوت).
# ⚠️ هذا المجلد جزء من الريبو نفسه (بجانب السكربت) — لازم يُقرأ من مسار
# السكربت مباشرة، وليس من BASE_DIR (الذي قد يُعاد توجيهه بمتغير BOT_DATA_DIR
# أو يُنشأ فارغاً على Render بدون قرص دائم، فيفقد صور الحماية بالخطأ).
BLOCKED_LOGOS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "blocked_logos"
)

# جدول Supabase اختياري لإضافة بصمات شعارات محظورة إضافية عن بُعد (بدون
# الحاجة لإعادة نشر الريبو) — يُدمَج مع صور BLOCKED_LOGOS_DIR المحلية معاً،
# وأي فشل بجلبه (مشكلة اتصال، الجدول غير موجود...) لا يوقف الفحص المحلي.
BLOCKED_LOGO_HASHES_TABLE = "blocked_logo_hashes"

# حجم "البصمة المرئية" (average hash) — 8 يعني مقارنة على أساس 64 بت
LOGO_HASH_SIZE = 8

# أقصى فرق مسموح بين بصمتين ليُعتبرا "نفس الشعار" (من أصل 64 بت)
LOGO_MATCH_MAX_DISTANCE = 6

# ══════════════════════════════════════════════════════════════════════
#  📢  تليجرام — إرسال العنوان والرابط تلقائياً بعد كل نشر
# ══════════════════════════════════════════════════════════════════════

TELEGRAM_ENABLED = True
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = "@shmsannews"

# 🔔 محادثة خاصة (وليست القناة العامة) لإرسال تنبيهات تقنية داخلية فقط
# (مثل تجاوز حجم جداول النظام cron/net لحد معيّن). لا علاقة لها بنشر الأخبار.
ADMIN_TELEGRAM_CHAT_ID = "3967444230"
SYSTEM_LOGS_ALERT_THRESHOLD = 50_000  # سجل — حد التنبيه لكل من الجدولين

# ⚠️ تم حذف SITE_SHARE_URL_BASE (كان يشير لمسار /share غير موجود بالموقع
# فعلاً، فكل رابط يُرسل لتيليجرام كان يفتح صفحة 404). الرابط الصحيح الآن
# يُبنى عبر build_canonical_url() بنفس صيغة الموقع: /YYYY/MM/DD/slug


def send_to_telegram(title: str, article_url: str) -> bool:
    if not TELEGRAM_ENABLED or not TELEGRAM_BOT_TOKEN:
        return False
    text = (
        f"{title}\n\n"
        f'أقرأ التفاصيل من "شمسان نيوز": {article_url}\n\n'
        f"📲 تابعونا على:  ⤵\n\n"
        f"✅ تيليجرام: https://t.me/shmsannews"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHANNEL_ID, "text": text}
    try:
        r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            return True
        log.warning(f"  ⚠️  فشل إرسال تليجرام [{r.status_code}]: {r.text[:200]}")
        return False
    except requests.RequestException as e:
        log.warning(f"  ⚠️  خطأ إرسال تليجرام: {e}")
        return False


def send_admin_alert(text: str) -> bool:
    """يرسل رسالة تنبيه تقني لمحادثة خاصة (ADMIN_TELEGRAM_CHAT_ID) — منفصلة
    تماماً عن قناة نشر الأخبار العامة. لا توقف تشغيل البوت أبداً لو فشلت."""
    if not TELEGRAM_ENABLED or not TELEGRAM_BOT_TOKEN or not ADMIN_TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": ADMIN_TELEGRAM_CHAT_ID, "text": text}
    try:
        r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            return True
        log.warning(f"  ⚠️  فشل إرسال تنبيه إداري [{r.status_code}]: {r.text[:200]}")
        return False
    except requests.RequestException as e:
        log.warning(f"  ⚠️  خطأ إرسال تنبيه إداري: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════
#  👁️  تحديث المشاهدات + 📡 الأرشفة (Google Indexing) — نفس آلية لوحة التحكم
# ══════════════════════════════════════════════════════════════════════

AUTO_SEED_VIEWS = True


def seed_views(post_id: str) -> None:
    """نسخة طبق الأصل من دالة seedViewsForPost بلوحة تحكم الموقع (AdminPanel.tsx):
    تجيب views + created_at + category الحاليين للخبر، وتحسب المشاهدات حسب عمر الخبر
    (diffMin = الفرق بالدقائق بين الآن و created_at) بدل رقم ثابت، مع فرع مختلف
    حسب القسم (نفس فرع الموقع بالضبط):
        - قسم "أخبار وتقارير": عمر < 60 دقيقة → 150-388 | 60-300 → 455-700 | >300 → 600-1500
        - باقي الأقسام: عمر < 60 دقيقة → 126-250 | 60-300 → 251-450 | >300 → 451-683
      (لو كانت عنده مشاهدات سابقة ≥ الحد الأدنى لقسمه: يُضاف لها زيادة صغيرة، بدل استبدالها)."""
    if not AUTO_SEED_VIEWS or not post_id:
        return
    try:
        # ⚠️ شمسان نيوز: لا يوجد عمود "category" نصي (فقط category_id UUID)،
        # فنجيب category_id ونقارنه بمعرّف قسم "أخبار وتقارير" المحلول عبر
        # get_category_id (نفس القسم المستخدم بالمنطق الأصلي بالضبط). كذلك
        # المرجع الزمني الصحيح لعمر الخبر هو published_at (وليس created_at).
        url = (f"{SUPABASE_URL}/rest/v1/{TABLE_NAME}?id=eq.{post_id}"
               "&select=id,views,published_at,category_id")
        r = requests.get(url, headers=sb_headers(), timeout=REQUEST_TIMEOUT)
        if r.status_code != 200 or not r.json():
            log.warning(f"  ⚠️  تعذّر جلب بيانات الخبر لتحديث المشاهدات [{r.status_code}]")
            return
        post = r.json()[0]
        current = post.get("views") or 0
        published_at = datetime.fromisoformat(post["published_at"].replace("Z", "+00:00"))
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        diff_min = (now - published_at).total_seconds() / 60

        is_news_reports = post.get("category_id") == get_category_id("أخبار وتقارير")

        if is_news_reports:
            # المنطق الأصلي: يُطبّق فقط على قسم "أخبار وتقارير"
            if current < 150:
                if diff_min < 60:
                    final = random.randint(150, 388)
                elif diff_min < 300:
                    final = random.randint(455, 700)
                else:
                    final = random.randint(600, 1500)
            else:
                final = current + random.randint(10, 59)
        else:
            # نفس بنية المنطق (تقسيم زمني ثلاثي) لكن بنطاقات مصغّرة ضمن 126-683 لباقي الأقسام
            if current < 126:
                if diff_min < 60:
                    final = random.randint(126, 250)
                elif diff_min < 300:
                    final = random.randint(251, 450)
                else:
                    final = random.randint(451, 683)
            else:
                final = min(683, current + random.randint(5, 25))

        patch_url = f"{SUPABASE_URL}/rest/v1/{TABLE_NAME}?id=eq.{post_id}"
        pr = requests.patch(patch_url, headers=sb_headers(), json={"views": final}, timeout=REQUEST_TIMEOUT)
        if pr.status_code not in (200, 204):
            log.warning(f"  ⚠️  فشل تحديث المشاهدات [{pr.status_code}]: {pr.text[:200]}")
        else:
            log.info(f"  👁️  تحسين المشاهدات ({final})")
    except (requests.RequestException, ValueError, KeyError) as e:
        log.warning(f"  ⚠️  خطأ تحديث المشاهدات: {e}")


YEMEN_TZ = timezone(timedelta(hours=3))  # توقيت اليمن (Asia/Aden) — لا يوجد توقيت صيفي


SITE_DOMAIN = "https://shmsannews.com"  # دومين شمسان نيوز


def build_canonical_url(slug: str, published_at_iso: str) -> str:
    """يبني رابط المقال الرسمي (نفس صيغة articlePath بملف src/lib/news.types.ts
    بموقع شمسان نيوز): /YYYY/MM/DD/slug — مبني حصراً على عمود published_at
    (وليس created_at؛ الموقع لا يقرأ created_at إطلاقاً لبناء الرابط).
    ⚠️ articlePath يحسب السنة/الشهر/اليوم عبر new Date(published_at) ثم
    getFullYear()/getMonth()/getDate() بتوقيت متصفح الزائر المحلي (توقيت
    اليمن UTC+3 عملياً). لازم نطابق نفس الحساب هنا وإلا الرابط يشاور على
    يوم مختلف عمّا يتوقعه الموقع = "الخبر غير موجود"."""
    dt = datetime.fromisoformat(published_at_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(YEMEN_TZ)
    return f"{SITE_DOMAIN}/{dt.year:04d}/{dt.month:02d}/{dt.day:02d}/{slug}"


def log_discovery_ready(urls: list) -> None:
    """يسجل الروابط الجاهزة للاكتشاف عبر Sitemap وNews Sitemap وRSS.

    لا يستخدم Google Indexing API للمقالات الإخبارية العادية.
    """
    for url in urls or []:
        log.info("  📡 جاهز للاكتشاف عبر Sitemap وNews Sitemap وRSS: %s", url)

# ══════════════════════════════════════════════════════════════════════
#  🔑  مفاتيح Gemini ونماذجه (تدوير تلقائي عند نفاذ الحصة)
# ══════════════════════════════════════════════════════════════════════

# تُقرأ من متغير بيئة GEMINI_API_KEYS بصيغة مفاتيح مفصولة بفواصل:
# key1,key2,key3,key4,key5
GEMINI_API_KEYS = [
    k.strip() for k in os.environ.get("GEMINI_API_KEYS", "").split(",") if k.strip()
]

# ══════════════════════════════════════════════════════════════════════
#  🔑 منطق تدوير المفاتيح/النماذج (تدوير على مراحل متعددة، وليس مرحلتين
#  فقط كما كان سابقاً):
#
#  كل مرحلة تمثّل نموذجاً واحداً من MODEL_CASCADE بالترتيب. يبدأ بالمفتاح
#  الأول + أول نموذج بالقائمة (PRIMARY_MODEL). عند انتهاء حصة مفتاح معيّن،
#  ينتقل للمفتاح التالي **بنفس النموذج الحالي** — يستمر كذلك حتى المفتاح
#  الأخير.
#
#  عند استُنفاد حصة النموذج الحالي على كل المفاتيح، ينتقل للنموذج التالي
#  بالقائمة (MODEL_CASCADE) بدءاً من المفتاح الأول من جديد، وهكذا حتى آخر
#  نموذج بالقائمة. لو استُنفدت حصته أيضاً على كل المفاتيح، تُرفع
#  الاستثناء نهائياً (لا مزيد من الخيارات لهذا التشغيل).
#
#  بداية كل تشغيل جديد للسكريبت (تشغيل تالٍ عبر cron مثلاً) تبدأ دائماً
#  من الصفر (المفتاح الأول + أول نموذج بالقائمة) تلقائياً، لأن الحالة
#  (_current_key_idx وَ_model_stage_idx) متغيرات وحدة عادية تُهيَّأ من
#  جديد مع كل تشغيل مستقل للعملية (process)، ولا تُحفظ بين التشغيلات.
# ══════════════════════════════════════════════════════════════════════

PRIMARY_MODEL = "gemini-3.6-flash"
FALLBACK_MODEL = "gemini-3.5-flash"

# ترتيب تجربة النماذج (2026-08-12) يعكس الترتيب الفعلي بالقوة والحداثة:
# يبدأ بأحدث وأقوى نموذج flash متاح (3.6)، ثم 3.5، ثم 2.5 (الجيل الأقدم)،
# ثم نسخ flash-lite الاقتصادية من الأحدث للأقدم، وأخيراً gemini-2.5-pro
# كملاذ أخير (أبطأ وأغلى، لكنه لا يزال يعمل لو استُنفدت كل خيارات flash
# على كل المفاتيح).
MODEL_CASCADE = [
    PRIMARY_MODEL,          # gemini-3.6-flash
    FALLBACK_MODEL,         # gemini-3.5-flash
    "gemini-3.7-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]

_current_key_idx = 0
_model_stage_idx = 0


def current_model() -> str:
    return MODEL_CASCADE[_model_stage_idx]


def current_key() -> str:
    return GEMINI_API_KEYS[_current_key_idx]


def model_url() -> str:
    return (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{current_model()}:generateContent"
    )


class DailyQuotaExceeded(Exception):
    pass


class ModelUnavailable(Exception):
    """يُرفع عند 404 (النموذج غير متاح/غير مفعّل لهذا المفتاح تحديداً)."""
    pass


class KeyForbidden(Exception):
    """يُرفع عند 401/403 (المفتاح غير صالح/ملغى أو مرفوض لهذا النموذج)."""
    pass


# ══════════════════════════════════════════════════════════════════════
#  📋  اللوغر
# ══════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("janoub_news_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
#  🌐  تجاوز حجب DNS المحلي لموقع "عدن تايم" — عبر DNS-over-HTTPS
# ──────────────────────────────────────────────────────────────────────
#  في بعض شبكات الإنترنت اليمنية يُحجب aden-tm.net على مستوى DNS فقط
#  (الاتصال بالـ IP نفسه غير محجوب). الحل هنا: نصحّح دالة تحليل الأسماء
#  socket.getaddrinfo بحيث يُحلّ اسم هذين النطاقين تحديداً عبر خدمة DNS
#  عامة (Cloudflare 1.1.1.1) بدل DNS مزود الإنترنت المحجوب، بينما تبقى
#  كل بقية الأسماء (Supabase، تيليجرام، r.jina.ai...) تُحلّ بشكل طبيعي.
#  هذا يعمل تلقائياً مع كل استدعاءات requests.get/post بالملف كله، بدون
#  تعديل كل موضع على حدة، ولا يغيّر أي سلوك آخر بالسكربت.
# ══════════════════════════════════════════════════════════════════════

import socket as _socket

_ORIGINAL_GETADDRINFO = _socket.getaddrinfo
_DOH_BYPASS_HOSTS = {"aden-tm.net", "www.aden-tm.net"}
_DOH_IP_CACHE: dict[str, str] = {}
_DOH_LAST_FAILURE_TIME: float = 0.0
_DOH_FAILURE_COOLDOWN_SECONDS = 120  # لا نعيد محاولة DoH إلا بعد دقيقتين من آخر فشل، لتجنب هدر وقت/اتصالات فاشلة متكررة


def _resolve_via_doh(hostname: str) -> Optional[str]:
    """يحل اسم النطاق عبر Cloudflare DNS-over-HTTPS (JSON API)."""
    global _DOH_LAST_FAILURE_TIME
    cached = _DOH_IP_CACHE.get(hostname)
    if cached:
        return cached
    if _DOH_LAST_FAILURE_TIME and (time.time() - _DOH_LAST_FAILURE_TIME) < _DOH_FAILURE_COOLDOWN_SECONDS:
        return None  # تجاهل المحاولة مباشرة بدل انتظار 8 ثواني لاتصال شبه مؤكد فشله
    try:
        r = requests.get(
            "https://1.1.1.1/dns-query",
            params={"name": hostname, "type": "A"},
            headers={"accept": "application/dns-json"},
            timeout=8,
        )
        r.raise_for_status()
        answers = r.json().get("Answer", [])
        ip = next((a["data"] for a in answers if a.get("type") == 1), None)
        if ip:
            _DOH_IP_CACHE[hostname] = ip
            log.info(f"  🌐 حُلّ {hostname} عبر DoH إلى {ip} (تجاوز حجب DNS المحلي)")
        return ip
    except Exception as e:
        _DOH_LAST_FAILURE_TIME = time.time()
        log.warning(f"  ⚠️  فشل تحليل {hostname} عبر DoH: {e}")
        return None


def _patched_getaddrinfo(host, *args, **kwargs):
    if host in _DOH_BYPASS_HOSTS:
        ip = _resolve_via_doh(host)
        if ip:
            try:
                return _ORIGINAL_GETADDRINFO(ip, *args, **kwargs)
            except OSError:
                pass  # لو فشل الاتصال بالـ IP المحلول لأي سبب، نكمل بالمسار العادي أدناه
    return _ORIGINAL_GETADDRINFO(host, *args, **kwargs)


_socket.getaddrinfo = _patched_getaddrinfo


# ── الطبقة الثالثة: لو الشبكة تحجب حتى IP خدمات DNS العامة نفسها (1.1.1.1)،
# نلجأ لسلسلة بروكسيات HTTP عامة تجلب الصفحة/الملف من سيرفرها هي وتعيده لنا
# خام — جهازك ما يتصل إطلاقاً بـ aden-tm.net ولا بأي IP معروف لخدمات DNS،
# فقط بدومين البروكسي نفسه. نجرّب أكثر من بروكسي بالتتابع لأن أي واحد منها
# ممكن يكون بطيء أو متعطل مؤقتاً (مشاهد فعلياً مع allorigins.win).
from urllib.parse import quote as _urlquote

_PROXY_PASSTHROUGH_URLS = [
    "https://janoub-proxy.moieen2000.workers.dev/?url={}",  # بروكسي خاص (Cloudflare Worker) — الأثبت لأنه IP/دومين خاص غير مستهدف بالحجب
]
_PROXY_RETRIES_PER_HOST = 2
_PROXY_RETRY_DELAY_SECONDS = 4


def fetch_with_bypass(url: str, headers: Optional[dict] = None,
                       timeout: int = REQUEST_TIMEOUT) -> requests.Response:
    """يجلب url بمحاولات متدرجة: اتصال مباشر ← DoH (مدمج تلقائياً عبر
    getaddrinfo أعلاه) ← سلسلة بروكسيات HTTP احتياطية (يجرّب كل واحد لو فشل
    اللي قبله) ← لو فشلت الجولة الكاملة، ينتظر قليلاً ويعيد جولة كاملة ثانية
    (لأن فشل كل البروكسيات دفعة واحدة غالباً تذبذب شبكة مؤقت لا حجب فعلي).
    يرمي آخر استثناء لو فشلت كل المحاولات، بنفس سلوك requests.get العادي حتى
    يبقى الكود المستدعي (fetch_feed / extract_article / download_image_bytes)
    بلا أي تغيير بطريقة التعامل مع النتيجة."""
    headers = headers or {}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp
    except requests.RequestException as e:
        log.warning(f"  ⚠️  فشل الاتصال المباشر/DoH بـ {url}: {e} — أجرّب بروكسيات HTTP احتياطية")

    last_exc: Optional[Exception] = None
    for full_pass in (1, 2):
        for template in _PROXY_PASSTHROUGH_URLS:
            proxied_url = template.format(_urlquote(url, safe=""))
            for attempt in range(1, _PROXY_RETRIES_PER_HOST + 1):
                try:
                    resp = requests.get(proxied_url, headers=headers, timeout=timeout + 20)
                    resp.raise_for_status()
                    log.info(f"  🌐 جُلبت {url} عبر بروكسي HTTP احتياطي ({proxied_url.split('/')[2]})")
                    return resp
                except requests.RequestException as e:
                    last_exc = e
                    log.warning(f"  ⚠️  فشل بروكسي {proxied_url.split('/')[2]} (محاولة {attempt}/{_PROXY_RETRIES_PER_HOST}): {e}")
                    if attempt < _PROXY_RETRIES_PER_HOST:
                        time.sleep(_PROXY_RETRY_DELAY_SECONDS)
                    continue
        if full_pass == 1:
            log.warning(f"  ⏳ فشلت كل البروكسيات بالجولة الأولى لـ {url} — انتظار 20 ثانية ثم إعادة جولة كاملة (على الأغلب تذبذب شبكة مؤقت)")
            time.sleep(20)

    raise last_exc


# ══════════════════════════════════════════════════════════════════════
#  ⏳  محدد المعدّل (Rate Limiter) — بسيط
# ══════════════════════════════════════════════════════════════════════

class RateLimiter:
    def __init__(self, requests_per_minute: int = 12):
        self.min_interval = 60.0 / max(requests_per_minute, 1)
        self._last = 0.0

    def wait(self):
        now = time.time()
        elapsed = now - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.time()


RATE_LIMITER = RateLimiter(requests_per_minute=12)


# ══════════════════════════════════════════════════════════════════════
#  🧬 متجهات Gemini (embeddings) لعناوين الأخبار — تُستخدم لكشف تكرار
#  الخبر عبر مصادر مختلفة اعتماداً على التشابه الدلالي للمعنى، وليس فقط
#  التطابق الحرفي بالنص. نطلب المتجه الكامل من gemini-embedding-001 ثم
#  نقصّه لأول EMBEDDING_DIM بعد فقط قبل التخزين (تقليم متجهات Matryoshka
#  بهذا الموديل يبقى دقيقاً لحساب تشابه جيب التمام cosine similarity حتى
#  بدون إعادة تطبيع، فيوفر مساحة تخزين بالسجل المحلي دون فقدان دقة تُذكر
#  لغرض كشف التكرار).
# ══════════════════════════════════════════════════════════════════════

EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIM = 256
EMBEDDING_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{EMBEDDING_MODEL}:embedContent"
)

_embedding_key_idx = 0


def get_title_embedding(title: str) -> Optional[list[float]]:
    """يجيب متجه Gemini embedding لعنوان خبر (لاستخدامه بكشف التكرار
    الدلالي). يدور تلقائياً على مفاتيح GEMINI_API_KEYS المتاحة بنفس
    ترتيبها الطبيعي (1 → 2 → ... → 9) عند فشل مفتاح معيّن (429 أو خطأ
    اتصال)، بتدوير مستقل تماماً عن تدوير مفاتيح توليد المقالات
    (call_with_rotation) حتى لا يتداخل معه."""
    global _embedding_key_idx
    title = (title or "").strip()
    if not title:
        return None

    body = {
        "model": f"models/{EMBEDDING_MODEL}",
        "content": {"parts": [{"text": title}]},
        "taskType": "SEMANTIC_SIMILARITY",
    }

    for _ in range(len(GEMINI_API_KEYS)):
        key = GEMINI_API_KEYS[_embedding_key_idx % len(GEMINI_API_KEYS)]
        try:
            RATE_LIMITER.wait()
            resp = requests.post(EMBEDDING_URL, params={"key": key}, json=body, timeout=30)
            if resp.status_code == 429:
                _embedding_key_idx += 1
                continue
            resp.raise_for_status()
            values = resp.json().get("embedding", {}).get("values")
            if not values:
                return None
            return values[:EMBEDDING_DIM]
        except requests.RequestException as e:
            log.warning(f"  ⚠️  فشل جلب embedding للعنوان: {e}")
            _embedding_key_idx += 1

    log.warning("  ⚠️  تعذّر جلب embedding بكل المفاتيح المتاحة — سيُستخدم التشابه النصي كاحتياط.")
    return None


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ══════════════════════════════════════════════════════════════════════
#  📡  سحب وتحليل RSS
# ══════════════════════════════════════════════════════════════════════

NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "media": "http://search.yahoo.com/mrss/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

IMG_TAG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)


def strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def extract_image_url(item: ET.Element, description_raw: str, content_encoded_raw: str) -> Optional[str]:
    """يبحث عن رابط صورة الخبر داخل عنصر <item> بترتيب الأولويات:
    media:content ← media:thumbnail ← enclosure ← أول <img> داخل content:encoded أو description."""
    media_content = item.find("media:content", NS)
    if media_content is not None:
        url = (media_content.get("url") or "").strip()
        if url:
            return url

    media_thumbnail = item.find("media:thumbnail", NS)
    if media_thumbnail is not None:
        url = (media_thumbnail.get("url") or "").strip()
        if url:
            return url

    enclosure = item.find("enclosure")
    if enclosure is not None:
        url = (enclosure.get("url") or "").strip()
        enc_type = (enclosure.get("type") or "").lower()
        if url and (not enc_type or enc_type.startswith("image")):
            return url

    for raw_html in (content_encoded_raw, description_raw):
        if raw_html:
            m = IMG_TAG_RE.search(raw_html)
            if m:
                return html.unescape(m.group(1).strip())

    return None


def fetch_feed(url: str, category: str) -> list[dict]:
    if url == RSS_ALJAZEERA_YEMEN_URL:
        return fetch_yemen_category_page(category)
    # يدعم رابط إنترنت (http/https) أو مسار ملف XML محلي على الجهاز
    if url.startswith("http://") or url.startswith("https://"):
        try:
            resp = fetch_with_bypass(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            raw_content = resp.content
        except requests.RequestException as e:
            log.warning(f"  ⚠️  فشل سحب {url}: {e}")
            return []
    else:
        try:
            with open(url, "rb") as f:
                raw_content = f.read()
        except OSError as e:
            log.warning(f"  ⚠️  فشل قراءة الملف المحلي {url}: {e}")
            return []

    try:
        root = ET.fromstring(raw_content)
    except ET.ParseError as e:
        log.warning(f"  ⚠️  فشل تحليل XML من {url}: {e}")
        return []

    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date_raw = item.findtext("pubDate") or ""
        description = item.findtext("description") or ""
        content_encoded = item.findtext("content:encoded", namespaces=NS) or ""
        author_raw = (item.findtext("author") or item.findtext("dc:creator") or "").strip()

        if not title or not link:
            continue

        pub_date = parse_pub_date(pub_date_raw, source_url=url)

        image_url = extract_image_url(item, description, content_encoded)

        body = content_encoded or description
        items.append({
            "title": strip_html(title),
            "link": link,
            "pub_date": pub_date,
            "raw_body": strip_html(body),
            "source_feed": url,
            "image_url": image_url,
            "category": category,
            "author": strip_html(author_raw) or None,
        })
    return items


# ══════════════════════════════════════════════════════════════════════
#  🌍  مصدر بديل لأخبار اليمن بالجزيرة: كشط صفحة القسم مباشرة بدل RSS العام
#  (فيد RSS_ALJAZEERA_YEMEN_URL كان يرجّع كل أقسام الجزيرة، فيحتاج فحص
#  <meta name="where"> لكل رابط، وأغلبها يُستبعد لأنها مو يمنية — النتيجة
#  عمليًا كانت غالبًا صفر أخبار منشورة. صفحة aljazeera.net/where/mideast/
#  arab/yemen/ مفلترة أصلاً من الموقع نفسه حسب الجغرافيا، فنسحب روابطها
#  مباشرة. فحص site_where بـapply_full_extraction يبقى شغّال كطبقة تحقق
#  إضافية تلقائية — بدون أي كلفة زائدة، لأن كل رابط يُفتح فعلياً بالأصل.)
# ══════════════════════════════════════════════════════════════════════
YEMEN_CATEGORY_PAGE_URL = "https://www.aljazeera.net/where/mideast/arab/yemen/"

_AJA_ARTICLE_LINK_RE = re.compile(
    r"^https?://www\.aljazeera\.net/([a-z\-]+)/(\d{4})/(\d{1,2})/(\d{1,2})/([^/?#]+)/?$"
)
_AJA_EXCLUDED_SEGMENTS = {"encyclopedia", "tag", "where"}


def fetch_yemen_category_page(category: str) -> list[dict]:
    """يجلب صفحة قسم اليمن بالجزيرة (مفلترة جغرافياً من الموقع نفسه) ويستخرج
    روابط أخبار اليوم الحالي فقط (بتوقيت اليمن) — بديل عن RSS العام."""
    try:
        resp = fetch_with_bypass(YEMEN_CATEGORY_PAGE_URL, headers=ARTICLE_HEADERS,
                                  timeout=ARTICLE_REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning(f"  ⚠️  فشل جلب صفحة قسم اليمن بالجزيرة: {e}")
        return []

    soup = BeautifulSoup(resp.content, "html.parser")
    main = soup.find(id="main-content-area") or soup

    now_yemen = datetime.now(YEMEN_TZ)
    today_str = now_yemen.strftime("%Y-%m-%d")
    # نضم أمس أيضاً (مو اليوم بس) — لأن التشغيلات بالساعات الأولى من اليوم
    # (منتصف الليل → الفجر بتوقيت اليمن) غالباً ما فيه أخبار منشورة لليوم
    # الحالي بعد، فتخرج صفر لو اقتصرنا على اليوم فقط. existing_urls/
    # published_titles_log أصلاً يمنعان أي تكرار نشر لخبر سبق نشره.
    yesterday_str = (now_yemen - timedelta(days=1)).strftime("%Y-%m-%d")
    now_utc = datetime.now(timezone.utc)

    seen: set = set()
    items: list[dict] = []
    for a in main.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("/"):
            href = "https://www.aljazeera.net" + href
        m = _AJA_ARTICLE_LINK_RE.match(href.split("?")[0])
        if not m or m.group(1) in _AJA_EXCLUDED_SEGMENTS:
            continue
        link_date = f"{m.group(2)}-{int(m.group(3)):02d}-{int(m.group(4)):02d}"
        if link_date not in (today_str, yesterday_str) or href in seen:
            continue
        seen.add(href)

        title = _clean_text(a.get_text(strip=True))
        if not title:
            heading = a.find(["h2", "h3"])
            title = _clean_text(heading.get_text(strip=True)) if heading else ""
        if not title:
            continue

        items.append({
            "title": title,
            "link": href,
            "pub_date": now_utc,
            "raw_body": "",
            "source_feed": RSS_ALJAZEERA_YEMEN_URL,
            "image_url": None,
            "category": category,
            "author": None,
        })

    log.info(f"  ↳ صفحة قسم اليمن: {len(items)} خبر بتاريخ اليوم أو أمس ({yesterday_str} / {today_str})")
    return items


def parse_pub_date(pub_date_raw: str, source_url: str = "") -> datetime:
    """
    تحليل تاريخ النشر من مصادر RSS/XML متعددة الصيغ.

    المشكلة الأصلية: parsedate_to_datetime تتوقع صيغة RFC-822 القياسية فقط
    (مثل "Sat, 04 Jul 2026 15:11:31 GMT"). أي مصدر يكتب التاريخ بصيغة
    مختلفة (شائع بالملفات المحلية المُصدَّرة يدوياً) كان يتسبب بفشل صامت
    ويُستبدل التاريخ الحقيقي بوقت تشغيل البوت نفسه (datetime.now()).

    الحل: تجربة عدة صيغ شائعة قبل الاستسلام، مع تسجيل تحذير واضح
    لو فشلت كل المحاولات (بدل الفشل الصامت السابق).
    """
    raw = (pub_date_raw or "").strip()
    if not raw:
        log.warning(f"  ⚠️  تاريخ نشر فارغ من المصدر {source_url or '(غير معروف)'} — استُخدم وقت البوت الحالي كبديل.")
        return datetime.now(timezone.utc)

    # 1) الصيغة القياسية RFC-822 (المتوقعة أصلاً بمعظم فيدات RSS)
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass

    # 2) صيغ شائعة بديلة (ISO 8601 وصيغ عربية/محلية مألوفة)
    fallback_formats = [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
    ]
    cleaned = raw.replace("Z", "+00:00")
    for fmt in fallback_formats:
        try:
            dt = datetime.strptime(cleaned, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            continue

    # 3) فشلت كل المحاولات — نسجل تحذير صريح بدل الفشل الصامت
    log.warning(
        f"  ⚠️  تعذّر تحليل تاريخ النشر '{raw}' من المصدر {source_url or '(غير معروف)'} "
        "— استُخدم وقت تشغيل البوت كبديل مؤقت. يُفضّل فحص صيغة التاريخ بهذا المصدر."
    )
    return datetime.now(timezone.utc)


def contains_blocked_keyword(*texts: str) -> bool:
    combined = " ".join(t for t in texts if t)
    return any(kw in combined for kw in BLOCKED_KEYWORDS)


def contains_blocked_ad_phrase(*texts: str) -> bool:
    """يفحص النشرات/العبارات المتكررة (BLOCKED_BULLETIN_PHRASE_GROUPS) بعد
    تطبيع النص (بأي صيغة كتابة). يُطبَّق على كل الأخبار من كل الأقسام
    والمصادر بدون استثناء، بخلاف BLOCKED_KEYWORDS المحصورة بـ
    BLOCKED_KEYWORDS_CATEGORIES/SOURCES."""
    combined = " ".join(t for t in texts if t)
    normalized = _normalize_ar_for_blocking(combined)
    return any(
        all(tok in normalized for tok in group)
        for group in BLOCKED_BULLETIN_PHRASE_GROUPS
    )


# الأقسام اللي تُطبَّق عليها فلترة BLOCKED_KEYWORDS. حالياً قسم "أخبار عدن"
# فقط (من ملفات XML المحلية) — بقية الأقسام (أخبار وتقارير، رياضة، أسعار
# العملات والذهب، آراء واتجاهات) وكذلك RSS المساء لا تُطبَّق عليها هذي الفلترة.
BLOCKED_KEYWORDS_CATEGORIES = {"أخبار عدن"}

# مصادر (روابط/ملفات فيد) إضافية تُطبَّق عليها فلترة BLOCKED_KEYWORDS بغض
# النظر عن قسمها — تحديداً فيد عدن تايم الحي المستخدم بوضع "1" (استخراج
# الخبر كاملاً). أي مصدر آخر غير مذكور هنا أو بـBLOCKED_KEYWORDS_CATEGORIES
# (مثل RSS المساء) يبقى بدون فلترة تماماً.
BLOCKED_KEYWORDS_SOURCES = {RSS_ADEN_TM_FULL_URL}


def collect_recent_items(feed_categories: Optional[dict] = None) -> list[dict]:
    if feed_categories is None:
        feed_categories = RSS_FEED_CATEGORIES
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_WINDOW)
    all_items = []
    for feed_url, category in feed_categories.items():
        log.info(f"📡 سحب: {feed_url}  ← القسم: {category}")
        items = fetch_feed(feed_url, category)
        recent = [it for it in items if it["pub_date"] >= cutoff]
        log.info(f"   ↳ {len(items)} خبر إجمالي، {len(recent)} خلال آخر {HOURS_WINDOW} ساعة")
        if category in BLOCKED_KEYWORDS_CATEGORIES or feed_url in BLOCKED_KEYWORDS_SOURCES:
            before = len(recent)
            recent = [it for it in recent if not contains_blocked_keyword(it["title"], it["raw_body"])]
            blocked = before - len(recent)
            if blocked:
                log.info(f"   ↳ 🚫 تم تجاوز {blocked} خبر يحتوي كلمات محظورة (لن يُرسل لـ Gemini أو يُنشر)")

        before_ad = len(recent)
        recent = [it for it in recent if not contains_blocked_ad_phrase(it["title"], it["raw_body"])]
        blocked_ad = before_ad - len(recent)
        if blocked_ad:
            log.info(f"   ↳ 🚫 تم تجاوز {blocked_ad} خبر يطابق نشرة/عبارة محظورة نهائياً (لن يُنشر)")
        all_items.extend(recent)
    return all_items


# ══════════════════════════════════════════════════════════════════════
#  📰  استخراج نص الخبر الكامل من صفحته (المنطق الجديد — extract_article_test.py)
# ──────────────────────────────────────────────────────────────────────
#  مدمجة حرفياً كما هي من extract_article_test.py، بدون أي تعديل على
#  منطقها الداخلي. تُستخدم فقط عندما يختار المستخدم وضع "1" عند التشغيل
#  (استخراج الخبر كاملاً بفتح كل صفحة، بدل الاكتفاء بملخص/متن RSS).
#  ⚠️ أسماء الثوابت العامة (REQUEST_TIMEOUT/HEADERS/DEBUG) في السكربت
#  الأصلي أُعيدت تسميتها بادئة ARTICLE_ لتفادي أي تعارض مع ثوابت البوت
#  الحالية (خصوصاً REQUEST_TIMEOUT=60 المستخدم بباقي طلبات الشبكة بالبوت).
# ══════════════════════════════════════════════════════════════════════

ARTICLE_REQUEST_TIMEOUT = 30
ARTICLE_HEADERS = {"User-Agent": "Mozilla/5.0 (Android; Mobile) NewsBot/1.0"}

# مطفأ افتراضياً هنا (كان True بسكربت الاختبار المستقل للتشخيص التفاعلي) —
# داخل البوت الكامل تشغيله لكل خبر سيغرق سجل اللوغ بتفاصيل كل خطوة استخراج.
# فعّله يدوياً إذا احتجت تشخيص مشكلة استخراج معينة.
ARTICLE_DEBUG = False

# حد أدنى لطول نص العنصر عشان يُعتبر "فقرة حقيقية محتملة" وليس زر/رابط قصير
MIN_LEAF_LEN = 25

# إذا الاستخراج المحلي (JSON-LD + ترتيب الصفحة + تجميع الكتل) رجّع أقل من
# هذا العدد من الحروف، نعتبره فاشلاً/غير كافي ونجرب Jina Reader كحل أخير
MIN_ACCEPTABLE_LOCAL_LEN = 150

# وسوم لا تحتوي أبداً نص مقال حقيقي — تُستبعد قبل أي تحليل
ALWAYS_STRIP_TAGS = ("script", "style", "iframe", "form", "noscript", "svg")

# محارف تحكم خفية (Zero-Width / BOM) تظهر أحياناً بمنتصف الكلمات بالنصوص
# العربية المنسوخة من الويب، وتسبب رمز غريب (�) عند الطباعة/التخزين
ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u200e\u200f\ufeff]")


def _clean_text(text: str) -> str:
    """يشيل محارف التحكم الخفية ويهذّب المسافات الزايدة."""
    text = ZERO_WIDTH_RE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


# أسطر مفردة تُتجاهل (تُتخطى فقط، بدون إيقاف الاستخراج) حتى لو وقعت
# داخل نطاق المتن — لأنها ليست جزءاً من الخبر نفسه
NOISE_LINE_PATTERNS = [
    r"^شارك$",
    r"^المصدر\s*/",
    r"^تابعونا على",
    r"^متابعات خاصة",     # مثل: "متابعات خاصة _ المساء برس"
    r"صحيفة (الكترونية|إلكترونية) تأسست",  # فقرة "عن الصحيفة" بالفوتر (نص ثابت لا يتغير، يظهر بأغلب صفحات نفس الموقع)
    r"^!Image\s*\d+",      # تعليق/بديل صورة (مكرر غالباً لنفس عنوان الخبر أو صور كتّاب)
    r"^آخر تحديث\s*:",     # توقيت آخر تحديث للموقع (ثابت بكل الصفحات، مو جزء من الخبر)
    r"^انشر",              # سطر أزرار المشاركة الاجتماعية المتلاصقة (مثل: "انشرFacebookTwitterEmail...")
    r"^\s*$",
]

# صيغة توقيت مميزة لويدجت "آخر الأخبار/أحدث المنشورات" الثابت (يتكرر بكل
# صفحات الموقع): "السبت/04/يوليو/2026 - 05:07 م" — بشرطات مائلة بين
# اليوم/الشهر/السنة، تختلف عن توقيت الخبر الحقيقي "السبت - 04 يوليو 2026 -
# 10:14 م بتوقيت عدن" (بمسافات وشرطات عادية). نستخدمها كعلامة توقف مبكرة
# وأدق من "احدث المنشورات" نفسها، لأن التيزرات تبدأ قبل تلك العبارة أحياناً.
STOP_REGEX_PATTERNS = [
    re.compile(r"^\S+/\d{1,2}/\S+/\d{4}\s*-\s*\d{1,2}:\d{2}\s*[صم]\.?\s*$"),
]


def _hits_stop_regex(text: str) -> bool:
    return any(p.match(text.strip()) for p in STOP_REGEX_PATTERNS)

# علامات توقف: أول عنصر بالكتلة الفائزة نصه يطابقها = نهاية المتن الفعلي
STOP_MARKERS = [
    "مواضيع قد تهمك",
    "قد يعجبك ايضا",
    "قد يعجبك أيضا",
    "المقال السابق",
    "المقال التالي",
    "الأكثر قراءة",
    "أحدث المنشورات",
    "احدث المنشورات",
    "مقالات ذات صلة",
    "التعليقات",
    "أضف تعليق",
    "اترك تعليقاً",
    "اترك رد",
    "شارك المقال",
]


def _extract_by_doc_order(h1: Optional[Tag], soup: BeautifulSoup) -> list[str]:
    """يمشي بترتيب ظهور الصفحة بعد العنوان ويلقط كل <p>/<h2-4>، متوقفاً عند
    أول علامة توقف (تعليقات/مقالات ذات صلة/إلخ)."""
    start_node = h1 or soup.body or soup
    paragraphs = []
    for el in start_node.find_all_next(["p", "h2", "h3", "h4"]):
        text = _clean_text(el.get_text(" ", strip=True))
        if not text:
            continue
        if _hits_stop_regex(text):
            break
        if _text_hits_stop_marker(text):
            break
        if _is_noise_line(text):
            continue
        if len(text) < 20:
            continue
        paragraphs.append(text)
    return paragraphs


def _is_noise_line(text: str) -> bool:
    """يفحص لو السطر بالكامل يطابق نمط ضجيج معروف. نستخدم re.search (مو
    re.match) لأن بعض الأنماط (مثل فقرة 'عن الصحيفة' بالفوتر) قد لا تبدأ
    بالضبط من أول حرف بالسطر."""
    text = text.strip()
    return any(re.search(p, text) for p in NOISE_LINE_PATTERNS)


def _text_hits_stop_marker(text: str) -> bool:
    text = text.strip()
    return any(marker in text for marker in STOP_MARKERS)


def _own_visible_text_len(tag: Tag) -> int:
    return len(tag.get_text(" ", strip=True))


def _find_leaf_blocks(soup: BeautifulSoup) -> list[Tag]:
    """يرجع كل العناصر اللي هي 'كتلة نص' فعلية (مو مجرد غلاف لعناصر أخرى)."""
    leaves = []
    for tag in soup.find_all(True):
        if tag.name in ALWAYS_STRIP_TAGS:
            continue
        text_len = _own_visible_text_len(tag)
        if text_len < MIN_LEAF_LEN:
            continue
        has_big_child = any(
            isinstance(c, Tag) and c.name not in ALWAYS_STRIP_TAGS
            and _own_visible_text_len(c) >= MIN_LEAF_LEN
            for c in tag.find_all(True, recursive=False)
        )
        if has_big_child:
            continue
        leaves.append(tag)
    return leaves


def _walk_jsonld(node) -> list[dict]:
    found = []
    if isinstance(node, dict):
        found.append(node)
        for v in node.values():
            found.extend(_walk_jsonld(v))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_jsonld(item))
    return found


def _extract_from_jsonld(soup: BeautifulSoup) -> Optional[dict]:
    scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    if ARTICLE_DEBUG:
        print(f"  🔧 عدد وسوم JSON-LD الموجودة: {len(scripts)}")

    for script in scripts:
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for obj in _walk_jsonld(data):
            body = obj.get("articleBody") or obj.get("text")
            if body and isinstance(body, str) and len(body) > MIN_LEAF_LEN:
                title = obj.get("headline") or obj.get("name")
                body = _clean_text(body)
                if ARTICLE_DEBUG:
                    print(f"  ✅ لقيت articleBody داخل JSON-LD ({len(body)} حرف)")
                return {"title": _clean_text(title) if title else title,
                        "body": body,
                        "paragraphs": [p.strip() for p in body.split("\n") if p.strip()]}
    return None


# ──────────────────────────────────────────────────────────────────────
# حل احتياطي: Jina AI Reader — يفتح الصفحة فعلياً (بمحرك متصفح خفي على
# سيرفرات Jina) ويشغّل جافاسكربت، ويرجع النص النهائي الجاهز كماركداون.
# مجاني، بدون تسجيل، بدون مفتاح API — فقط GET عادي.
# ──────────────────────────────────────────────────────────────────────

JINA_READER_BASE = "https://r.jina.ai/"

# أسطر تعتبر "قائمة تنقل/روابط" مو متن خبر حقيقي، تظهر كثير بمخرجات Jina
_MD_LINK_ONLY_RE = re.compile(r"^[-*+]?\s*!?\[[^\]]*\]\([^)]*\)\s*$")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _strip_markdown_noise(text: str) -> str:
    """يشيل تنسيق الماركداون تماماً، بالترتيب الصحيح من الداخل للخارج:
    أولاً الصور (حتى المتداخلة داخل رابط، مثل [![نص](رابط_صورة)](رابط_وجهة)
    التي يستخدمها هذا الموقع لكل الإعلانات والأيقونات) تُحذف بالكامل مع نصها
    البديل (alt text)، ثم أي رابط نصي متبقٍ [نص](رابط) يتحول لنصه فقط."""
    # صور (مع دعم title اختياري بين علامتي تنصيص داخل الأقواس) — تُحذف بالكامل
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', "", text)
    # روابط نصية متبقية بعد حذف الصور — نحتفظ بالنص فقط
    text = _MD_LINK_RE.sub(r"\1", text)
    text = re.sub(r"^#+\s*", "", text)          # عناوين ماركداون (#, ##...)
    text = re.sub(r"[*_`]+", "", text)           # تنسيق غامق/مائل
    return _clean_text(text)


def fetch_via_jina(url: str) -> Optional[str]:
    """يرجع محتوى الصفحة كنص/ماركداون بعد تشغيل جافاسكربت فعلياً، أو None
    لو فشل الطلب."""
    reader_url = JINA_READER_BASE + url
    try:
        resp = requests.get(reader_url, headers=ARTICLE_HEADERS, timeout=ARTICLE_REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        if ARTICLE_DEBUG:
            print(f"  ⚠️  فشل طلب Jina Reader: {e}")
        return None
    if ARTICLE_DEBUG:
        print(f"  🔧 Jina Reader رجّع {len(resp.text)} حرف")
    return resp.text


def extract_via_jina(url: str, fallback_title: Optional[str] = None) -> Optional[dict]:
    raw = fetch_via_jina(url)
    if not raw:
        return None

    # مخرجات Jina العادية تجي بصيغة:
    #   Title: ...
    #   URL Source: ...
    #   Markdown Content:
    #   <المحتوى الفعلي هنا>
    title = fallback_title
    if not title:
        m = re.search(r"^Title:\s*(.+)$", raw, re.MULTILINE)
        if m:
            title = _clean_text(m.group(1))

    if "Markdown Content:" in raw:
        content = raw.split("Markdown Content:", 1)[1]
    else:
        content = raw

    paragraphs: list[str] = []
    for line in content.splitlines():
        line = ZERO_WIDTH_RE.sub("", line).strip()
        if not line:
            continue
        cleaned = _strip_markdown_noise(line)
        if not cleaned:
            continue
        if _hits_stop_regex(cleaned):
            if ARTICLE_DEBUG:
                print(f"  ⛔ (Jina) توقف عند توقيت تيزر: \"{cleaned[:60]}\"")
            break
        if _text_hits_stop_marker(cleaned):
            if ARTICLE_DEBUG:
                print(f"  ⛔ (Jina) توقف عند: \"{cleaned[:60]}\"")
            break
        if _is_noise_line(cleaned):
            continue
        if len(cleaned) < 25:
            continue
        paragraphs.append(cleaned)

    if not paragraphs:
        return None

    body = "\n\n".join(paragraphs)
    if ARTICLE_DEBUG:
        print(f"  ✅ (Jina) استخرجت {len(paragraphs)} فقرة، {len(body)} حرف")
    return {"title": title, "body": body, "paragraphs": paragraphs}


def _detect_jsonld_section(soup: BeautifulSoup) -> Optional[str]:
    """يحاول قراءة القسم من حقل articleSection داخل JSON-LD (لو الموقع يوفره) —
    بيانات مهيكلة صريحة يعلنها الموقع نفسه، أدق من تخمين h2/h3 بالموقع (ما فيها
    خطر التقاط عنوان ويدجت/تنقّل غير متعلق بالخبر). يرجّع None لو الحقل غير
    موجود بأي كائن JSON-LD بالصفحة، عندها يُستخدم fallback الـ h2/h3."""
    scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    for script in scripts:
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for obj in _walk_jsonld(data):
            section = obj.get("articleSection")
            if isinstance(section, list) and section:
                section = section[0]
            if section and isinstance(section, str):
                return _clean_text(section)
    return None


def _detect_site_category(h1: Optional[Tag]) -> Optional[str]:
    """يحاول قراءة اسم القسم الفعلي للخبر كما يعرضه موقع عدن تايم بعنوان
    (h2/h3) يظهر مباشرة قبل عنوان الخبر الرئيسي (h1) بالصفحة، مثل
    'اخبار رياضية' أو 'اخبار عدن'. يُستخدم فقط بوضع '1' مع فيد عدن تايم
    لتصحيح قسم النشر تلقائياً بدل تثبيته على قسم واحد لكل الفيد.
    ⚠️ هذا fallback موضعي (أقرب h2/h3 قبل h1 بترتيب الصفحة كاملة، وليس
    بالضرورة داخل رأس الخبر تحديداً) — يُستخدم فقط لو _detect_jsonld_section
    ما لقت شي، لأنه أقل موثوقية من بيانات JSON-LD المهيكلة."""
    if h1 is None:
        return None
    for tag in h1.find_all_previous(["h2", "h3"]):
        text = _clean_text(tag.get_text(strip=True))
        if text:
            return text
    return None


def _detect_aljazeera_where(soup: BeautifulSoup) -> Optional[list[str]]:
    """يقرأ وسم <meta name="where"> بصفحات الجزيرة نت (تصنيف الجغرافيا
    الفعلي الذي يحدده الموقع نفسه لكل خبر، مثل "الشرق الأوسط, اليمن, عربي")
    ويرجّع قائمة القيم مفصولة. يُستخدم فقط لأخبار فيد الجزيرة (RSS_ALJAZEERA_
    YEMEN_URL) لتحديد هل الخبر فعلاً عن اليمن أو عن دولة/موضوع آخر — أدق من
    الاعتماد على articleSection العام (الذي يرجع دائماً "أخبار" بموقع
    الجزيرة بغض النظر عن الدولة). يرجّع None لو الوسم غير موجود بالصفحة."""
    tag = soup.find("meta", attrs={"name": "where"})
    if not tag or not tag.get("content"):
        return None
    return [t.strip() for t in tag["content"].split(",") if t.strip()]


def extract_article(url: str) -> Optional[dict]:
    try:
        resp = fetch_with_bypass(url, headers=ARTICLE_HEADERS, timeout=ARTICLE_REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  ⚠️  فشل جلب الصفحة: {e}")
        return None

    if ARTICLE_DEBUG:
        print(f"  🔧 حجم HTML المستلم: {len(resp.content)} بايت")

    soup = BeautifulSoup(resp.content, "html.parser")

    h1 = soup.find("h1")
    title = _clean_text(h1.get_text(strip=True)) if h1 else None
    if ARTICLE_DEBUG:
        print(f"  🔧 العنوان (h1): {title}")

    jsonld_section = _detect_jsonld_section(soup)
    site_category = jsonld_section or _detect_site_category(h1)
    if ARTICLE_DEBUG:
        source_label = "JSON-LD articleSection" if jsonld_section else "تخمين h2/h3 (ما فيه articleSection بالـ JSON-LD)"
        print(f"  🔧 القسم المكتشف من الصفحة [{source_label}]: {site_category}")

    # 🌍 خاص بموقع الجزيرة نت فقط: تصنيف الجغرافيا الفعلي من <meta name="where">
    # (يُستخدم لفلترة فيد الجزيرة العام لأخبار اليمن تحديداً، شوف
    # RSS_ALJAZEERA_YEMEN_URL وapply_full_extraction). لا علاقة له بمنطق
    # site_category/SITE_CATEGORY_MAP الخاص بعدن تايم.
    site_where = _detect_aljazeera_where(soup)

    def _with_cat(d: Optional[dict]) -> Optional[dict]:
        if d is not None:
            d.setdefault("site_category", site_category)
            d.setdefault("site_where", site_where)
        return d

    jsonld_result = _extract_from_jsonld(soup)
    if jsonld_result:
        if not jsonld_result.get("title"):
            jsonld_result["title"] = title
        return _with_cat(jsonld_result)

    for tag_name in ALWAYS_STRIP_TAGS:
        for t in soup.find_all(tag_name):
            t.decompose()

    doc_order_paragraphs = _extract_by_doc_order(h1, soup)
    total_len = sum(len(p) for p in doc_order_paragraphs)
    if ARTICLE_DEBUG:
        print(f"  🔧 نتيجة الاستخراج بترتيب ظهور الصفحة: {len(doc_order_paragraphs)} "
              f"فقرة، {total_len} حرف")

    if total_len >= MIN_ACCEPTABLE_LOCAL_LEN:
        return _with_cat({"title": title, "body": "\n\n".join(doc_order_paragraphs),
                "paragraphs": doc_order_paragraphs})

    if ARTICLE_DEBUG:
        print("  ℹ️  المحتوى المستخرج بترتيب الصفحة قصير جداً — أجرب تجميع الكتل النصية")

    leaves = _find_leaf_blocks(soup)
    if ARTICLE_DEBUG:
        print(f"  🔧 عدد كتل النص (leaf blocks) بكل الصفحة: {len(leaves)}")

    best_paragraphs: list[str] = []
    if leaves:
        groups: dict[int, dict] = {}
        for leaf in leaves:
            parent = leaf.parent
            key = id(parent)
            g = groups.setdefault(key, {"parent": parent, "leaves": [], "total_len": 0})
            g["leaves"].append(leaf)
            g["total_len"] += _own_visible_text_len(leaf)

        best = max(groups.values(), key=lambda g: g["total_len"])
        if ARTICLE_DEBUG:
            print(f"  🔧 عدد المجموعات (parents) المرشحة: {len(groups)}")

        for leaf in best["leaves"]:
            text = _clean_text(leaf.get_text(" ", strip=True))
            if _hits_stop_regex(text):
                if ARTICLE_DEBUG:
                    print(f"  ⛔ توقف عند توقيت تيزر: \"{text[:60]}\"")
                break
            if _text_hits_stop_marker(text):
                if ARTICLE_DEBUG:
                    print(f"  ⛔ توقف عند: \"{text[:60]}\"")
                break
            if _is_noise_line(text):
                continue
            best_paragraphs.append(text)

    best_total_len = sum(len(p) for p in best_paragraphs)
    if best_total_len >= MIN_ACCEPTABLE_LOCAL_LEN:
        return _with_cat({"title": title, "body": "\n\n".join(best_paragraphs),
                "paragraphs": best_paragraphs})

    # ── كل المحاولات المحلية ما كفت (يعني الأغلب المحتوى مبني بجافاسكربت) ──
    if ARTICLE_DEBUG:
        print("  ℹ️  كل الطرق المحلية رجّعت محتوى قصير/فاضي — أجرب Jina Reader")

    jina_result = extract_via_jina(url, fallback_title=title)
    if jina_result:
        return _with_cat(jina_result)

    # آخر ما نرجع له: أفضل نتيجة محلية توصلنا لها، ولو قصيرة
    if best_total_len > total_len:
        return _with_cat({"title": title, "body": "\n\n".join(best_paragraphs),
                "paragraphs": best_paragraphs})
    return _with_cat({"title": title, "body": "\n\n".join(doc_order_paragraphs),
            "paragraphs": doc_order_paragraphs})


# خريطة تحويل اسم القسم كما يظهر فعلياً بصفحة الخبر بموقع عدن تايم إلى أحد
# أقسام "الجنوب فويس" المعتمدة — تُستخدم فقط بوضع "1" لتصحيح قسم كل خبر من
# عدن تايم تلقائياً حسب قسمه الحقيقي، بدل تثبيته دائماً على "أخبار وتقارير".
# أي قسم مصدر غير مذكور هنا، أو تعذّر اكتشافه من الصفحة، يعني استبعاد الخبر
# كاملاً من النشر (بدل تخمين قسمه أو نشره بقسم خاطئ).
SITE_CATEGORY_MAP = {
    "رياض": "رياضة",              # يغطي: رياضة / اخبار رياضية / الرياضة
    "عرب وعالم": "شؤون دولية",
    "صرف العملات": "أسعار العملات والذهب",
    "كتابات": "آراء واتجاهات",
    "اخبار عدن": "أخبار عدن",
    "اخبار وتقارير": "أخبار وتقارير",
    "اخبار محافظات اليمن": "أخبار محلية",
    "منوعات": "منوعات",
}

# 🔒 الأقسام المسموح بها فعلياً بوضع "1" = الأقسام المحددة أصلاً بـ
# RSS_FEED_CATEGORIES بالأعلى، زائد "أخبار محلية" و"منوعات" (أُضيفا بطلب صريح).
# أي قسم يكتشفه SITE_CATEGORY_MAP ولا يقع ضمن هذي المجموعة (حالياً: "شؤون دولية"
# فقط) يُعامل كقسم غير معروف ويُستبعد الخبر — بدل نشره بقسم لم تحدده أنت أصلاً.
ALLOWED_LIVE_CATEGORIES = set(RSS_FEED_CATEGORIES.values()) | {"أخبار محلية", "منوعات"}


def map_site_category(site_category: Optional[str]) -> Optional[str]:
    """يرجّع اسم القسم المطابق بموقعنا، بشرط أن يكون ضمن ALLOWED_LIVE_CATEGORIES.
    يرجّع None لو القسم المكتشف غير معروف، أو خارج الأقسام المسموحة، أو لم
    يُكتشف أصلاً (كل هذي الحالات تعني: استبعد الخبر من النشر)."""
    if not site_category:
        return None
    for key, target in SITE_CATEGORY_MAP.items():
        if key in site_category and target in ALLOWED_LIVE_CATEGORIES:
            return target
    return None


def apply_full_extraction(items: list[dict]) -> None:
    """يُستدعى فقط بوضع '1' (استخراج الخبر كاملاً). يمشي على كل خبر بالقائمة
    (من عدن تايم أو المساء برس معاً) ويفتح رابطه فعلياً عبر extract_article()،
    ويستبدل raw_body بالنص الكامل المستخرج من الصفحة نفسها بدل الاكتفاء
    بملخص/متن RSS القصير. العنوان الأصلي القادم من RSS يبقى كما هو (لا
    يُستبدل)، وأي خبر يفشل استخراجه كاملاً يحتفظ بـ raw_body الأصلي من RSS
    كما هو (بدون إيقاف تشغيل البوت). لأخبار عدن تايم فقط: يصحّح القسم
    تلقائياً حسب القسم الفعلي المكتشف من صفحة الخبر (عبر SITE_CATEGORY_MAP)؛
    وأي خبر قسمه غير معروف أو تعذّر اكتشافه يُعلَّم بـ "_excluded" ليُستبعد
    من النشر تماماً بدل تخمين قسمه. قسم أخبار المساء لا يُمس إطلاقاً ويبقى
    كما اختاره المستخدم عند التشغيل."""
    total = len(items)
    for idx, it in enumerate(items, start=1):
        log.info(f"  🧲 [{idx}/{total}] استخراج الخبر الكامل: {it['link'][:80]}")
        result = extract_article(it["link"])
        body_ok = bool(result and result.get("body") and len(result["body"]) >= MIN_ACCEPTABLE_LOCAL_LEN)

        if body_ok:
            it["raw_body"] = result["body"]
        else:
            log.warning(
                f"  ⚠️  تعذّر استخراج الخبر كاملاً لهذا الرابط — سيُستخدم نص RSS "
                "الأصلي بدلاً منه (بدون إيقاف التشغيل)."
            )

        # 🗂️ تصحيح القسم مستقل تماماً عن نجاح استخراج المتن الكامل: نحتاجه
        # حتى لو فشل استخراج المتن (extract_article ترجع site_category بكل
        # الحالات تقريباً عبر _with_cat)، وإلا يبقى الخبر بقسمه الافتراضي
        # الخاطئ (أخبار وتقارير) ويفلت من فلاتر الاستبعاد بالسكربت التلقائي.
        if result and it.get("source_feed") == RSS_ADEN_TM_FULL_URL:
            corrected = map_site_category(result.get("site_category"))
            if corrected is None:
                log.info(
                    f"     ↳ 🚫 قسم غير معروف/تعذّر اكتشافه "
                    f"({result.get('site_category')!r}) — سيُستبعد الخبر من النشر."
                )
                it["_excluded"] = True
            elif corrected != it["category"]:
                log.info(f"     ↳ 🗂️  تصحيح القسم تلقائياً: {it['category']} → {corrected}")
                it["category"] = corrected
        elif result is None and it.get("source_feed") == RSS_ADEN_TM_FULL_URL:
            # فشل جلب الصفحة بالكامل (extract_article رجّعت None) — ما فيه
            # أي معلومة عن القسم الحقيقي، فلا يجوز نشره بالقسم الافتراضي.
            log.info("     ↳ 🚫 تعذّر فتح الصفحة نهائياً — سيُستبعد الخبر من النشر (لا يمكن التأكد من قسمه).")
            it["_excluded"] = True

        # 🌍 أخبار فيد الجزيرة العام (RSS_ALJAZEERA_YEMEN_URL): الفيد يحوي كل
        # أقسام الجزيرة نت (عالمي/رياضة/اقتصاد/اليمن/إلخ)، فلازم نتأكد من
        # صفحة كل خبر فعلياً إنه مصنّف تحت "اليمن" (عبر <meta name="where">)
        # قبل نشره — أي خبر غير يمني، أو تعذّر فتح صفحته أصلاً، يُستبعد.
        elif it.get("source_feed") == RSS_ALJAZEERA_YEMEN_URL:
            where_list = (result or {}).get("site_where") or []
            if "اليمن" in where_list:
                if it["category"] != RSS_ALJAZEERA_YEMEN_CATEGORY:
                    it["category"] = RSS_ALJAZEERA_YEMEN_CATEGORY
            else:
                log.info(
                    f"     ↳ 🚫 الخبر ليس عن اليمن حسب تصنيف الجزيرة الفعلي "
                    f"(where={where_list or 'تعذّر اكتشافه'}) — سيُستبعد."
                )
                it["_excluded"] = True


# ══════════════════════════════════════════════════════════════════════
#  🧹  كشف الأخبار المكررة عبر مصادر مختلفة (تشابه دلالي + تقارب زمني)
# ══════════════════════════════════════════════════════════════════════

_ARABIC_DIACRITICS_RE = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670\u06D6-\u06ED]")

DUPLICATE_TITLE_THRESHOLD = 0.92            # احتياطي نصي (difflib) — يُستخدم فقط لو تعذّر جلب embedding
DUPLICATE_EMBEDDING_THRESHOLD = 0.93        # المعيار الأساسي: تشابه دلالي عبر المتجهات (cosine similarity)
DUPLICATE_TIME_WINDOW_MINUTES = 1440


def _normalize_title_for_dedup(title: str) -> str:
    t = title or ""
    t = _ARABIC_DIACRITICS_RE.sub("", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ى", "ي").replace("ة", "ه")
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def remove_duplicate_news(
    items: list[dict],
    threshold: float = DUPLICATE_TITLE_THRESHOLD,
    embedding_threshold: float = DUPLICATE_EMBEDDING_THRESHOLD,
    time_window_minutes: int = DUPLICATE_TIME_WINDOW_MINUTES,
    history_items: Optional[list[dict]] = None,
) -> list[dict]:
    """يستبعد الأخبار المكررة (نفس الحدث من أكثر من مصدر) بشرطين معاً:
    تشابه دلالي مرتفع جداً بين متجهي العنوانين (Gemini embedding) + تقارب
    زمني بين تاريخي النشر. اشتراط الزمن مع التشابه الدلالي المرتفع جداً
    يقلل بشدة احتمال حذف خبر مختلف فعلياً بالخطأ.

    لو تعذّر جلب embedding لأحد العنصرين (خطأ اتصال عابر بـGemini)، يُستخدم
    تلقائياً كاحتياط تشابه العنوان النصي (difflib) بدل تعطيل كشف التكرار
    كلياً لذلك العنصر.

    كل عنصر بـitems يُخزَّن به متجه عنوانه مؤقتاً تحت المفتاح
    "_title_embedding" لإعادة استخدامه لاحقاً (مثلاً عند تسجيل الخبر
    بسجل العناوين المنشورة) دون طلب Gemini إضافي مكرر.

    history_items: أخبار منشورة فعلاً (من تشغيلات سابقة، محتملة من فيد
    مختلف) تُستخدم كمرجع مقارنة فقط ولا تُعاد بالنتيجة. كل عنصر منها يمكن
    أن يحمل "embedding" (متجه) بجانب "title" و"pub_date"."""
    kept: list[dict] = []
    kept_norm_titles: list[str] = []
    kept_pub_dates: list[Optional[datetime]] = []
    kept_embeddings: list[Optional[list[float]]] = []
    time_window = timedelta(minutes=time_window_minutes)

    for h in (history_items or []):
        norm = _normalize_title_for_dedup(h.get("title", ""))
        pub_date = h.get("pub_date")
        if norm and pub_date is not None:
            kept_norm_titles.append(norm)
            kept_pub_dates.append(pub_date)
            kept_embeddings.append(h.get("embedding"))

    history_count = len(kept_norm_titles)

    for it in items:
        norm = _normalize_title_for_dedup(it.get("title", ""))
        pub_date = it.get("pub_date")
        emb = get_title_embedding(it.get("title", ""))
        it["_title_embedding"] = emb
        is_dup = False
        if norm and pub_date is not None:
            for i, existing_norm in enumerate(kept_norm_titles):
                existing_pub_date = kept_pub_dates[i]
                if existing_pub_date is None:
                    continue
                if abs(pub_date - existing_pub_date) > time_window:
                    continue  # بعيدان زمنياً — لا يُعتبران مكررين مهما تشابه العنوان
                existing_emb = kept_embeddings[i]
                if emb and existing_emb:
                    sim = _cosine_similarity(emb, existing_emb)
                    is_match = sim >= embedding_threshold
                    method_label = "دلالي"
                else:
                    if not existing_norm:
                        continue
                    sim = difflib.SequenceMatcher(None, norm, existing_norm).ratio()
                    is_match = sim >= threshold
                    method_label = "نصي احتياطي"
                if is_match:
                    is_dup = True
                    source = "منشور سابقاً" if i < history_count else "بنفس الدفعة"
                    log.info(
                        f"  🔁 خبر مكرر تم استبعاده (تشابه {method_label} {sim:.0%} + تقارب زمني، {source}): "
                        f"{it.get('title', '')[:70]}"
                    )
                    break
        if is_dup:
            continue
        kept.append(it)
        kept_norm_titles.append(norm)
        kept_pub_dates.append(pub_date)
        kept_embeddings.append(emb)

    removed = len(items) - len(kept)
    if removed:
        log.info(f"🧹 تم استبعاد {removed} خبر مكرر من إجمالي {len(items)}.")
    return kept


# ══════════════════════════════════════════════════════════════════════
#  🗄️  Supabase REST API
# ══════════════════════════════════════════════════════════════════════

SB_HEADERS: Optional[dict] = None


def sb_headers() -> dict:
    global SB_HEADERS
    if SB_HEADERS is None:
        SB_HEADERS = {
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
        }
    return SB_HEADERS


DUPLICATE_DB_CHECK_WINDOW_HOURS = 48        # نطاق الفحص المباشر بقاعدة البيانات (أوسع من السجل المحلي 24 ساعة)


def check_similar_published_title_db(
    title: str,
    hours: int = DUPLICATE_DB_CHECK_WINDOW_HOURS,
    threshold: float = DUPLICATE_TITLE_THRESHOLD,
) -> Optional[dict]:
    """يسأل Supabase مباشرة (وليس السجل المحلي) هل نُشر خبر بعنوان مشابه
    خلال آخر `hours` ساعة، بمعزل تام عن ملف السجل المحلي (published_titles_log.json).
    يمسك التكرار حتى لو شُغّل البوت من جهاز آخر، أو انحذف/تأخر تحديث
    السجل المحلي لأي سبب.

    المقارنة نصية (difflib) على العنوان المُطبَّع — جدول Supabase لا يخزّن
    متجهات الـembedding، فلا داعي لطلب Gemini من جديد لكل عنوان قديم.

    يعيد dict فيه 'title' و'similarity_score' لأقرب خبر مشابه لو وُجد
    تشابه ≥ threshold، وإلا يعيد None."""
    norm_title = _normalize_title_for_dedup(title)
    if not norm_title:
        return None

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    url = f"{SUPABASE_URL}/rest/v1/{TABLE_NAME}"
    params = {
        "select": "title,created_at",
        "created_at": f"gte.{cutoff}",
        "status": "eq.published",
        "order": "created_at.desc",
        "limit": "500",
    }
    try:
        r = requests.get(url, headers=sb_headers(), params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        rows = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning(f"  ⚠️  تعذّر فحص التكرار المباشر بقاعدة البيانات: {e}")
        return None

    best_match = None
    best_sim = 0.0
    for row in rows:
        existing_title = row.get("title") or ""
        existing_norm = _normalize_title_for_dedup(existing_title)
        if not existing_norm:
            continue
        sim = difflib.SequenceMatcher(None, norm_title, existing_norm).ratio()
        if sim >= threshold and sim > best_sim:
            best_sim = sim
            best_match = {"title": existing_title, "similarity_score": sim}

    return best_match


def cleanup_system_logs() -> None:
    """ينظّف سجلات النظام المتراكمة (cron.job_run_details و net._http_response)
    عبر دالة RPC آمنة (cleanup_system_logs) بقاعدة البيانات. لا يوقف تشغيل
    البوت أبداً حتى لو فشل التنظيف (مثلاً لو الدالة غير موجودة بعد بقاعدة البيانات)."""
    url = f"{SUPABASE_URL}/rest/v1/rpc/cleanup_system_logs"
    try:
        r = requests.post(url, headers=sb_headers(), json={}, timeout=REQUEST_TIMEOUT)
        if r.status_code in (200, 204):
            try:
                data = r.json()
            except ValueError:
                data = {}
            cron_n = data.get("deleted_cron_job_run_details")
            net_n = data.get("deleted_net_http_response")
            parts = []
            if cron_n == -1:
                parts.append("cron.job_run_details غير متاح")
            elif cron_n is not None:
                parts.append(f"cron: {cron_n} سجل")
            if net_n == -1:
                parts.append("net._http_response غير متاح")
            elif net_n is not None:
                parts.append(f"net: {net_n} سجل")
            summary = " | ".join(parts) if parts else "بدون تفاصيل إضافية"
            log.info(f"🧹 تم تنظيف سجلات النظام الأقدم من 72 ساعة ({summary})")
        else:
            log.warning(f"⚠️  تعذّر تنظيف سجلات النظام [{r.status_code}]: {r.text[:200]}")
    except requests.RequestException as e:
        log.warning(f"⚠️  تعذّر تنظيف سجلات النظام (خطأ اتصال): {e}")


def check_system_logs_size() -> None:
    """يتحقق من عدد صفوف cron.job_run_details و net._http_response عبر دالة
    RPC (get_system_logs_counts) بقاعدة البيانات. لو أي منهما تجاوز
    SYSTEM_LOGS_ALERT_THRESHOLD، يرسل تنبيهاً لمحادثة الإدارة الخاصة (وليس
    قناة الأخبار العامة). لا يوقف تشغيل البوت أبداً حتى لو فشل الفحص."""
    url = f"{SUPABASE_URL}/rest/v1/rpc/get_system_logs_counts"
    try:
        r = requests.post(url, headers=sb_headers(), json={}, timeout=REQUEST_TIMEOUT)
        if r.status_code not in (200, 204):
            log.warning(f"⚠️  تعذّر فحص حجم سجلات النظام [{r.status_code}]: {r.text[:200]}")
            return
        try:
            data = r.json()
        except ValueError:
            data = {}
        cron_n = data.get("cron_count")
        net_n = data.get("net_count")
        exceeded = []
        if isinstance(cron_n, int) and cron_n > SYSTEM_LOGS_ALERT_THRESHOLD:
            exceeded.append(f"cron.job_run_details: {cron_n:,} سجل")
        if isinstance(net_n, int) and net_n > SYSTEM_LOGS_ALERT_THRESHOLD:
            exceeded.append(f"net._http_response: {net_n:,} سجل")
        if exceeded:
            msg = (
                "🚨 تنبيه: تجاوز حجم جداول سجلات النظام الحد المسموح "
                f"({SYSTEM_LOGS_ALERT_THRESHOLD:,} سجل):\n\n"
                + "\n".join(f"• {line}" for line in exceeded)
                + "\n\nتحقّق من جدولة cron.schedule('cleanup-system-logs-6h') "
                  "وتأكد أنها تعمل بشكل صحيح."
            )
            log.warning(f"⚠️  {msg}")
            send_admin_alert(msg)
        else:
            log.info(
                f"✅ حجم سجلات النظام طبيعي (cron: {cron_n}, net: {net_n})"
            )
    except requests.RequestException as e:
        log.warning(f"⚠️  تعذّر فحص حجم سجلات النظام (خطأ اتصال): {e}")


def load_blocked_links() -> set:
    """يقرأ روابط الأخبار الممنوعة نهائياً من ملف BLOCKED_LINKS_FILE (لو موجود).
    يرجّع set فارغ لو الملف غير موجود أو تالف، بدون إيقاف البوت."""
    try:
        with open(BLOCKED_LINKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except FileNotFoundError:
        return set()
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"⚠️  تعذّر قراءة ملف الروابط المحظورة دائماً ({BLOCKED_LINKS_FILE}): {e}")
        return set()


def save_blocked_link(link: str) -> None:
    """يضيف رابط خبر لملف الحظر الدائم (BLOCKED_LINKS_FILE) فوراً، بحيث
    يبقى مستبعداً في كل التشغيلات القادمة حتى لو أُلغيت الجلسة الحالية
    قبل مرحلة 'تأكيد' النشر."""
    if not link:
        return
    blocked = load_blocked_links()
    if link in blocked:
        return
    blocked.add(link)
    try:
        import os
        os.makedirs(os.path.dirname(BLOCKED_LINKS_FILE), exist_ok=True)
        with open(BLOCKED_LINKS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(blocked), f, ensure_ascii=False, indent=2)
    except OSError as e:
        log.warning(f"⚠️  تعذّر حفظ الحظر الدائم لهذا الرابط ({BLOCKED_LINKS_FILE}): {e}")


# ══════════════════════════════════════════════════════════════════════
#  📋  سجل العناوين المنشورة محلياً (لكشف التكرار عبر تشغيلات/فيدات مختلفة)
# ══════════════════════════════════════════════════════════════════════

PUBLISHED_TITLES_LOG_FILE = os.path.join(BASE_DIR, "published_titles_log.json")
PUBLISHED_TITLES_MAX_AGE_HOURS = 24


def _load_and_prune_published_titles_log(max_age_hours: int) -> list[dict]:
    """يقرأ سجل العناوين المحلي، يستبعد أي خبر تجاوز عمره max_age_hours (لكل
    خبر على حدة، وليس للملف ككل)، ويعيد كتابة الملف مُنقّى عشان ما يكبر للأبد."""
    try:
        with open(PUBLISHED_TITLES_LOG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    kept_raw = []
    kept_out = []
    for row in raw:
        try:
            pub_date = datetime.fromisoformat(row["pub_date"])
        except (KeyError, ValueError):
            continue
        if pub_date < cutoff:
            continue  # عمره تجاوز 24 ساعة — يُستبعد من السجل نهائياً
        kept_raw.append(row)
        kept_out.append({"title": row["title"], "pub_date": pub_date, "embedding": row.get("embedding")})

    if len(kept_raw) != len(raw):
        try:
            import os
            os.makedirs(os.path.dirname(PUBLISHED_TITLES_LOG_FILE), exist_ok=True)
            with open(PUBLISHED_TITLES_LOG_FILE, "w", encoding="utf-8") as f:
                json.dump(kept_raw, f, ensure_ascii=False)
        except OSError as e:
            log.warning(f"⚠️  تعذّر تحديث سجل العناوين المحلي: {e}")

    return kept_out


def log_published_title(title: str, pub_date_iso: str, embedding: Optional[list[float]] = None) -> None:
    """يُضاف كل خبر يُنشر فعلاً لسجل محلي دائم (بدون أي طلب لـSupabase)،
    يُستخدم لاحقاً لمنع تكرار نفس الخبر من فيد آخر عبر تشغيلات مختلفة.

    embedding: متجه العنوان (اختياري) — يُفضَّل تمرير المتجه المحسوب أصلاً
    أثناء remove_duplicate_news (المخزَّن بـit["_title_embedding"]) بدل
    طلب Gemini من جديد، حتى تبقى المقارنات المستقبلية متسقة."""
    try:
        with open(PUBLISHED_TITLES_LOG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        raw = []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=PUBLISHED_TITLES_MAX_AGE_HOURS)
    kept_raw = []
    for row in raw:
        try:
            pub_date = datetime.fromisoformat(row["pub_date"])
        except (KeyError, ValueError):
            continue
        if pub_date >= cutoff:
            kept_raw.append(row)

    kept_raw.append({"title": title, "pub_date": pub_date_iso, "embedding": embedding})

    try:
        import os
        os.makedirs(os.path.dirname(PUBLISHED_TITLES_LOG_FILE), exist_ok=True)
        with open(PUBLISHED_TITLES_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(kept_raw, f, ensure_ascii=False)
    except OSError as e:
        log.warning(f"⚠️  تعذّر حفظ الخبر بسجل العناوين المحلي: {e}")


def get_recent_published_titles(hours: int = PUBLISHED_TITLES_MAX_AGE_HOURS) -> list[dict]:
    """يعيد الأخبار المنشورة خلال آخر عدة ساعات من السجل المحلي (بدون أي
    استعلام لـSupabase)، لمقارنتها بالأخبار الجديدة القادمة من تشغيلات لاحقة
    (منع تكرار نفس الحدث من فيد آخر حتى لو نُشر بتشغيل سابق منفصل)."""
    return _load_and_prune_published_titles_log(hours)


# ══════════════════════════════════════════════════════════════════════
#  📋  سجل العناوين المنشورة عبر Supabase (يدوم بين تشغيلات GitHub Actions)
# ══════════════════════════════════════════════════════════════════════
#  GitHub Actions لا يملك قرص دائم: كل تشغيلة تبدأ بنسخة نظيفة من الريبو،
#  فسجل الملف المحلي أعلاه (PUBLISHED_TITLES_LOG_FILE) يتصفّر مع كل تشغيلة
#  جديدة، وطبقة كشف التكرار الأقوى (embedding الدلالي عبر remove_duplicate_
#  news) تفقد فعاليتها عبر التشغيلات المتفرقة (تبقى فعّالة فقط داخل نفس
#  الدفعة/التشغيلة). هذا الجدول يخزّن نفس البيانات (عنوان + تاريخ نشر +
#  متجه) بقاعدة البيانات مباشرة، فتدوم بين كل التشغيلات — دون إزالة
#  الحماية المحلية أعلاه (تبقى تعمل كطبقة إضافية، وتحديداً كخط أول سريع
#  لا يحتاج شبكة، ولا يتوقف لو تعذّر الاتصال بـSupabase مؤقتاً).

BOT_PUBLISHED_TITLES_TABLE = "bot_published_titles_log"
PUBLISHED_TITLES_DB_MAX_AGE_HOURS = 24


def get_recent_published_titles_from_db(
    hours: int = PUBLISHED_TITLES_DB_MAX_AGE_HOURS,
) -> list[dict]:
    """يجلب الأخبار المنشورة خلال آخر عدة ساعات من جدول Supabase (يدوم بين
    كل تشغيلات GitHub Actions، بخلاف الملف المحلي). يرجّع list بنفس شكل
    get_recent_published_titles (title/pub_date/embedding) لدمجها معه مباشرة
    كـhistory_items بـremove_duplicate_news. أي فشل (مشكلة اتصال، الجدول
    غير موجود بعد...) يرجّع list فارغة بدون إيقاف البوت."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    url = f"{SUPABASE_URL}/rest/v1/{BOT_PUBLISHED_TITLES_TABLE}"
    params = {
        "select": "title,pub_date,embedding",
        "pub_date": f"gte.{cutoff}",
        "order": "pub_date.desc",
        "limit": "500",
    }
    try:
        r = requests.get(url, headers=sb_headers(), params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            log.info(
                f"ℹ️  تعذّر جلب سجل العناوين المنشورة من Supabase "
                f"({BOT_PUBLISHED_TITLES_TABLE}) — سيُعتمَد على السجل المحلي فقط."
            )
            return []
        rows = r.json()
    except (requests.RequestException, ValueError) as e:
        log.info(f"ℹ️  تعذّر الاتصال بجدول {BOT_PUBLISHED_TITLES_TABLE} بـSupabase: {e}")
        return []

    out = []
    for row in rows:
        try:
            pub_date = datetime.fromisoformat(row["pub_date"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({
            "title": row.get("title", ""),
            "pub_date": pub_date,
            "embedding": row.get("embedding"),
        })
    if out:
        log.info(f"📋 تم جلب {len(out)} عنوان منشور من سجل Supabase الدائم (آخر {hours} ساعة)")
    return out


def save_published_title_to_db(
    title: str, pub_date_iso: str, embedding: Optional[list[float]] = None,
) -> None:
    """يضيف الخبر المنشور لجدول Supabase الدائم (بجانب السجل المحلي، وليس
    بدلاً عنه)، ليدوم بين تشغيلات GitHub Actions المختلفة. لا يوقف البوت
    عند أي فشل بالإدخال (مشكلة اتصال، الجدول غير موجود بعد...).
    يحذف أيضاً أي صفوف تجاوز عمرها PUBLISHED_TITLES_DB_MAX_AGE_HOURS بنفس
    الاستدعاء حتى لا يتضخّم الجدول للأبد (أسلوب مماثل لتقليم الملف المحلي)."""
    url = f"{SUPABASE_URL}/rest/v1/{BOT_PUBLISHED_TITLES_TABLE}"
    try:
        r = requests.post(
            url, headers=sb_headers(),
            json={"title": title, "pub_date": pub_date_iso, "embedding": embedding},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code not in (200, 201):
            log.warning(
                f"⚠️  تعذّر حفظ الخبر بسجل Supabase الدائم [{r.status_code}]: {r.text[:200]}"
            )
    except requests.RequestException as e:
        log.warning(f"⚠️  تعذّر حفظ الخبر بسجل Supabase الدائم (خطأ اتصال): {e}")
        return

    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=PUBLISHED_TITLES_DB_MAX_AGE_HOURS)
    ).isoformat()
    try:
        requests.delete(
            url, headers=sb_headers(), params={"pub_date": f"lt.{cutoff}"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        log.info(f"ℹ️  تعذّر تقليم سجل Supabase الدائم (لا يؤثر على النشر): {e}")


def load_pending_scheduled() -> list:
    """يقرأ قائمة الأخبار المجدولة اللي لسا ما اتأكدنا من نشرها ولا أرسلنا
    رابطها لتيليجرام بعد. يرجّع قائمة فارغة لو الملف غير موجود أو تالف."""
    try:
        with open(PENDING_SCHEDULED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"⚠️  تعذّر قراءة ملف الأخبار المجدولة المعلّقة ({PENDING_SCHEDULED_FILE}): {e}")
        return []


def save_pending_scheduled(pending: list) -> None:
    """يحفظ قائمة الأخبار المجدولة المعلّقة (بعد الإضافة أو الإزالة)."""
    try:
        import os
        os.makedirs(os.path.dirname(PENDING_SCHEDULED_FILE), exist_ok=True)
        with open(PENDING_SCHEDULED_FILE, "w", encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log.warning(f"⚠️  تعذّر حفظ ملف الأخبار المجدولة المعلّقة ({PENDING_SCHEDULED_FILE}): {e}")


def check_and_notify_scheduled_posts() -> None:
    """
    يُستدعى بأول كل تشغيلة للسكربت (قبل جلب أي أخبار جديدة).

    يفحص كل خبر بقائمة PENDING_SCHEDULED_FILE (أخبار سبق جدولتها بجلسة
    سابقة) ويتحقق من حالتها الحالية بقاعدة البيانات:
      - لو status = published  → صار منشوراً فعلياً (الـ Cron نشره) →
        يُرسل رابطه لتيليجرام الآن لأول مرة، ثم يُحذف من قائمة الانتظار.
      - لو status = scheduled  → لسا ما نُشر → يبقى بقائمة الانتظار
        بدون أي إرسال، ويُعاد فحصه بالتشغيلة القادمة.
      - لو الخبر غير موجود أصلاً (حُذف يدوياً) → يُحذف من القائمة مع تحذير.
    """
    pending = load_pending_scheduled()
    if not pending:
        return

    log.info(f"🔎 فحص {len(pending)} خبر مجدول من جلسات سابقة...")
    still_pending = []
    notified = 0

    for entry in pending:
        post_id = entry.get("id")
        if not post_id:
            continue
        url = f"{SUPABASE_URL}/rest/v1/{TABLE_NAME}"
        params = {"id": f"eq.{post_id}", "select": "id,status,slug,created_at,published_at"}
        try:
            r = requests.get(url, headers=sb_headers(), params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            log.warning(f"  ⚠️  تعذّر التحقق من الخبر المجدول '{entry.get('title', '')[:50]}': {e}")
            still_pending.append(entry)
            continue

        if r.status_code != 200 or not r.json():
            log.warning(f"  🗑️  الخبر المجدول '{entry.get('title', '')[:50]}' لم يعد موجوداً بقاعدة البيانات — حُذف من قائمة الانتظار.")
            continue

        row = r.json()[0]
        if row.get("status") == "published":
            canonical_url = build_canonical_url(row.get("slug") or entry.get("slug"), row.get("published_at") or row["created_at"])
            if send_to_telegram(entry.get("title", ""), canonical_url):
                log.info(f"  📢 نُشر فعلياً وأُرسل لتيليجرام الآن: {entry.get('title', '')[:60]}")
                notified += 1
            else:
                log.warning(f"  ⚠️  نُشر لكن فشل إرسال تيليجرام، سيُعاد المحاولة لاحقاً: {entry.get('title', '')[:60]}")
                still_pending.append(entry)
        else:
            # لسا scheduled (أو أي حالة ثانية غير published) — نبقيه بالانتظار
            still_pending.append(entry)

    save_pending_scheduled(still_pending)
    log.info(f"🔎 انتهى الفحص: {notified} خبر أُرسل لتيليجرام الآن، {len(still_pending)} لسا بانتظار النشر.")


def get_existing_source_urls() -> set:
    url = f"{SUPABASE_URL}/rest/v1/{TABLE_NAME}"
    params = {"select": "source_url", "source_url": "not.is.null", "limit": "500",
              "order": "created_at.desc"}
    r = requests.get(url, headers=sb_headers(), params=params, timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        log.warning(f"⚠️  تعذّر جلب الروابط الحالية (تأكد من تنفيذ ملف SQL): {r.text[:200]}")
        return set()
    return {row["source_url"] for row in r.json() if row.get("source_url")}


def sb_insert(record: dict) -> Optional[str]:
    """ينشر السجل ويرجّع id السجل المُدرَج (UUID) عند النجاح، أو None عند الفشل.
    ‏?select=id يقيّد الصف المُعاد لعمود id فقط بدل السجل كاملاً (شاملاً
    content الكامل) — نفس الـ id بالضبط، لكن بدون إعادة إرسال نص المقال كله
    عبر الشبكة فور إرساله، توفيراً على Database Egress بكل عملية نشر."""
    url = f"{SUPABASE_URL}/rest/v1/{TABLE_NAME}?select=id"
    delay = 3
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(url, headers={**sb_headers(), "Prefer": "return=representation"},
                               json=record, timeout=REQUEST_TIMEOUT)
            if r.status_code in (200, 201):
                try:
                    data = r.json()
                    return data[0]["id"] if data else None
                except (ValueError, KeyError, IndexError):
                    return None
            if r.status_code in (401, 403):
                log.error(
                    f"❌ رفض الصلاحية [{r.status_code}]: {r.text[:300]}\n"
                    "   على الأغلب RLS يمنع الإدخال بمفتاح anon. نفّذ هذا مؤقتاً بـ SQL Editor:\n"
                    '   CREATE POLICY "posts_bot_insert" ON public.posts\n'
                    '     FOR INSERT WITH CHECK (true);'
                )
                return None
            if r.status_code == 409:
                log.info("   ↳ الخبر منشور مسبقاً (تعارض unique) — تخطي")
                return None
            if r.status_code == 429:
                log.warning(f"Rate limit (429) — انتظار {delay}s ...")
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            log.error(f"Supabase error [{r.status_code}]: {r.text[:300]}")
        except requests.RequestException as e:
            log.warning(f"محاولة {attempt}/{MAX_RETRIES} فشلت: {e}")
        if attempt < MAX_RETRIES:
            time.sleep(delay)
            delay = min(delay * 2, 60)
    return None


# ══════════════════════════════════════════════════════════════════════
#  ✍️  ربط مقالات الرأي بجدول authors (لإظهار بطاقة "بقلم الكاتب" بالموقع)
# ══════════════════════════════════════════════════════════════════════

# كاش بالذاكرة لتفادي استعلام Supabase عن نفس الكاتب أكثر من مرة بنفس التشغيلة
_AUTHOR_ID_CACHE: dict[str, str] = {}


def get_or_create_author_id(author_name: str) -> Optional[str]:
    """يرجّع id الكاتب من جدول authors — يبحث بالاسم أولاً، ولو غير موجود
    ينشئ صفاً جديداً له تلقائياً. هذا الحقل (author_id) هو ما تعتمد عليه
    الواجهة فعلياً لعرض بطاقة "بقلم الكاتب" بمقالات "آراء واتجاهات"
    (PostDetail.tsx يقرأ post.authors عبر join مع author_id، وليس عمود
    author النصي المستقل)."""
    name = (author_name or "").strip()
    if not name:
        return None

    if name in _AUTHOR_ID_CACHE:
        return _AUTHOR_ID_CACHE[name]

    url = f"{SUPABASE_URL}/rest/v1/authors"

    # 1) البحث عن كاتب موجود بنفس الاسم
    try:
        r = requests.get(
            url,
            headers=sb_headers(),
            params={"name": f"eq.{name}", "select": "id", "limit": "1"},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code == 200:
            rows = r.json()
            if rows:
                author_id = rows[0]["id"]
                _AUTHOR_ID_CACHE[name] = author_id
                return author_id
        else:
            log.warning(f"⚠️  تعذّر البحث بجدول authors [{r.status_code}]: {r.text[:200]}")
    except requests.RequestException as e:
        log.warning(f"⚠️  فشل الاتصال أثناء البحث عن الكاتب '{name}': {e}")

    # 2) لم يوجد — إنشاء صف جديد له
    try:
        r = requests.post(
            url,
            headers={**sb_headers(), "Prefer": "return=representation"},
            json={"name": name},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code in (200, 201):
            data = r.json()
            if data:
                author_id = data[0]["id"]
                _AUTHOR_ID_CACHE[name] = author_id
                log.info(f"  ➕ أُنشئ كاتب جديد بجدول authors: {name}")
                return author_id
        elif r.status_code in (401, 403):
            log.error(
                f"❌ رفض الصلاحية عند إنشاء كاتب [{r.status_code}]: {r.text[:300]}\n"
                "   نفّذ هذا مرة واحدة بـ SQL Editor للسماح للبوت بإضافة كتّاب جدد:\n"
                '   CREATE POLICY "authors_bot_insert" ON public.authors\n'
                '     FOR INSERT WITH CHECK (true);'
            )
        else:
            log.warning(f"⚠️  فشل إنشاء كاتب جديد [{r.status_code}]: {r.text[:200]}")
    except requests.RequestException as e:
        log.warning(f"⚠️  فشل الاتصال أثناء إنشاء الكاتب '{name}': {e}")

    return None


# ══════════════════════════════════════════════════════════════════════
#  🖼️  تحميل ومعالجة صور الأخبار ورفعها إلى Supabase Storage
# ══════════════════════════════════════════════════════════════════════

try:
    RESAMPLE_FILTER = Image.Resampling.LANCZOS if Image else None
except AttributeError:  # نسخ Pillow القديمة على بعض بيئات Pydroid 3
    RESAMPLE_FILTER = Image.LANCZOS


def generate_image_filename() -> str:
    """يولّد اسم ملف فريد بنمط: {حروف عشوائية}-{timestamp}.webp"""
    random_chars = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    timestamp = int(time.time())
    return f"{random_chars}-{timestamp}.webp"


def download_image_bytes(image_url: str) -> Optional[bytes]:
    """يحمّل بايتات الصورة الأصلية من رابطها. يجرّب أولاً بروكسي الصور
    المتخصص images.weserv.nl (أسرع وأثبت من البروكسيات العامة تحديداً
    لملفات الصور)، ثم يقع إلى fetch_with_bypass العام لو فشل."""
    try:
        no_scheme = re.sub(r"^https?://", "", image_url)
        weserv_url = f"https://images.weserv.nl/?url={_urlquote(no_scheme, safe='')}"
        r = requests.get(weserv_url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        if r.content and len(r.content) > 500:
            log.info("  🌐 حُمّلت الصورة عبر بروكسي images.weserv.nl")
            return r.content
    except requests.RequestException as e:
        log.warning(f"  ⚠️  فشل بروكسي images.weserv.nl: {e} — أجرّب المسار العادي")

    try:
        r = fetch_with_bypass(
            image_url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        return r.content
    except requests.RequestException as e:
        log.warning(f"  ⚠️  فشل تحميل الصورة من {image_url[:80]}: {e}")
        return None


def compress_image_to_webp(raw_bytes: bytes) -> Optional[bytes]:
    """يعيد ضبط أبعاد الصورة (أقصى 1200px)، يحوّلها WebP، ويضغطها تدريجياً
    (من جودة 85% نزولاً حتى 30%) حتى يصبح حجمها أقل من 100 كيلوبايت."""
    if Image is None:
        log.error("  ❌ مكتبة Pillow غير مثبّتة. نفّذ: pip install pillow")
        return None

    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img.load()
    except Exception as e:
        log.warning(f"  ⚠️  تعذّرت قراءة بيانات الصورة: {e}")
        return None

    # WebP لا يدعم وضع P (باليتة) بشكل جيد، ونحوّل الشفافية RGBA إذا وُجدت
    if img.mode in ("P", "LA"):
        img = img.convert("RGBA")
    elif img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")

    # ضبط الأبعاد القصوى مع الحفاظ على النسبة
    img.thumbnail((IMAGE_MAX_DIMENSION, IMAGE_MAX_DIMENSION), RESAMPLE_FILTER)

    quality = IMAGE_START_QUALITY
    best_attempt: Optional[bytes] = None
    while quality >= IMAGE_MIN_QUALITY:
        buf = io.BytesIO()
        try:
            img.save(buf, format="WEBP", quality=quality, method=6)
        except Exception as e:
            log.warning(f"  ⚠️  فشل ترميز WebP بجودة {quality}%: {e}")
            return None
        data = buf.getvalue()
        best_attempt = data
        if len(data) <= IMAGE_TARGET_MAX_BYTES:
            log.info(f"  🖼️  ضُغطت الصورة بجودة {quality}% → {len(data) / 1024:.1f} كيلوبايت")
            return data
        quality -= IMAGE_QUALITY_STEP

    # ما وصلنا للحجم المطلوب حتى بأدنى جودة — نستخدم آخر محاولة (أصغر حجم متاح)
    if best_attempt is not None:
        log.warning(
            f"  ⚠️  تعذّر الوصول لأقل من {IMAGE_TARGET_MAX_BYTES / 1024:.0f}KB حتى بجودة "
            f"{IMAGE_MIN_QUALITY}% — استُخدمت الصورة بحجم {len(best_attempt) / 1024:.1f}KB"
        )
    return best_attempt


def compress_image_to_square_webp(raw_bytes: bytes) -> Optional[bytes]:
    """يقصّ الصورة من المنتصف لمربّع (1:1)، يصغّرها لـ 600×600، ويضغطها
    تدريجياً حتى أقل من 50 كيلوبايت. تُستخدم بايتات الصورة الأصلية المحمّلة
    أصلاً (raw_bytes) — بدون أي طلب شبكة إضافي، توفيراً لحصة Supabase."""
    if Image is None:
        return None
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img.load()
    except Exception as e:
        log.warning(f"  ⚠️  تعذّرت قراءة بيانات الصورة (نسخة مربّعة): {e}")
        return None

    if img.mode in ("P", "LA"):
        img = img.convert("RGBA")
    elif img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")

    # قصّ مربّع من المنتصف (أقصر ضلع) قبل التصغير — يتفادى تشويه النسبة
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img.thumbnail((IMAGE_SQUARE_DIMENSION, IMAGE_SQUARE_DIMENSION), RESAMPLE_FILTER)

    quality = IMAGE_SQUARE_START_QUALITY
    best_attempt: Optional[bytes] = None
    while quality >= IMAGE_SQUARE_MIN_QUALITY:
        buf = io.BytesIO()
        try:
            img.save(buf, format="WEBP", quality=quality, method=6)
        except Exception as e:
            log.warning(f"  ⚠️  فشل ترميز WebP (نسخة مربّعة) بجودة {quality}%: {e}")
            return None
        data = buf.getvalue()
        best_attempt = data
        if len(data) <= IMAGE_SQUARE_TARGET_MAX_BYTES:
            return data
        quality -= IMAGE_SQUARE_QUALITY_STEP

    return best_attempt


def apply_watermark_to_image(raw_bytes: bytes) -> Optional[bytes]:
    """يطبّق العلامة المائية (شعار الجنوب فويس) على الصورة — نفس خوارزمية
    applyWatermark() بملف src/lib/imageWatermark.ts بالموقع حرفياً:
    1) قصّ مركزي (center-crop) لنسبة 1200×630 (نفس نسبة og-image).
    2) لصق الشعار بالزاوية السفلى اليمنى بحجم 12% من العرض، مع هامش 3%.
    3) شفافية 85% وظل خفيف خلف الشعار.
    4) حفظ النتيجة WebP بجودة 90%.
    يرجّع None لو الشعار غير موجود بـ WATERMARK_LOGO_PATH أو أي خطأ صار
    (بدون إيقاف تشغيل البوت — يُكتفى بالصورة العادية بدون علامة)."""
    if Image is None:
        log.warning("  ⚠️  Pillow غير مثبّتة — تعذّر تطبيق العلامة المائية.")
        return None

    try:
        import os
        if not os.path.isfile(WATERMARK_LOGO_PATH):
            log.warning(f"  ⚠️  ملف الشعار غير موجود بالمسار: {WATERMARK_LOGO_PATH} — تم تجاوز العلامة المائية.")
            return None
    except Exception:
        return None

    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img.load()
    except Exception as e:
        log.warning(f"  ⚠️  تعذّرت قراءة الصورة للعلامة المائية: {e}")
        return None

    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    if img.mode == "RGBA":
        # نحوّل لخلفية بيضاء قبل القصّ عشان ما تطلع الشفافية سوداء بصيغة WebP لاحقاً
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg

    try:
        logo = Image.open(WATERMARK_LOGO_PATH)
        logo.load()
        if logo.mode != "RGBA":
            logo = logo.convert("RGBA")
    except Exception as e:
        log.warning(f"  ⚠️  تعذّرت قراءة ملف الشعار: {e}")
        return None

    # 1) قصّ مركزي لنسبة OG (1200×630) — نفس منطق sourceAspect/targetAspect بالموقع
    target_w, target_h = WATERMARK_OG_WIDTH, WATERMARK_OG_HEIGHT
    target_aspect = target_w / target_h
    src_w, src_h = img.size
    src_aspect = src_w / src_h

    if src_aspect > target_aspect:
        crop_w = int(src_h * target_aspect)
        crop_h = src_h
        left = (src_w - crop_w) // 2
        top = 0
    else:
        crop_w = src_w
        crop_h = int(src_w / target_aspect)
        left = 0
        top = (src_h - crop_h) // 2

    img = img.crop((left, top, left + crop_w, top + crop_h))
    img = img.resize((target_w, target_h), RESAMPLE_FILTER)

    # 2) حساب حجم وموقع الشعار (زاوية سفلى يمنى، بهامش)
    logo_w = int(target_w * WATERMARK_LOGO_SIZE_PERCENT)
    logo_h = int(logo.height * (logo_w / logo.width))
    logo = logo.resize((logo_w, logo_h), RESAMPLE_FILTER)
    padding = int(target_w * WATERMARK_PADDING_PERCENT)
    logo_x = target_w - logo_w - padding
    logo_y = target_h - logo_h - padding

    # 3) ظل خفيف خلف الشعار (تقريب لتأثير shadowBlur بالموقع)
    try:
        from PIL import ImageFilter
        shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        shadow_layer = Image.new("RGBA", (logo_w, logo_h), (0, 0, 0, 150))
        shadow_layer.putalpha(logo.split()[3].point(lambda a: min(a, 150)))
        shadow.paste(shadow_layer, (logo_x + 3, logo_y + 3), shadow_layer)
        shadow = shadow.filter(ImageFilter.GaussianBlur(4))
        img = img.convert("RGBA")
        img = Image.alpha_composite(img, shadow)
    except Exception:
        img = img.convert("RGBA")

    # 4) لصق الشعار نفسه بشفافية 85%
    alpha = logo.split()[3].point(lambda a: int(a * WATERMARK_LOGO_OPACITY))
    logo.putalpha(alpha)
    img.paste(logo, (logo_x, logo_y), logo)

    img = img.convert("RGB")
    buf = io.BytesIO()
    try:
        img.save(buf, format="WEBP", quality=90, method=6)
    except Exception as e:
        log.warning(f"  ⚠️  فشل ترميز الصورة المائية WebP: {e}")
        return None
    return buf.getvalue()


def _headline_shape_ar(text: str) -> str:
    """يهيئ نص عربي للرسم بـPillow (ربط الحروف + اتجاه RTL صحيح) — تُستخدم
    فقط كخط احتياطي عندما لا يدعم Pillow محرك raqm (شوف _headline_text_bbox
    و_headline_draw_text بالأسفل لتفادي التشكيل المزدوج)."""
    if arabic_reshaper is None or _bidi_get_display is None:
        return text
    return _bidi_get_display(arabic_reshaper.reshape(text))


def _headline_text_bbox(draw, text: str, font):
    """يقيس نص عربي. يجرّب أولاً raqm (النص الخام + direction=rtl، تشكيل
    وترتيب تلقائي صحيح لمرة واحدة)، ولو Pillow غير مبني بدعم raqm يرجع
    تلقائياً للتشكيل اليدوي (arabic_reshaper+bidi) لتفادي التشكيل المزدوج
    (الحرف المقلوب/المبعثر) الذي يحدث لو طبّقنا الاثنين معاً."""
    try:
        return draw.textbbox((0, 0), text, font=font, direction="rtl", language="ar")
    except Exception:
        return draw.textbbox((0, 0), _headline_shape_ar(text), font=font)


def _headline_draw_text(draw, xy, text: str, font, fill):
    try:
        draw.text(xy, text, font=font, fill=fill, direction="rtl", language="ar")
    except Exception:
        draw.text(xy, _headline_shape_ar(text), font=font, fill=fill)


def _headline_wrap_text(draw, text: str, font, max_width: int) -> list:
    words = text.split(" ")
    lines, cur = [], ""
    for w in words:
        test = (cur + " " + w).strip()
        bbox = _headline_text_bbox(draw, test, font)
        if bbox[2] - bbox[0] <= max_width or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _headline_draw_band(size: tuple, band_h: int) -> "Image.Image":
    """يرسم شريطاً سفلياً بحافة علوية مستقيمة (أسلوب القنوات العالمية:
    BBC/Reuters/Al Jazeera) بدل الموجة، مع تدرّج كحلي داخل الشريط (نفس
    هوية هيدر aljnoubvoice.com) وتدرّج تعتيم ناعم إضافي فوقه يذوب داخل
    الصورة الأصلية بدل القطع الفجائي، وخط حافة أحمر رفيع مستقيم يفصل
    الصورة عن الشريط."""
    w, h = size
    top_y = h - band_h
    fade_h = min(HEADLINE_FADE_H, top_y)
    fade_top = top_y - fade_h

    top_rgb = HEADLINE_BAND_COLOR_TOP[:3]
    bot_rgb = HEADLINE_BAND_COLOR_BOTTOM[:3]

    band = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    band_draw = ImageDraw.Draw(band)

    # تدرّج تعتيم ناعم فوق الشريط (يذوب تدريجياً داخل الصورة الأصلية)
    for y in range(fade_top, top_y):
        t = (y - fade_top) / max(1, fade_h)
        alpha = int(255 * (t ** 1.4))
        band_draw.line([(0, y), (w, y)], fill=(*top_rgb, alpha))

    # تدرّج كحلي رأسي عبر كامل ارتفاع الشريط (معتم بالكامل)
    span = max(1, h - top_y)
    for y in range(top_y, h):
        t = (y - top_y) / span
        r = int(top_rgb[0] * (1 - t) + bot_rgb[0] * t)
        g = int(top_rgb[1] * (1 - t) + bot_rgb[1] * t)
        b = int(top_rgb[2] * (1 - t) + bot_rgb[2] * t)
        band_draw.line([(0, y), (w, y)], fill=(r, g, b, 255))

    # خط الحافة العلوية — مستقيم رفيع بدل المنحنى
    band_draw.rectangle(
        [0, top_y - HEADLINE_LINE_THICKNESS, w, top_y],
        fill=HEADLINE_CURVE_COLOR,
    )
    return band


def apply_headline_design_to_image(raw_bytes: bytes, headline_text: str) -> Optional[bytes]:
    """يصمم صورة الخبر بنفس أسلوب المواقع المنافسة: قصّ الصورة لنسبة OG،
    ثم إضافة شريط سفلي منحني فيه عنوان الخبر (مُشكَّل عربياً بشكل صحيح)
    مع شعار الموقع. يرجّع None عند أي فشل (خط ناقص/شعار ناقص/مكتبة ناقصة)
    بدون إيقاف تشغيل البوت — يُكتفى حينها بالصورة العادية."""
    if Image is None or ImageDraw is None or ImageFont is None:
        log.warning("  ⚠️  Pillow غير مكتملة (Image/ImageDraw/ImageFont) — تعذّر تصميم صورة العنوان.")
        return None
    if arabic_reshaper is None or _bidi_get_display is None:
        log.warning("  ⚠️  مكتبات النص العربي غير مثبّتة (pip install arabic-reshaper python-bidi).")
        return None

    import os
    if not os.path.isfile(HEADLINE_FONT_PATH):
        log.warning(f"  ⚠️  خط العنوان غير موجود بالمسار: {HEADLINE_FONT_PATH} — تعذّر تصميم صورة العنوان.")
        return None

    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img.load()
    except Exception as e:
        log.warning(f"  ⚠️  تعذّرت قراءة الصورة لتصميم العنوان: {e}")
        return None

    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg

    # قصّ مركزي لنسبة OG (نفس منطق العلامة المائية بالضبط)
    target_w, target_h = WATERMARK_OG_WIDTH, WATERMARK_OG_HEIGHT
    target_aspect = target_w / target_h
    src_w, src_h = img.size
    src_aspect = src_w / src_h
    if src_aspect > target_aspect:
        crop_w = int(src_h * target_aspect)
        crop_h = src_h
        left = (src_w - crop_w) // 2
        top = 0
    else:
        crop_w = src_w
        crop_h = int(src_w / target_aspect)
        left = 0
        top = (src_h - crop_h) // 2
    img = img.crop((left, top, left + crop_w, top + crop_h))
    img = img.resize((target_w, target_h), RESAMPLE_FILTER)
    img = img.convert("RGBA")

    try:
        headline_font = ImageFont.truetype(HEADLINE_FONT_PATH, HEADLINE_FONT_SIZE)
    except Exception as e:
        log.warning(f"  ⚠️  تعذّر تحميل خط العنوان: {e}")
        return None

    # حافة الشريط العلوية أصبحت خطاً مستقيماً (لا انحناء)، فالهامش العلوي
    # يحتاج فقط تنفّساً بصرياً بسيطاً فوق الخط، لا تغطية قمم موجة.
    TOP_PAD = 26
    BOTTOM_PAD = 20
    LEFT_MARGIN = 30
    RIGHT_MARGIN = 40
    GAP_BETWEEN = 30  # فاصل أفقي بين كتلة الشعار وكتلة العنوان
    DIVIDER_GAP = 26  # مسافة الفاصل الرفيع بين كتلة الشعار/الاسم وكتلة العنوان
    ACCENT_BAR_W = 4  # عرض الشريط الأحمر الصغير (kicker) جنب العنوان
    ACCENT_GAP = 18   # مسافة بين الشريط الأحمر وبداية نص العنوان
    LOGO_SIZE = 108

    # قياس مسبق (بدون رسم فعلي) لعرض كتلة الشعار+الاسم — هذا العرض لا
    # يعتمد على ارتفاع الشريط، فنحسبه أولاً لمعرفة العرض المتاح للعنوان.
    measure_draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    logo_exists = os.path.isfile(WATERMARK_LOGO_PATH)
    left_block_end_x = LEFT_MARGIN
    if logo_exists:
        try:
            site_font = ImageFont.truetype(HEADLINE_FONT_PATH, 45)
        except Exception:
            site_font = headline_font
        site_gap = 22
        site_x = LEFT_MARGIN + LOGO_SIZE + site_gap
        site_bbox = _headline_text_bbox(measure_draw, HEADLINE_SITE_NAME, site_font)
        left_block_end_x = site_x + (site_bbox[2] - site_bbox[0]) + DIVIDER_GAP
    accent_x = left_block_end_x + GAP_BETWEEN
    text_start_x = accent_x + ACCENT_BAR_W + ACCENT_GAP
    max_text_w = max(100, target_w - RIGHT_MARGIN - text_start_x)

    # العنوان يُعرض كاملاً دائماً مهما طال — بدون أي قص أو حذف كلمات.
    # نجرّب أولاً بحجم الخط الافتراضي؛ إن لم يتسع بعدد الأسطر المفضّل
    # (HEADLINE_MAX_LINES) نصغّر الخط تدريجياً حتى الحد الأدنى، وإن ظل
    # النص أطول من ذلك نكتفي بأكبر عدد أسطر ينتجه الحد الأدنى — كل الكلمات
    # تبقى محفوظة داخل الأسطر (bidi/wrap لا يحذف كلمات أبداً، فقط يلفّها).
    headline_text = headline_text.strip()
    font_size = HEADLINE_FONT_SIZE
    while True:
        try:
            headline_font = ImageFont.truetype(HEADLINE_FONT_PATH, font_size)
        except Exception as e:
            log.warning(f"  ⚠️  تعذّر تحميل خط العنوان: {e}")
            return None
        lines = _headline_wrap_text(measure_draw, headline_text, headline_font, max_text_w)
        if len(lines) <= HEADLINE_MAX_LINES or font_size <= HEADLINE_FONT_MIN_SIZE:
            break
        font_size -= 2

    line_h = int(font_size * 1.25)
    text_block_h = line_h * len(lines)

    # ارتفاع الشريط: يبدأ من النسبة الافتراضية (يحافظ على الشكل المعتاد
    # للعناوين القصيرة)، ويكبر تلقائياً إن احتاج العنوان مساحة أكثر —
    # بحد أقصى يترك جزءاً من الصورة الأصلية ظاهراً فوق الشريط دائماً.
    default_band_h = int(target_h * 0.33)
    max_band_h = target_h - HEADLINE_MIN_PHOTO_VISIBLE
    required_band_h = TOP_PAD + BOTTOM_PAD + max(LOGO_SIZE, text_block_h)
    band_h = max(default_band_h, min(required_band_h, max_band_h))

    band = _headline_draw_band((target_w, target_h), band_h)
    img = Image.alpha_composite(img, band)
    draw = ImageDraw.Draw(img)

    row_top = target_h - band_h + TOP_PAD
    row_bottom = target_h - BOTTOM_PAD
    row_center_y = (row_top + row_bottom) // 2

    def _vcenter_y(bbox, center_y):
        # يحسب y بحيث يقع المركز الرأسي الفعلي للنص (حسب bbox) على center_y
        return center_y - (bbox[1] + bbox[3]) // 2

    # شعار الموقع + اسمه يسار الصف (يعيد استخدام WATERMARK_LOGO_PATH)
    try:
        if logo_exists:
            logo = Image.open(WATERMARK_LOGO_PATH).convert("RGBA")
            logo.thumbnail((LOGO_SIZE, LOGO_SIZE))
            logo_x = LEFT_MARGIN
            logo_y = row_center_y - logo.height // 2
            img.paste(logo, (logo_x, logo_y), logo)

            # مساحة تنفّس بسيطة بدل الخط العمودي الثقيل بين الشعار والاسم
            site_gap = 22
            site_x = logo_x + logo.width + site_gap

            try:
                site_font = ImageFont.truetype(HEADLINE_FONT_PATH, 45)
            except Exception:
                site_font = headline_font
            site_bbox = _headline_text_bbox(draw, HEADLINE_SITE_NAME, site_font)
            site_y = _vcenter_y(site_bbox, row_center_y)
            _headline_draw_text(draw, (site_x, site_y), HEADLINE_SITE_NAME, site_font, fill=HEADLINE_SITE_COLOR)

            # فاصل رفيع خفيف بين كتلة الشعار/الاسم وكتلة العنوان (بدل تكرار
            # نفس الخط الثقيل) — يمنح توازناً واضحاً بين يمين الصورة ويسارها
            divider_x = site_x + (site_bbox[2] - site_bbox[0]) + DIVIDER_GAP
            draw.line(
                [(divider_x, row_top + 6), (divider_x, row_bottom - 6)],
                fill=HEADLINE_DIVIDER_COLOR, width=1,
            )
        else:
            log.warning(f"  ⚠️  شعار الموقع غير موجود بالمسار: {WATERMARK_LOGO_PATH} — سيُنشر بدون شعار.")
    except Exception as e:
        log.warning(f"  ⚠️  تعذّر رسم الشعار على صورة العنوان: {e}")

    # عنوان الخبر يمين الصف، بنفس مستوى الشعار أفقياً — كل الأسطر تُرسم
    # كاملة (lines أعلاه لا يُقصّ إطلاقاً)
    start_y = row_center_y - text_block_h // 2
    right_edge = target_w - RIGHT_MARGIN

    # شريط أحمر عمودي صغير (kicker) يفتح كتلة العنوان — لمسة القنوات العالمية
    draw.rectangle(
        [accent_x, start_y, accent_x + ACCENT_BAR_W, start_y + text_block_h],
        fill=HEADLINE_ACCENT_COLOR,
    )

    for i, line in enumerate(lines):
        bbox = _headline_text_bbox(draw, line, headline_font)
        tw = bbox[2] - bbox[0]
        x = right_edge - tw
        y = start_y + i * line_h
        _headline_draw_text(draw, (x, y), line, headline_font, fill=HEADLINE_TEXT_COLOR)

    img = img.convert("RGB")
    buf = io.BytesIO()
    try:
        img.save(buf, format="WEBP", quality=90, method=6)
    except Exception as e:
        log.warning(f"  ⚠️  فشل ترميز صورة العنوان WebP: {e}")
        return None
    return buf.getvalue()


def upload_image_to_supabase(image_bytes: bytes, filename: str) -> Optional[str]:
    """يرفع بايتات صورة WebP إلى Supabase Storage داخل bucket العام، ويرجّع
    الرابط العام (Public URL) عند النجاح."""
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_IMAGE_BUCKET}/{filename}"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "image/webp",
        "x-upsert": "true",
        "cache-control": "31536000",
    }
    try:
        r = requests.post(upload_url, headers=headers, data=image_bytes, timeout=REQUEST_TIMEOUT)
        if r.status_code in (200, 201):
            public_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_IMAGE_BUCKET}/{filename}"
            return public_url
        log.error(f"  ❌ فشل رفع الصورة إلى Supabase Storage [{r.status_code}]: {r.text[:200]}")
        return None
    except requests.RequestException as e:
        log.error(f"  ❌ خطأ اتصال أثناء رفع الصورة: {e}")
        return None


OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
OG_IMAGE_RE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image(?::secure_url)?["\']',
    re.IGNORECASE,
)
TWITTER_IMAGE_RE = re.compile(
    r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
TWITTER_IMAGE_RE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
    re.IGNORECASE,
)


def fetch_og_image(article_url: str) -> Optional[str]:
    """يجلب صفحة الخبر نفسها (وليس الفيد) ويستخرج رابط الصورة من وسم
    og:image أو twitter:image بترويسة الصفحة (<head>). يُستخدم فقط كخط
    احتياطي عندما لا يزوّد الفيد أي رابط صورة إطلاقاً (مثل مصدر RSS المساء
    الذي لا يضمّن الصورة البارزة داخل عناصر الفيد أبداً). يعني طلب شبكة
    إضافي واحد لكل خبر من هذا النوع بس، ولا يوقف تشغيل البوت لو فشل."""
    if not article_url:
        return None
    try:
        r = fetch_with_bypass(article_url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        page_html = r.text
    except requests.RequestException as e:
        log.warning(f"  ⚠️  فشل جلب صفحة الخبر لاستخراج og:image: {e}")
        return None

    for pattern in (OG_IMAGE_RE, OG_IMAGE_RE_ALT, TWITTER_IMAGE_RE, TWITTER_IMAGE_RE_ALT):
        m = pattern.search(page_html)
        if m:
            url = html.unescape(m.group(1).strip())
            if url:
                log.info(f"  🔎 وُجدت صورة عبر og:image/twitter:image من صفحة الخبر: {url[:90]}")
                return url

    log.info("  ℹ️  لا يوجد og:image ولا twitter:image بصفحة الخبر أيضاً.")
    return None


_BLOCKED_LOGO_HASHES_CACHE: Optional[list] = None
_BLOCKED_LOGO_ORB_TEMPLATES_CACHE: Optional[list] = None

# لا يُقبل الحظر الموضعي إلا بعد تحقق هندسي محافظ مع صورة مرجعية محظورة.
LOGO_ORB_FEATURES = 3000
LOGO_ORB_RATIO_TEST = 0.78
LOGO_ORB_MIN_INLIERS = 6


def _average_hash(img: "Image.Image") -> int:
    """يحسب بصمة مرئية بسيطة (average hash) للصورة: يحوّلها لتدرج رمادي،
    يصغّرها لـLOGO_HASH_SIZE×LOGO_HASH_SIZE، ويقارن كل بكسل بالمتوسط لبناء
    رقم ثنائي (bit لكل بكسل). صور متشابهة بصرياً تُنتج بصمات متقاربة."""
    small = img.convert("L").resize((LOGO_HASH_SIZE, LOGO_HASH_SIZE), Image.LANCZOS)
    pixels = list(small.getdata())
    avg = sum(pixels) / len(pixels)
    bits = "".join("1" if p > avg else "0" for p in pixels)
    return int(bits, 2)


def _hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _load_local_blocked_logo_hashes() -> list:
    """يقرأ كل صور الشعار من BLOCKED_LOGOS_DIR (مجلد الريبو المحلي) ويحسب
    بصمتها. يرجّع list فارغة لو المجلد غير موجود أو فارغ، بدون إيقاف البوت."""
    hashes = []
    if Image is None or not os.path.isdir(BLOCKED_LOGOS_DIR):
        return hashes

    for filename in os.listdir(BLOCKED_LOGOS_DIR):
        if not filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            continue
        path = os.path.join(BLOCKED_LOGOS_DIR, filename)
        try:
            with Image.open(path) as logo_img:
                hashes.append(_average_hash(logo_img))
        except Exception as e:
            log.warning(f"⚠️  تعذّر قراءة صورة الشعار ({filename}): {e}")

    log.info(f"🖼️  تم تحميل {len(hashes)} صورة شعار محظور محلياً من {BLOCKED_LOGOS_DIR}")
    return hashes


def _load_supabase_blocked_logo_hashes() -> list:
    """يجلب بصمات شعارات محظورة إضافية من جدول Supabase (اختياري، مُدار عن
    بُعد بدون إعادة نشر الريبو). أي فشل (الجدول غير موجود، مشكلة اتصال...)
    يرجّع list فارغة بدون إيقاف البوت أو حتى تسجيل خطأ مزعج."""
    hashes = []
    try:
        url = f"{SUPABASE_URL}/rest/v1/{BLOCKED_LOGO_HASHES_TABLE}"
        r = requests.get(
            url, headers=sb_headers(), params={"select": "hash_hex"},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            log.info(
                f"ℹ️  تعذّر جلب بصمات شعارات إضافية من Supabase "
                f"({BLOCKED_LOGO_HASHES_TABLE}) — سيُعتمَد على المجلد المحلي فقط."
            )
            return hashes
        for row in r.json():
            hex_val = row.get("hash_hex")
            if not hex_val:
                continue
            try:
                hashes.append(int(hex_val, 16))
            except (TypeError, ValueError):
                log.warning(f"⚠️  بصمة غير صالحة بجدول {BLOCKED_LOGO_HASHES_TABLE}: {hex_val!r}")
        if hashes:
            log.info(f"🖼️  تم تحميل {len(hashes)} بصمة شعار محظور إضافية من Supabase")
    except Exception as e:
        log.info(f"ℹ️  تعذّر الاتصال بجدول {BLOCKED_LOGO_HASHES_TABLE} بـSupabase: {e}")
    return hashes


def _load_blocked_logo_hashes() -> list:
    """يجمع بصمات الشعارات المحظورة من المصدرين معاً: المجلد المحلي
    (BLOCKED_LOGOS_DIR، الحماية الأصلية) + جدول Supabase (حماية إضافية عن
    بُعد). النتيجة مخزّنة بذاكرة التشغيل (_BLOCKED_LOGO_HASHES_CACHE) بعد أول
    استدعاء. فشل أحد المصدرين لا يوقف الآخر — الفحص يستمر بما هو متوفر."""
    global _BLOCKED_LOGO_HASHES_CACHE
    if _BLOCKED_LOGO_HASHES_CACHE is not None:
        return _BLOCKED_LOGO_HASHES_CACHE

    hashes = _load_local_blocked_logo_hashes() + _load_supabase_blocked_logo_hashes()
    _BLOCKED_LOGO_HASHES_CACHE = hashes
    return hashes


def _load_local_blocked_logo_orb_templates() -> list:
    """يجهّز خصائص ملفات صور الحظر الموجودة في blocked_logos فقط.

    لا يدخل عنوان الخبر أو رابط مصدره في المطابقة؛ كل قالب هنا مصدره ملف أضافه
    المسؤول صراحةً إلى قائمة الحظر المحلية.
    """
    global _BLOCKED_LOGO_ORB_TEMPLATES_CACHE
    if _BLOCKED_LOGO_ORB_TEMPLATES_CACHE is not None:
        return _BLOCKED_LOGO_ORB_TEMPLATES_CACHE

    templates = []
    if cv2 is None or not os.path.isdir(BLOCKED_LOGOS_DIR):
        _BLOCKED_LOGO_ORB_TEMPLATES_CACHE = templates
        return templates

    detector = cv2.ORB_create(
        nfeatures=LOGO_ORB_FEATURES,
        scaleFactor=1.12,
        nlevels=12,
        fastThreshold=5,
    )
    for filename in sorted(os.listdir(BLOCKED_LOGOS_DIR)):
        if not filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            continue
        try:
            image = cv2.imread(os.path.join(BLOCKED_LOGOS_DIR, filename), cv2.IMREAD_GRAYSCALE)
            if image is None:
                raise ValueError("لا يمكن فك ترميز الصورة")
            keypoints, descriptors = detector.detectAndCompute(image, None)
            if descriptors is not None and len(keypoints) >= 4:
                templates.append((filename, keypoints, descriptors))
        except Exception as e:
            log.warning(f"⚠️  تعذّر تجهيز مرجع الحظر الموضعي ({filename}): {e}")

    log.info(f"🖼️  تم تجهيز {len(templates)} مرجع حظر موضعي من blocked_logos.")
    _BLOCKED_LOGO_ORB_TEMPLATES_CACHE = templates
    return templates


def _matches_local_blocked_logo_template(raw_bytes: bytes) -> bool:
    """يتحقق من تشابه موضعي مع ملفات الحظر المرجعية فقط.

    لا تؤثر النتيجة على نشر الخبر: عند التطابق يترك مسار الصورة حقلي الغلاف
    والصورة المصغرة فارغين، بينما يبقى السجل نفسه قابلاً للنشر.
    """
    if cv2 is None or np is None:
        log.warning("⚠️  OpenCV غير متاح؛ سيُستخدم فحص البصمة الكاملة فقط.")
        return False

    templates = _load_local_blocked_logo_orb_templates()
    if not templates:
        return False

    try:
        target = cv2.imdecode(np.frombuffer(raw_bytes, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if target is None:
            raise ValueError("لا يمكن فك ترميز الصورة المُنزَّلة")

        detector = cv2.ORB_create(
            nfeatures=LOGO_ORB_FEATURES,
            scaleFactor=1.12,
            nlevels=12,
            fastThreshold=5,
        )
        target_keypoints, target_descriptors = detector.detectAndCompute(target, None)
        if target_descriptors is None or len(target_keypoints) < 4:
            return False

        matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        for filename, reference_keypoints, reference_descriptors in templates:
            pairs = matcher.knnMatch(reference_descriptors, target_descriptors, k=2)
            good_matches = [
                pair[0] for pair in pairs
                if len(pair) == 2 and pair[0].distance < LOGO_ORB_RATIO_TEST * pair[1].distance
            ]
            if len(good_matches) < 4:
                continue

            reference_points = np.float32(
                [reference_keypoints[match.queryIdx].pt for match in good_matches]
            ).reshape(-1, 1, 2)
            target_points = np.float32(
                [target_keypoints[match.trainIdx].pt for match in good_matches]
            ).reshape(-1, 1, 2)
            _, inlier_mask = cv2.findHomography(reference_points, target_points, cv2.RANSAC, 5.0)
            inliers = int(inlier_mask.sum()) if inlier_mask is not None else 0
            if inliers >= LOGO_ORB_MIN_INLIERS:
                log.info(
                    f"  🚫 تطابق موضعي مع مرجع حظر صريح ({filename}; "
                    f"{inliers} تطابقات هندسية) — لن تُرفع الصورة."
                )
                return True
    except Exception as e:
        log.warning(f"⚠️  تعذّر فحص التطابق الموضعي للصورة: {e}")

    return False


def image_contains_blocked_logo(raw_bytes: bytes) -> bool:
    """يفحص فقط مراجع الحظر التي أضيفت صراحةً.

    لا يفحص نطاق المصدر أو اسم الموقع أو عنوان الخبر. تطابق الصورة يمنع رفعها
    فقط؛ الخبر نفسه لا يُحظر ولا تتغير حالة نشره.
    """
    reference_hashes = _load_blocked_logo_hashes()
    if Image is not None and reference_hashes:
        try:
            with Image.open(io.BytesIO(raw_bytes)) as img:
                img_hash = _average_hash(img.convert("RGB"))
            for ref_hash in reference_hashes:
                if _hamming_distance(img_hash, ref_hash) <= LOGO_MATCH_MAX_DISTANCE:
                    log.info("  🚫 تطابق مباشر مع صورة حظر مرجعية — لن تُرفع الصورة.")
                    return True
        except Exception as e:
            log.warning(f"⚠️  تعذّر فحص بصمة الصورة: {e}")

    return _matches_local_blocked_logo_template(raw_bytes)


def get_post_image_url(
    source_image_url: Optional[str], article_url: Optional[str] = None,
    apply_watermark: bool = False, headline_text: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """يدير خط أنابيب الصورة كاملاً: تحميل → معالجة/ضغط → رفع إلى Supabase.
    يرجّع (رابط الصورة الرئيسية, رابط النسخة المربّعة) — أي منهما None عند
    أي فشل (بدون إيقاف تشغيل البوت). النسخة المربّعة تُنتج فقط لو
    GENERATE_SQUARE_IMAGE_VARIANT مفعّلة، ومن نفس البايتات المحمّلة (بدون
    طلب شبكة إضافي).
    لو الفيد ما زوّد رابط صورة إطلاقاً (source_image_url فارغ) ومُرِّر article_url،
    يُحاول جلب og:image من صفحة الخبر نفسها كخط احتياطي أخير.
    لو apply_watermark=True: تُلصق علامة الجنوب فويس المائية على الصورة
    الرئيسية قبل الضغط والرفع (نفس منطق imageWatermark.ts بالموقع تماماً)
    — النسخة المربّعة تبقى دائماً بدون علامة مائية (مخصصة لـ thumbnails فقط)."""
    if not source_image_url and article_url:
        log.info("  ℹ️  لا يوجد رابط صورة بالفيد — محاولة جلبها من صفحة الخبر مباشرة (og:image)...")
        source_image_url = fetch_og_image(article_url)

    if not source_image_url:
        log.info("  ℹ️  لا يوجد رابط صورة بهذا الخبر — سيُترك حقل image_url فارغاً.")
        return None, None

    log.info(f"  🔗 رابط الصورة الأصلي: {source_image_url[:90]}")

    raw_bytes = download_image_bytes(source_image_url)
    if not raw_bytes:
        log.warning("  ⚠️  تعذّر تحميل الصورة — سيُترك حقل image_url فارغاً.")
        return None, None

    if image_contains_blocked_logo(raw_bytes):
        log.info("  🚫 الصورة تحتوي شعار مصدر ممنوع (تطابق مع blocked_logos) — لن تُنشر، سيُترك حقل image_url فارغاً.")
        return None, None

    webp_bytes = None
    if headline_text and HEADLINE_DESIGN_ENABLED:
        webp_bytes = apply_headline_design_to_image(raw_bytes, headline_text)
        if webp_bytes:
            log.info("  📰  صُممت صورة الخبر بشريط العنوان.")
        else:
            log.warning("  ⚠️  تعذّر تصميم صورة العنوان — سيُتابع للخيار التالي.")

    if not webp_bytes and apply_watermark:
        webp_bytes = apply_watermark_to_image(raw_bytes)
        if webp_bytes:
            log.info("  🖋️  طُبّقت العلامة المائية على الصورة الرئيسية.")
        else:
            log.warning("  ⚠️  تعذّر تطبيق العلامة المائية — سيُنشر بالصورة العادية بدون علامة.")

    if not webp_bytes:
        webp_bytes = compress_image_to_webp(raw_bytes)

    if not webp_bytes:
        log.warning("  ⚠️  تعذّرت معالجة/ضغط الصورة — سيُترك حقل image_url فارغاً.")
        return None, None

    filename = generate_image_filename()
    public_url = upload_image_to_supabase(webp_bytes, filename)
    if not public_url:
        log.warning("  ⚠️  فشل رفع الصورة إلى Supabase — سيُترك حقل image_url فارغاً.")
        return None, None

    log.info(f"  ✅ رُفعت الصورة بنجاح: {filename}")

    square_url = None
    if GENERATE_SQUARE_IMAGE_VARIANT:
        square_bytes = compress_image_to_square_webp(raw_bytes)
        if square_bytes:
            square_filename = filename.replace(".webp", "-square.webp")
            square_url = upload_image_to_supabase(square_bytes, square_filename)
            if square_url:
                log.info(f"  ✅ رُفعت النسخة المربّعة ({len(square_bytes) / 1024:.1f}KB): {square_filename}")
            else:
                log.warning("  ⚠️  فشل رفع النسخة المربّعة — سيُكتفى بالصورة الرئيسية.")
        else:
            log.warning("  ⚠️  تعذّر توليد النسخة المربّعة — سيُكتفى بالصورة الرئيسية.")

    return public_url, square_url


# ══════════════════════════════════════════════════════════════════════
#  🤖  Gemini — إعادة الصياغة
# ══════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """\
أنت محرر صحفي في موقع إخباري جنوبي مستقل. تصلك نصوص أخبار خام فتعيد كتابتها من الصفر كما يفعل محرر بشري محترف يمتلك كامل المهارات الصحفية.

الخطوة الصفر — تصفية المصدر:
قبل أي كتابة، اقرأ الخبر الخام وحدّد بصمة مصدره: حوثي، إخواني، إماراتي، سعودي، حكومي، انتقالي، أو غيره. ثم جرّد الخبر تماماً من زاويته ولغته وتوظيفه، واحتفظ بالوقائع فقط: من، ماذا، أين، متى، وما التداعي على المواطن. لا تنقل أجندة المصدر ولو بدت منسجمة مع الهوية التحريرية للموقع.

الهوية التحريرية:
صوت المواطن الجنوبي المستقل، لا ناطق بأجندة أي طرف
تفضح الفساد والتبعية للتحالف السعودي عبر الوقائع لا التصريحات المباشرة
لتعكس معاناة المواطن الجنوبي

المصطلحات الإلزامية:
"الحكومة المدعومة من التحالف" لا "الشرعية"
"التحالف بقيادة السعودية" لا "التحالف العربي"
"قوات مجلس القيادة" أو "القوات المدعومة سعودياً" لا "قوات الشرعية"
"تسيطر على" لا "تحرر"
"القوات الإخوانية" أو "قوات تنظيم الإخوان" لا "قوات الإصلاح"
المجلس الانتقالي يُذكر باسمه فقط بلا ألقاب تمجيدية
"سلطة مجلس القيادة الرئاسي" للسلطة المحلية في عدن المدعومة سعودياً
"الحاكم العسكري السعودي الشهراني" عند الإشارة إليه

الحكم التحريري — لا قالب ثابت:
كل خبر له شكله الذي يخدم مضمونه:
الخبر العاجل القصير: مباشر ومكثف بلا حشو
الخبر الميداني: يتصاعد من الواقعة إلى التداعية
التقرير التحليلي: طبقات متراكمة، وقائع ثم سياق ثم دلالة
الخبر الاقتصادي: أرقام مربوطة بوجع إنساني حقيقي
الخبر السياسي: التصريح في مواجهة الواقع المعاش
الخبر الأمني والعسكري: الضحايا والتداعيات أولاً

إخفاء بصمة الآلة — شرط غير قابل للتنازل:
تفاوت حقيقي في طول الجمل وإيقاعها
لا تكرار في مفتتحات الفقرات أو الأساليب
لا نبرة ثابتة تكشف مصدراً واحداً
أحياناً جملة قصيرة مباغتة وسط نص طويل
أحياناً مفتتح هادئ يتصاعد، وأحياناً مفتتح صادم مباشر
المتابع يشعر أن محررين بشريين مختلفين كتبوا الأخبار
قواعد الكتابة الثابتة:
أعد الكتابة من الصفر بلغتك الخاصة تماماً، لا تترجم ولا تعيد ترتيب
أول جملة في content هي الملخص التلقائي — قوية ومكثفة وتنتهي بنقطة
ابدأ بالأهم: المواطن، التداعيات، الوقائع
اختم بما يربط المشهد بحقيقته الأعمق دون خطابية مبتذلة
"""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "excerpt": {"type": "STRING"},
        "content": {"type": "STRING"},
        "houthi_iran_exclude": {"type": "BOOLEAN"},
    },
    "required": ["title", "excerpt", "content", "houthi_iran_exclude"],
}

# ── مخطط وبرومبت خاصّان بإعادة صياغة العنوان فقط (لمقالات الرأي المستثناة) ──
TITLE_ONLY_SCHEMA = {
    "type": "OBJECT",
    "properties": {"title": {"type": "STRING"}},
    "required": ["title"],
}

TITLE_ONLY_PROMPT = """\
أنت محرر عناوين في موقع إخباري جنوبي مستقل. سيصلك عنوان ونص مقال رأي منسوب
لكاتبه بالاسم. مهمتك الوحيدة: أعد صياغة العنوان فقط بأسلوب صحفي جذاب ودقيق
يعكس فحوى المقال، بنفس المصطلحات التحريرية لموقعنا (مثال: "الحكومة المعترف
بها دولياً" لا "الشرعية"، "التحالف بقيادة السعودية" لا "التحالف العربي").
⚠️ ممنوع المساس بأي كلمة من نص المقال نفسه — مهمتك تقتصر على العنوان حصراً،
ولا تلخّص المقال ولا تُبدي رأياً فيه، فقط أعد صياغة عنوانه.
أعد النص فقط بصيغة JSON دون أي شرح إضافي.

العنوان الأصلي:
{title}

نص المقال (للسياق فقط، لا تعدّل عليه):
{body}

أعد كائن JSON: {{title}}
"""


def build_title_only_prompt(title: str, body: str) -> str:
    return TITLE_ONLY_PROMPT.format(title=title, body=body[:4000])


def build_prompt(title: str, body: str, category: str, source_feed: Optional[str] = None) -> str:
    """
    بناء البرومبت الذكي بناءً على تصنيف الخبر ومحتواه لضمان التوازن بين
    الهوية التحريرية لـ "جنوب فويس" والمحددات الأمنية والخدمية والرياضية.
    """
    raw_body = body[:8000]
    cat = category.strip()

    # 1. قائمة الأقسام المستثناة تماماً من الخط التحريري السياسي لـ "جنوب فويس"
    neutral_categories = ["الرياضة", "رياضة", "منوعات", "شؤون دولية", "أسعار الصرف", "أسعار صرف العملات", "الذهب"]

    # 2. الكلمات المفتاحية الحساسة الخاصة بسلطات صنعاء والحوثيين (للتبريد التحريري والأمني)
    sanaa_keywords = ["حوثي", "الحوثي", "صنعاء", "أنصار الله", "المليشيا", "المشاط", "الحوثيين", "اللجنة الثورية"]

    # 3. فلتر إلزامي صارم بشأن الحوثي/أنصار الله/إيران — يُطبَّق على المسارات
    # الثلاثة كلها عبر json_instruction المشتركة بالأسفل، بمعزل عن أي تبريد
    # لهجة سابق بالمسار الثاني (ذاك يُبقي على ذكرهم بلهجة محايدة، وهذا فلتر
    # إضافي أشد صرامة: يستبعد الخبر كلياً لو كان محوره هجومياً خالصاً عليهم).
    # البند 2 (إشارة عابرة بخبر مهم) له نسختان حسب المصدر:
    #   - فيد الجزيرة نت اليمن (RSS_ALJAZEERA_YEMEN_URL) فقط: يستبدل الذكر
    #     بـ"صنعاء" أو "قوات صنعاء" (حسب السياق ميداني/عام)، بدون كلمة
    #     "جماعة" ولا "أنصار الله" ولا "الحوثي" مباشرة، مقصور على هذا المصدر.
    #   - أي مصدر آخر (عدن تايم، المساء برس، إلخ): يبقى السلوك الأصلي —
    #     حذف الإشارة نهائياً من النص بلا ذكر أي اسم أو مصطلح بديل.
    is_aljazeera_source = (source_feed == RSS_ALJAZEERA_YEMEN_URL)

    if is_aljazeera_source:
        rule_2 = """2) إذا كان الخبر مهماً وله وقائع وتفاصيل فعلية (قرار، حدث ميداني، بيان، تعيين، إلخ) لكنه يتضمن فقط إشارة أو ذكراً ضمن سياقه للحوثي/أنصار الله/إيران دون أن يكون محور الخبر هجوماً عليهم — اجعل "houthi_iran_exclude" = false، وأعد صياغة العنوان والمتن معاً بحيث لا تُستخدم كلمة "جماعة" ولا "أنصار الله" ولا "الحوثي" ولا "إيران" بأي صيغة مباشرة، واستبدلها بما يلي حسب سياق كل فقرة:
   - عند الإشارة لعملية أو حدث ميداني (قصف، استهداف، اشتباك): استخدم "قوات صنعاء" (مثال: "قصفت قوات صنعاء"، "استهدفت قوات صنعاء").
   - عند الإشارة العامة لهم كطرف أو جهة (بيان، تصريح، موقف، خلفية سياسية): استخدم "صنعاء" فقط (مثال: "صرّح متحدث صنعاء"، "لم يصدر عن صنعاء تعليق").
   طبّق المصطلح المناسب بثبات بكل موضع بالنص (العنوان والمتن)، مع الحفاظ الكامل على بقية وقائع الخبر وتفاصيله وأهميته دون أي نقصان."""
    else:
        rule_2 = """2) إذا كان الخبر مهماً وله وقائع وتفاصيل فعلية (قرار، حدث ميداني، بيان، تعيين، إلخ) لكنه يتضمن فقط إشارة أو ذكراً ضمن سياقه للحوثي/أنصار الله/إيران دون أن يكون محور الخبر هجوماً عليهم — اجعل "houthi_iran_exclude" = false، وأعد صياغة العنوان والمتن معاً بحيث لا يُذكر الحوثي ولا أنصار الله ولا إيران بأي اسم أو صفة أو كنية إطلاقاً (احذف الإشارة نهائياً من النص بدل تبريدها فقط)، مع الحفاظ الكامل على بقية وقائع الخبر وتفاصيله وأهميته دون أي نقصان."""

    houthi_filter_instruction = f"""

قاعدة إلزامية صارمة وأخيرة بشأن الحوثي/أنصار الله/إيران (تُطبَّق بعد كل التعليمات السابقة، ولا تُلغي أياً منها، لكنها تحكم القرار النهائي):
1) إذا كان محور الخبر بأكمله هجوماً أو تصريحاً مضاداً أو انتقاداً موجّهاً بشكل أساسي ضد الحوثي/أنصار الله/سلطات صنعاء أو إيران — بحيث يكون "الهجوم عليهم" هو جوهر الخبر نفسه وليس حدثاً مستقلاً يتضمن إشارة عابرة لهم، بأي صياغة أو نبرة كانت ولو بعد تبريدها — اجعل الحقل "houthi_iran_exclude" = true. بهذه الحالة اكتب أي نص مختصر بحقول title/excerpt/content (لن تُستخدم إطلاقاً، الخبر لن يُنشر).
{rule_2}
3) أي خبر لا علاقة له بالحوثي أو أنصار الله أو إيران أصلاً — اجعل "houthi_iran_exclude" = false وأعد صياغته بشكل طبيعي كالمعتاد دون أي تدخل بهذا الخصوص.

أعد كائن JSON: {{title, excerpt, content, houthi_iran_exclude}}"""

    json_instruction = houthi_filter_instruction

    # --- المسار الأول: إذا كان الخبر ينتمي للأقسام المحايدة أو الخدمية ---
    if any(nc in cat for nc in neutral_categories):
        prompt = f"""
أنت محرر صحفي محترف ومتخصص. مطلوب منك إعادة صياغة الخبر التالي ليتناسب تماماً مع قسم ({cat}) بأسلوب احترافي، دقيق، وبعيد تماماً عن أي استقطاب سياسي.

القواعد التحريرية لهذا القسم:
- الأسلوب: مهني، واضح، ومباشر بحسب طبيعة القسم (رياضي ممتع، دولي رصين، خدمي دقيق بالأرقام لأسعار العملات والذهب).
- التركيز: على المعلومة والفائدة المباشرة للقارئ دون أي إسقاطات سياسية أو مصطلحات موجهة.
- الصياغة: من الصفر، بجمل متفاوة الطول، وإخفاء أي بصمة للذكاء الاصطناعي (تجنب التكرار والنمطية).
- البداية والنهاية: أول جملة في المتن هي الملخص التلقائي القوي المكثف وتنتهي بنقطة، والختام يربط الخبر بسياقه الطبيعي.

عنوان الخبر الفيد: {title}
نص الخبر الخام:
{raw_body}
"""
        return prompt + json_instruction

    # --- المسار الثاني: إذا كان الخبر سياسياً/محلياً ولكنه يحتوي على إشارات لصنعاء أو الحوثيين (حماية أمنية) ---
    elif any(kw in raw_body for kw in sanaa_keywords):
        prompt = f"""
أنت محرر صحفي محترف ومحايد تعمل في وكالة أنباء دولية رصينة. يُطلب منك إعادة صياغة الخبر التالي (الذي يخص صنعاء أو جماعة أنصار الله) بأسلوب "دبلوماسي بارد" وجاف تماماً، يركز على الوقائع ويتحاشى أي لغة هجومية أو تحريضية قد تسبب مشاكل أمنية وقانونية للموقع.

محددات وقواعد الأمان التحريري لـ (أخبار صنعاء):
1. تجريد الهجوم: إذا كان النص الخام قادماً من مصدر مهاجم (إخواني، حكومي، سعودي) ويحتوي على عبارات تخوين أو شتائم أو أوصاف مثل "نهب، اختطاف، مليشيا إرهابية"، قم بتبريدها فوراً وحولها إلى نقل رسمي محايد (مثل: "اتهمت تقارير"، "أفادت مصادر محلية بفرض رسوم"، "تحفظت الأجهزة الأمنية").
2. تفكيك البروباغندا والمديح العسكري (لعدم الظهور كبوق ترويجي): إذا كان النص الخام قادماً من مصدر يمتدح سلطات صنعاء ويحتوي على عبارات إطراء، تمجيد، مصطلحات حماسية وعاطفية، أو خطابات وبيانات عسكرية رسمية (مثل: القوة الصاروخية المباركة، ضربات مسددة، القيادة الحكيمة، مجاهدين، دك حصون)، قم بتجريدها تماماً وحولها إلى لغة عسكرية تقريرية جافة تنقل الحدث بأسلوب المشاهد والتوثيق المحايد، وانسب التصريحات دائماً لمتحدثها الرسمي (مثل: "أعلنت قوات صنعاء في بيان عسكري"، "أوضح المتحدث العسكري"، "أشار البيان الصادر عن").
3. المصطلحات المعتمدة: استخدم عبارات واقعية وموصفة مثل "سلطات صنعاء"، "جماعة أنصار الله (الحوثيين)"، "الأجهزة الأمنية في صنعاء"، وتجنب الألقاب التمجيدية أو الهجومية الفجة. ويُذكر المسؤولون والقيادات بأسمائهم وصفاتهم الرسمية الجافة بلا هجوم أو تمجيد.
4. التغطية الخدمية والإدارية: انقل القرارات، التعميمات، التعيينات، أو الأحداث كما هي كوقائع مجردة، دون إبداء موقف سياسي أو صياغة عاطفية.
5. لغة وكالات الأنباء: اجعل النص رزيناً وقانونياً كأنه صادر عن رويترز, بحيث لا يمسك أي طرف في صنعاء أي ممسك قانوني ضد الموقع.
6. شرط التحرير الأساسي: أول جملة في المتن هي الملخص التلقائي القوي للحدث وتنتهي بنقطة، مع تفاوت أطوال الجمل وإخفاء بصمة الآلة.

عنوان الخبر الفيد: {title}
نص الخبر الخام:
{raw_body}
"""
        return prompt + json_instruction

    # --- المسار الثالث: البرومبت الأساسي والسيادي لموقع "جنوب فويس" للأخبار المحلية والسياسية الأخرى ---
    else:
        prompt = f"""
أنت محرر صحفي في موقع إخباري جنوبي مستقل (جنوب فويس). تصلك نصوص أخبار خام فتعيد كتابتها من الصفر كما يفعل محرر بشري محترف يمتلك كامل المهارات الصحفية.

الخطوة الصفر — تصفية المصدر:
قبل أي كتابة، اقرأ الخبر الخام وحدّد بصمة مصدره (حوثي، إخواني، إماراتي، سعودي، حكومي، انتقالي، أو غيره) ثم جرّد الخبر تماماً من زاويته ولغته وتوظيفه، واحتفظ بالوقائع فقط: من، ماذا، أين، متى، وما التداعي على المواطن. لا تنقل أجندة المصدر ولو بدت منسجمة مع الهوية التحريرية للموقع.

الهوية التحريرية لـ (جنوب فويس):
- صوت المواطن الجنوبي المستقل، لا ناطق بأجندة أي طرف.
- تفضح الفساد والتبعية للتحالف السعودي عبر الوقائع لا التصريحات المباشرة لتعكس معاناة المواطن الجنوبي عند الحاجة التحريرية فقط.

المصطلحات الإلزامية:
"الحكومة المدعومة من التحالف" لا "الشرعية"
"التحالف بقيادة السعودية" لا "التحالف العربي"
"قوات مجلس القيادة" أو "القوات المدعومة سعودياً" لا "قوات الشرعية"
"تسيطر على" لا "تحرر"
"القوات الإخوانية" أو "قوات تنظيم الإخوان" لا "قوات الإصلاح"
المجلس الانتقالي يُذكر باسمه فقط بلا ألقاب تمجيدية
"سلطة مجلس القيادة الرئاسي" للسلطة المحلية في عدن المدعومة سعودياً
"الحاكم العسكري السعودي الشهراني" عند الإشارة إليه

الحكم التحريري — لا قالب ثابت:
كل خبر له شكله الذي يخدم مضمونه:
الخبر العاجل القصير: مباشر ومكثف بلا حشو
الخبر الميداني: يتصاعد من الواقعة إلى التداعية
التقرير التحليلي: طبقات متراكمة، وقائع ثم سياق ثم دلالة
الخبر الاقتصادي: أرقام مربوطة بوجع إنساني حقيقي
الخبر السياسي: التصريح في مواجهة الواقع المعاش
الخبر الأمني والعسكري: الضحايا والتداعيات أولاً

إخفاء بصمة الآلة وضبط تنوع الخواتيم (شرط صارم لمنع التكرار):
- يُمنع منعاً باتاً ختم كل الأخبار بنغمة مكررة عن "معاناة المواطن الجنوبي والظروف المعيشية".
- التزم بالتنويع التحريري الذكي بحسب نوع الخبر:
  * الخبر السياسي أو العسكري البحتي: يختم بعبارة تربط المشهد بأبعاده السياسية أو الميدانية القادمة وسياق الحدث الطبيعي دون إقحام المعيشة.
  * الخبر الخدمي، الاقتصادي، أو الميداني المحلي: هنا فقط، اربط الخاتمة بوجع الناس وتأثير هذه القرارات على حياتهم اليومية وبشكل مقتضب وغير مكرر.
- تفاوت حقيقي في طول الجمل وإيقاعها، ولا تكرار في مفتتحات الفقرات أو الأساليب التحريرية.

قواعد الكتابة الثابتة:
أعد الكتابة من الصفر بلغتك الخاصة تماماً، لا تترجم ولا تعيد ترتيب
أول جملة في content هي الملخص التلقائي — قوية ومكثفة وتنتهي بنقطة
ابدأ بالأهم: المواطن، التداعيات، الوقائع
اختم بما يربط المشهد بحقيقته الأعمق دون خطابية مبتذلة أو تكرار استعطافي

تصنيف الخبر الحالي: {cat}
عنوان الخبر الفيد: {title}
نص الخبر الخام:
{raw_body}
"""
        return prompt + json_instruction


def _parse_429(resp) -> tuple[bool, Optional[float]]:
    is_daily, retry_delay = False, None
    try:
        for detail in resp.json().get("error", {}).get("details", []):
            dtype = detail.get("@type", "")
            if "QuotaFailure" in dtype:
                for v in detail.get("violations", []):
                    qid = (v.get("quotaId", "") + v.get("quotaMetric", "")).lower()
                    if any(x in qid for x in ("perday", "per_day", "/day")):
                        is_daily = True
            if "RetryInfo" in dtype:
                m = re.match(r"(\d+(?:\.\d+)?)s", str(detail.get("retryDelay", "")))
                if m:
                    retry_delay = float(m.group(1))
    except Exception:
        pass
    if not is_daily:
        low = resp.text.lower()
        if any(x in low for x in ("perday", "per day", "/day")):
            is_daily = True
    return is_daily, retry_delay


def call_gemini(prompt_text: str, schema: dict = None) -> str:
    gen_config = {
        "temperature": 0.5,
        "maxOutputTokens": 32768,
        "responseMimeType": "application/json",
        "responseSchema": schema or RESPONSE_SCHEMA,
    }
    body = {"contents": [{"parts": [{"text": prompt_text}]}], "generationConfig": gen_config}
    headers = {"Content-Type": "application/json", "x-goog-api-key": current_key()}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            RATE_LIMITER.wait()
            resp = requests.post(model_url(), headers=headers, json=body, timeout=120)
            resp.raise_for_status()
            r = resp.json()
            if not r.get("candidates"):
                log.warning(f"  ⚠️  استجابة بدون candidates: {str(r)[:300]}")
                time.sleep(5)
                continue
            parts = r["candidates"][0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts)

        except requests.exceptions.Timeout:
            log.warning("  ⚠️  انتهت المهلة — إعادة بعد 15ث...")
            time.sleep(15)

        except requests.exceptions.HTTPError:
            code = resp.status_code
            if code == 429:
                is_daily, retry_delay = _parse_429(resp)
                if is_daily:
                    raise DailyQuotaExceeded()
                wait = min(int(retry_delay or 0) + 1 if retry_delay else 10 * attempt, MAX_BACKOFF)
                log.warning(f"  ⏳ 429 — انتظار {wait}ث (محاولة {attempt})...")
                time.sleep(wait)
            elif code in (500, 503):
                log.warning(f"  ⚠️  خطأ خادم ({code}) — إعادة بعد 20ث...")
                time.sleep(20)
            elif code == 404:
                log.error(f"  ❌ النموذج {current_model()} غير متاح لهذا المفتاح (404)")
                raise ModelUnavailable()
            elif code in (401, 403):
                log.error(f"  ❌ المفتاح مرفوض/محظور لهذا النموذج ({code}): {resp.text[:200]}")
                raise KeyForbidden()
            else:
                log.error(f"  ❌ خطأ HTTP {code}: {resp.text[:200]}")
                raise
        except DailyQuotaExceeded:
            raise
        except ModelUnavailable:
            raise
        except KeyForbidden:
            raise
        except Exception as e:
            log.error(f"  ❌ خطأ: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(10)
            else:
                raise

    raise RuntimeError("فشل الاتصال بعد كل المحاولات")


def call_with_rotation(prompt_text: str, schema: dict = None) -> str:
    global _current_key_idx, _model_stage_idx
    while True:
        try:
            return call_gemini(prompt_text, schema)
        except (DailyQuotaExceeded, ModelUnavailable, KeyForbidden) as e:
            if isinstance(e, ModelUnavailable):
                log.warning(
                    f"  ⚠️  النموذج غير متاح لهذا المفتاح (404): {current_model()} "
                    f"[مفتاح {_current_key_idx + 1}/{len(GEMINI_API_KEYS)}]"
                )
            elif isinstance(e, KeyForbidden):
                log.warning(
                    f"  🚫 المفتاح مرفوض/محظور لهذا النموذج (401/403): {current_model()} "
                    f"[مفتاح {_current_key_idx + 1}/{len(GEMINI_API_KEYS)}]"
                )
            else:
                log.warning(
                    f"  🛑 انتهت الحصة اليومية ({current_model()}) "
                    f"[مفتاح {_current_key_idx + 1}/{len(GEMINI_API_KEYS)}]"
                )

            if _current_key_idx + 1 < len(GEMINI_API_KEYS):
                # لسه فيه مفاتيح تانية لنفس المرحلة (نفس النموذج الحالي) — ننتقل للمفتاح التالي
                _current_key_idx += 1
                log.info(f"  🔑 مفتاح جديد [{_current_key_idx + 1}/{len(GEMINI_API_KEYS)}] | {current_model()}")
                continue

            if _model_stage_idx + 1 < len(MODEL_CASCADE):
                # استُنفدت حصة النموذج الحالي على كل المفاتيح — ننتقل للنموذج
                # التالي بالقائمة (MODEL_CASCADE)، بدءاً من المفتاح الأول من جديد.
                _model_stage_idx += 1
                _current_key_idx = 0
                log.warning(
                    f"  🔄 استُنفدت حصة {MODEL_CASCADE[_model_stage_idx - 1]} على كل المفاتيح — "
                    f"التبديل إلى {current_model()} بدءاً من المفتاح الأول "
                    f"({_model_stage_idx + 1}/{len(MODEL_CASCADE)})."
                )
                continue

            # استُنفدت كل المفاتيح حتى بآخر نموذج بالقائمة أيضاً — لا مزيد من
            # الخيارات لهذا التشغيل، تُرفع الاستثناء للمتصل.
            log.error(f"  ❌ استُنفدت حصة {current_model()} أيضاً على كل المفاتيح — لا مزيد من الخيارات.")
            raise


def rewrite_article(title: str, body: str, category: str, source_feed: Optional[str] = None) -> Optional[dict]:
    prompt = build_prompt(title, body, category, source_feed=source_feed)
    raw = call_with_rotation(prompt)
    try:
        import json
        data = json.loads(raw)
        if not all(k in data for k in ("title", "excerpt", "content")):
            return None
        if data.get("houthi_iran_exclude") is True:
            log.info(f"  🚫 [فلتر الحوثي/إيران] خبر هجومي خالص — استُبعد من النشر: {title[:60]}")
            return None
        return data
    except Exception as e:
        log.warning(f"  ⚠️  فشل تحليل رد Gemini: {e}")
        return None


def rewrite_title_only(title: str, body: str) -> Optional[str]:
    """يعيد صياغة العنوان فقط (لمقالات الرأي المستثناة من الصياغة الكاملة)
    — النص الأصلي للمقال لا يُمرَّر للتعديل، فقط للسياق."""
    prompt = build_title_only_prompt(title, body)
    try:
        raw = call_with_rotation(prompt, schema=TITLE_ONLY_SCHEMA)
        import json
        data = json.loads(raw)
        new_title = (data.get("title") or "").strip()
        return new_title or None
    except Exception as e:
        log.warning(f"  ⚠️  فشلت إعادة صياغة العنوان فقط: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════
#  🧩  أدوات مساعدة
# ══════════════════════════════════════════════════════════════════════

def format_content_paragraphs(text: str) -> str:
    """يفصل كل جملة إلى فقرة HTML <p> مستقلة — الموقع يعرض content
    كـ HTML مباشرة (dangerouslySetInnerHTML) ولا يحوّل \\n إلى سطر جديد."""
    text = re.sub(r"\s+", " ", text.strip())
    parts = re.split(r"(?<=[.!؟])\s+", text)
    parts = [p.strip() for p in parts if p.strip()]
    return "".join(f"<p>{p}</p>" for p in parts)


def make_slug(title: str) -> str:
    base = re.sub(r"[^\w\s\u0600-\u06FF-]", "", title).strip()
    base = re.sub(r"\s+", "-", base)[:80]
    suffix = uuid.uuid4().hex[:6]
    return f"{base}-{suffix}"


def word_stats(text: str) -> tuple[int, int]:
    words = len(text.split())
    reading_time = max(1, round(words / 200))
    return words, reading_time


# قائمة كلمات وظيفية عربية شائعة تُستبعد من الكلمات المفتاحية التلقائية
# (أدوات ربط/جر/إشارة لا تحمل معنى دلالياً يفيد الفهرسة أو البحث)
_KEYWORD_STOPWORDS = {
    "من", "إلى", "على", "في", "عن", "مع", "أن", "إن", "كان", "كانت",
    "هذا", "هذه", "ذلك", "تلك", "الذي", "التي", "الذين", "كل", "بعد",
    "قبل", "بين", "عند", "حيث", "كما", "لكن", "غير", "دون", "خلال",
    "أو", "و", "لا", "ما", "لم", "لن", "قد", "هو", "هي", "هم", "نحن",
    "أنا", "أنت", "التقرير", "وأضاف", "وأشار", "وأكد", "وقال", "قال",
    "أعلن", "أعلنت", "وذكر", "ذكرت", "اليوم", "أمس", "غدا",
}


def extract_keywords(title: str, content: str, max_keywords: int = 8) -> list[str]:
    """يستخرج كلمات مفتاحية تلقائية من العنوان والمحتوى (بدون استدعاء AI):
    يعزل الكلمات العربية/الإنجليزية، يستبعد الكلمات الوظيفية والقصيرة
    والأرقام المفردة، ثم يرتّب حسب الأولوية (كلمات العنوان أولاً) مع
    إزالة التكرار. يُستخدم لتعبئة عمود keywords الجديد بجدول posts."""
    def clean_words(text: str) -> list[str]:
        text = re.sub(r"<[^>]+>", " ", text)  # إزالة أي وسوم HTML متبقية
        raw = re.findall(r"[\u0600-\u06FFA-Za-z]{3,}", text)
        return [w for w in raw if w not in _KEYWORD_STOPWORDS]

    ordered_unique: list[str] = []
    seen: set[str] = set()
    for w in clean_words(title) + clean_words(content):
        if w not in seen:
            seen.add(w)
            ordered_unique.append(w)
        if len(ordered_unique) >= max_keywords:
            break
    return ordered_unique


# ⚠️ الدالتان التاليتان نسخة مطابقة تماماً (سطراً بسطر) لمنطق
# generateMetaTitle/meta_description بملف src/lib/seoHelpers.ts بالموقع —
# أي تعديل مستقبلي بأحد الطرفين لازم يُطبَّق على الآخر فوراً وإلا
# يصير التوليد اليدوي (Admin Panel) مختلفاً عن التوليد الآلي (البوت).

META_TITLE_BRAND_SUFFIX = " | شمسان نيوز"
META_TITLE_MAX_LEN = 70
META_DESCRIPTION_MAX_LEN = 160


def generate_meta_title(title: str) -> str:
    """يطابق generateMetaTitle() بـ seoHelpers.ts: يضيف اسم الموقع كلاحقة،
    وإن تجاوز الطول الأقصى (70 حرفاً شاملاً اللاحقة) يقصّه عند آخر مسافة."""
    max_len = META_TITLE_MAX_LEN - len(META_TITLE_BRAND_SUFFIX)
    trimmed = title.strip()
    if len(trimmed) <= max_len:
        return trimmed + META_TITLE_BRAND_SUFFIX
    trimmed = trimmed[:max_len]
    last_space = trimmed.rfind(" ")
    if last_space > 0:
        trimmed = trimmed[:last_space]
    return trimmed + META_TITLE_BRAND_SUFFIX


def generate_meta_description(excerpt: str) -> str:
    """يطابق منطق meta_description المستخدم بـ AdminPanel.tsx/JsonNewsImporter.tsx:
    نفس نص excerpt مقصوصاً عند 160 حرفاً (بدون قطع كلمة بالمنتصف قدر الإمكان)."""
    trimmed = (excerpt or "").strip()
    if len(trimmed) <= META_DESCRIPTION_MAX_LEN:
        return trimmed
    trimmed = trimmed[:META_DESCRIPTION_MAX_LEN]
    last_space = trimmed.rfind(" ")
    if last_space > 0:
        trimmed = trimmed[:last_space]
    return trimmed.rstrip() + "…"


# ══════════════════════════════════════════════════════════════════════
#  🚀  نقطة الدخول
# ══════════════════════════════════════════════════════════════════════

def choose_category_name() -> str:
    print("\nاختر القسم اللي تبي تنشر فيه:")
    for key, name in CATEGORY_OPTIONS.items():
        print(f"  {key}) {name}")
    while True:
        choice = input("اكتب الرقم: ").strip()
        if choice in CATEGORY_OPTIONS:
            return CATEGORY_OPTIONS[choice]
        print("⚠️  رقم غير صحيح، جرّب مرة ثانية.")


def choose_extraction_mode() -> str:
    """يسأل المستخدم بأول تشغيل للسكربت عن طريقة جلب نص الخبر:
    1 = استخراج الخبر كاملاً (فتح كل صفحة فعلياً عبر extract_article + Jina
        عند الحاجة) — المنطق الجديد.
    2 = استخدام ملفات XML المحلية فقط، بنفس المنطق الحالي تماماً (بدون فتح
        أي صفحة إضافية — راحة/متن RSS كما هو).
    3 = استخدام RSS المساء فقط، بنفس المنطق الحالي تماماً.
    يرجّع "1" أو "2" أو "3"."""
    print("\nكيف تريد جلب نص الأخبار؟")
    print("  1) استخراج الخبر كاملاً (يفتح كل صفحة ويسحب المتن الكامل) — المنطق الجديد")
    print("  2) استخدام XML المحلي — نفس المنطق الحالي")
    print("  3) استخدام RSS المساء — نفس المنطق الحالي")
    while True:
        choice = input("اكتب الرقم: ").strip()
        if choice in ("1", "2", "3"):
            return choice
        print("⚠️  رقم غير صحيح، جرّب مرة ثانية.")


def choose_auto_mode() -> bool:
    """يسأل المستخدم هل يفعّل النشر التلقائي (كل خبر بقسمه الخاص حسب ملفه
    المصدر في RSS_FEED_CATEGORIES) أو الوضع اليدوي (قسم واحد موحّد لكل الدفعة)."""
    while True:
        choice = input("\nتفعيل التلقائي؟ (كل خبر يُنشر بقسمه حسب ملفه) [Y/N]: ").strip().lower()
        if choice in ("y", "yes", "ن", "نعم"):
            return True
        if choice in ("n", "no", "لا"):
            return False
        print("⚠️  اكتب Y أو N.")


def choose_feed_sources() -> dict:
    """يسأل المستخدم أي مصادر يشتغل عليها:
    y/نعم = الاثنين (ملفات XML المحلية + RSS المساء)،
    1 = ملفات XML المحلية فقط، 2 = RSS المساء فقط."""
    masa_feed = {RSS_MASA_URL: RSS_MASA_CATEGORY}
    while True:
        choice = input(
            "\nهل تريد العمل على ملفات XML المحلية و RSS المساء؟ "
            "(اكتب: y = الاثنين، 1 = XML المحلية فقط، 2 = RSS المساء فقط): "
        ).strip().lower()
        if choice in ("y", "yes", "نعم"):
            return {**RSS_FEED_CATEGORIES, **masa_feed}
        if choice == "1":
            return dict(RSS_FEED_CATEGORIES)
        if choice == "2":
            return masa_feed
        print("⚠️  إجابة غير صحيحة، جرّب مرة ثانية.")


def parse_interval_minutes(raw: str) -> Optional[int]:
    """يحوّل مدخل المستخدم لفارق زمني بالدقائق. حرف 'د' اختياري وغير مؤثر —
    الرقم يمثّل دقائق دائماً (مثلاً 30، 60، 120، أو 15د، 10د)."""
    raw = raw.strip()
    if raw.endswith("د"):
        raw = raw[:-1].strip()
    if not raw.isdigit():
        return None
    value = int(raw)
    return value if value > 0 else None


def choose_category_overrides(new_items: list, auto_mode: bool, category_name: Optional[str]) -> dict:
    """يسمح بتخصيص قسم مختلف لأخبار معينة من الدفعة (باستخدام رقمها بالقائمة
    أعلاه)، بدون التأثير على قسم بقية الأخبار (التلقائي حسب الملف المصدر،
    أو اليدوي الموحّد). يرجّع dict: {index (1-based): اسم القسم}."""
    overrides: dict[int, str] = {}
    print("\nهل تريد تخصيص قسم مختلف لأي خبر من القائمة أعلاه؟")
    print("  اكتب: رقم الخبر ثم رقم القسم مفصولين بشرطة، مثال: 3-2")
    print("  (رقم القسم حسب القائمة: " +
          "، ".join(f"{k}={v}" for k, v in CATEGORY_OPTIONS.items()) + ")")
    print("  اترك السطر فارغاً (Enter) واضغط إدخال عندما تنتهي.")
    while True:
        raw = input("خبر-قسم (أو Enter للمتابعة): ").strip()
        if not raw:
            break
        m = re.match(r"^(\d+)\s*[-:]\s*(\d+)$", raw)
        if not m:
            print("⚠️  صيغة غير صحيحة، استخدم الشكل: رقم_الخبر-رقم_القسم (مثال: 3-2)")
            continue
        news_idx, cat_key = int(m.group(1)), m.group(2)
        if news_idx < 1 or news_idx > len(new_items):
            print(f"⚠️  رقم الخبر {news_idx} غير موجود بالقائمة (المتاح 1-{len(new_items)}).")
            continue
        if cat_key not in CATEGORY_OPTIONS:
            print("⚠️  رقم القسم غير صحيح.")
            continue
        overrides[news_idx] = CATEGORY_OPTIONS[cat_key]
        it = new_items[news_idx - 1]
        print(f"  ✅ الخبر {news_idx} ({it['title'][:50]}) سيُنشر بقسم: {overrides[news_idx]}")
    return overrides


def choose_excluded_items(new_items: list) -> set[int]:
    """يسمح بمنع أخبار معينة من الدفعة من النشر تماماً (باستخدام رقمها
    بالقائمة أعلاه)، بدون التأثير على بقية الأخبار. يرجّع set بأرقام
    الأخبار الممنوعة (1-based)."""
    excluded: set[int] = set()
    print("\nهل تريد منع أي خبر من القائمة أعلاه من النشر نهائياً؟")
    print("  اكتب رقم الخبر واضغط إدخال — يُمنع هذا الخبر تماماً من النشر.")
    print("  تقدر تكرر لأكثر من خبر، واحد بكل سطر.")
    print("  اترك السطر فارغاً (Enter) واضغط إدخال عندما تنتهي.")
    while True:
        raw = input("رقم الخبر الممنوع (أو Enter للمتابعة): ").strip()
        if not raw:
            break
        if not raw.isdigit():
            print("⚠️  اكتب رقم الخبر فقط.")
            continue
        news_idx = int(raw)
        if news_idx < 1 or news_idx > len(new_items):
            print(f"⚠️  رقم الخبر {news_idx} غير موجود بالقائمة (المتاح 1-{len(new_items)}).")
            continue
        excluded.add(news_idx)
        it = new_items[news_idx - 1]
        save_blocked_link(it["link"])  # حظر دائم — يُستبعد تلقائياً بكل تشغيل قادم
        print(f"  🚫 الخبر {news_idx} ({it['title'][:50]}) لن يُنشر (ومحظور دائماً، لن يظهر بتشغيل قادم).")
    return excluded


def choose_skipped_items(new_items: list) -> set[int]:
    """يسمح بتخطي أخبار معينة من هذه الدفعة فقط (بدون حظر دائم) — بنفس شكل
    choose_excluded_items تماماً، لكن بدون استدعاء save_blocked_link. الخبر
    المتخطى لن يُنشر بهذه الجلسة، لكنه يبقى مؤهلاً للظهور طبيعياً بجلسة
    قادمة (طالما ما نُشر فعلاً ولا اتحظر عبر خيار المنع الدائم أعلاه).
    يرجّع set بأرقام الأخبار المتخطاة (1-based)."""
    skipped_idx: set[int] = set()
    print("\nهل تريد تخطي أي خبر من القائمة أعلاه بهذه الجلسة فقط (بدون منع دائم)؟")
    print("  اكتب رقم الخبر واضغط إدخال — يُتخطى هذا الخبر الآن فقط،")
    print("  وسيظهر طبيعياً بجلسة قادمة (لا يُحظر نهائياً).")
    print("  تقدر تكرر لأكثر من خبر، واحد بكل سطر.")
    print("  اترك السطر فارغاً (Enter) واضغط إدخال عندما تنتهي.")
    while True:
        raw = input("رقم الخبر المتخطى (أو Enter للمتابعة): ").strip()
        if not raw:
            break
        if not raw.isdigit():
            print("⚠️  اكتب رقم الخبر فقط.")
            continue
        news_idx = int(raw)
        if news_idx < 1 or news_idx > len(new_items):
            print(f"⚠️  رقم الخبر {news_idx} غير موجود بالقائمة (المتاح 1-{len(new_items)}).")
            continue
        if news_idx in skipped_idx:
            print(f"⚠️  الخبر {news_idx} متخطى بالفعل.")
            continue
        skipped_idx.add(news_idx)
        it = new_items[news_idx - 1]
        print(f"  ⏭️  الخبر {news_idx} ({it['title'][:50]}) لن يُنشر بهذه الجلسة فقط (سيظهر طبيعياً بجلسة قادمة).")
    return skipped_idx


def choose_watermark_items(new_items: list) -> set[int]:
    """يسمح باختيار أخبار معينة من الدفعة توضع علامة شمسان نيوز المائية على
    صورتها الرئيسية عند النشر — بنفس شكل choose_excluded_items/choose_skipped_items
    تماماً (رقم الخبر بكل سطر، Enter فارغ للإنهاء). لا تأثير على بقية الأخبار
    (تُنشر بصورتها العادية بدون علامة، كما هو الوضع الحالي).
    يرجّع set بأرقام الأخبار المختارة (1-based)."""
    watermark_idx: set[int] = set()
    print("\nهل تريد وضع العلامة المائية (شعار شمسان نيوز) على صورة أي خبر من القائمة أعلاه؟")
    print("  اكتب رقم الخبر واضغط إدخال — تُلصق العلامة المائية على صورته الرئيسية عند النشر.")
    print("  تقدر تكرر لأكثر من خبر، واحد بكل سطر.")
    print("  اترك السطر فارغاً (Enter) واضغط إدخال عندما تنتهي (بدون علامة مائية للباقي).")
    while True:
        raw = input("رقم الخبر (علامة مائية) (أو Enter للمتابعة): ").strip()
        if not raw:
            break
        if not raw.isdigit():
            print("⚠️  اكتب رقم الخبر فقط.")
            continue
        news_idx = int(raw)
        if news_idx < 1 or news_idx > len(new_items):
            print(f"⚠️  رقم الخبر {news_idx} غير موجود بالقائمة (المتاح 1-{len(new_items)}).")
            continue
        if news_idx in watermark_idx:
            print(f"⚠️  الخبر {news_idx} مُختار بالفعل للعلامة المائية.")
            continue
        watermark_idx.add(news_idx)
        it = new_items[news_idx - 1]
        print(f"  🖋️  الخبر {news_idx} ({it['title'][:50]}) ستوضع على صورته العلامة المائية عند النشر.")
    return watermark_idx


def choose_scheduled_items(new_items: list) -> set[int]:
    """يسمح باختيار أخبار معينة من الدفعة لتُجدول (status=scheduled) بدل
    النشر المباشر — بنفس شكل choose_excluded_items/choose_skipped_items تماماً
    (رقم الخبر بكل سطر، Enter فارغ للإنهاء). أي خبر لا يُختار هنا يُنشر
    مباشرة فوراً (published) بتاريخ نشره الأصلي من المصدر، كالوضع الحالي.
    يرجّع set بأرقام الأخبار المختارة للجدولة (1-based)."""
    scheduled_idx: set[int] = set()
    print("\nهل تريد جدولة أي خبر من القائمة أعلاه بدل نشره مباشرة؟")
    print("  اكتب رقم الخبر واضغط إدخال — يُجدول هذا الخبر (status=scheduled).")
    print("  تقدر تكرر لأكثر من خبر، واحد بكل سطر.")
    print("  اترك السطر فارغاً (Enter) واضغط إدخال عندما تنتهي (الباقي يُنشر مباشرة فوراً).")
    while True:
        raw = input("رقم الخبر الذي تريد جدولته (أو Enter للمتابعة): ").strip()
        if not raw:
            break
        if not raw.isdigit():
            print("⚠️  اكتب رقم الخبر فقط.")
            continue
        news_idx = int(raw)
        if news_idx < 1 or news_idx > len(new_items):
            print(f"⚠️  رقم الخبر {news_idx} غير موجود بالقائمة (المتاح 1-{len(new_items)}).")
            continue
        if news_idx in scheduled_idx:
            print(f"⚠️  الخبر {news_idx} مُختار للجدولة بالفعل.")
            continue
        scheduled_idx.add(news_idx)
        it = new_items[news_idx - 1]
        print(f"  📅 الخبر {news_idx} ({it['title'][:50]}) سيُجدول بدل النشر المباشر.")
    return scheduled_idx


def choose_gemini_mode() -> str:
    """يسأل المستخدم مرة وحدة عند التشغيل عن طريقة التعامل مع Gemini لكل
    الأخبار من كل الفيدات وكل الأقسام — ما عدا مقالات الرأي (قسم "آراء
    واتجاهات") اللي تبقى دائماً بمنطقها الحالي الثابت بغض النظر عن هذا
    الاختيار (عنوانها يُعاد صياغته عبر Gemini، ومتنها ينشر كما هو منسوباً
    لكاتبها).
    1 = مع Gemini للعنوان والمتن معاً (الافتراضي — إعادة صياغة كاملة)
    2 = مع Gemini للعنوان فقط، والمتن يُنشر كما استُخرج حرفياً بدون تعديل
    3 = بدون Gemini إطلاقاً — العنوان والمتن معاً كما استُخرجا حرفياً
    يرجّع "1" أو "2" أو "3"."""
    print("\nكيف تريد التعامل مع Gemini؟ (يشمل كل الفيدات وكل الأقسام ما عدا مقالات الرأي)")
    print("  1) مع Gemini عنوان+متن (الافتراضي)")
    print("  2) مع Gemini عنوان فقط دون المتن")
    print("  3) بدون Gemini عنوان+متن (كما استُخرج حرفياً)")
    while True:
        choice = input("اكتب الرقم: ").strip()
        if choice in ("1", "2", "3"):
            return choice
        print("⚠️  رقم غير صحيح، جرّب مرة ثانية.")


def choose_publish_mode() -> Optional[int]:
    """يرجّع None للنشر المباشر، أو عدد الدقائق بين كل خبر للجدولة."""
    print("\nطريقة النشر:")
    print("  1) نشر مباشر فوري (published)")
    print("  2) جدولة الأخبار بفارق زمني بينها (scheduled)")
    while True:
        choice = input("اكتب الرقم: ").strip()
        if choice == "1":
            return None
        if choice == "2":
            while True:
                raw = input("اكتب الفارق الزمني بالدقائق بين كل خبر والثاني (مثلاً 30 أو 60 أو 15د): ")
                minutes = parse_interval_minutes(raw)
                if minutes:
                    return minutes
                print("⚠️  قيمة غير صحيحة، جرّب مرة ثانية (رقم صحيح أكبر من صفر).")
        print("⚠️  رقم غير صحيح، جرّب مرة ثانية.")


def main():
    if not RSS_FEED_CATEGORIES:
        log.error("❌ لم تُضف أي ملفات RSS بعد. عبّئ قاموس RSS_FEED_CATEGORIES بالأعلى.")
        sys.exit(1)

    log.info("═" * 60)
    log.info("  📰  شمسان نيوز — سحب وإعادة صياغة الأخبار")
    log.info("═" * 60)

    # 🧹 ملاحظة: تم إزالة استدعاء cleanup_system_logs() من هنا (كان يفشل دوماً
    # بخطأ statement timeout عبر REST/PostgREST). التنظيف يعمل بشكل مستقل
    # وناجح عبر جدولة pg_cron('cleanup-system-logs-6h') التي تُنفَّذ داخل
    # قاعدة البيانات مباشرة بدون المرور بقيود مهلة الـ API.

    # 🔔 يتحقق من حجم جدولي cron/net، ويرسل تنبيهاً لمحادثة الإدارة الخاصة
    # لو تجاوزا الحد الطبيعي (مؤشر على تعطّل جدولة pg_cron الدورية).
    check_system_logs_size()

    # ✅ يُنفَّذ بأول كل تشغيلة: يتحقق من أي أخبار مجدولة بجلسات سابقة
    # صار الآن نشرها فعلياً (عبر الـ Cron)، ويرسل روابطها لتيليجرام لأول مرة.
    check_and_notify_scheduled_posts()

    extraction_mode = choose_extraction_mode()
    if extraction_mode == "1":
        log.info("🧲 وضع الاستخراج: استخراج الخبر كاملاً (فتح كل صفحة) — المنطق الجديد")
    elif extraction_mode == "2":
        log.info("🗂️  وضع الاستخراج: XML المحلي — نفس المنطق الحالي")
    else:
        log.info("🗂️  وضع الاستخراج: RSS المساء — نفس المنطق الحالي")

    auto_mode = choose_auto_mode()
    if auto_mode:
        log.info("🗂️  الوضع: تلقائي — كل خبر يُنشر بقسمه الخاص حسب ملفه المصدر")
        category_name = None
    else:
        category_name = choose_category_name()
        log.info(f"🗂️  الوضع: يدوي — القسم الموحّد المختار: {category_name}")

    gemini_mode = choose_gemini_mode()
    _GEMINI_MODE_LABELS = {
        "1": "مع Gemini عنوان+متن (الافتراضي)",
        "2": "مع Gemini عنوان فقط دون المتن",
        "3": "بدون Gemini عنوان+متن (كما استُخرج حرفياً)",
    }
    log.info(f"🤖 وضع Gemini (لكل الفيدات/الأقسام ما عدا آراء واتجاهات): {_GEMINI_MODE_LABELS[gemini_mode]}")

    # ⚠️ كل الأوضاع الثلاثة تحدّد مصدرها مباشرة الآن، بدون سؤال y/1/2 الإضافي
    # بـ choose_feed_sources (أصبحت غير مستخدمة)، لأن السؤال الجديد بالأعلى
    # (1/2/3) يحدّد المصدر بدقة لكل وضع.
    if extraction_mode == "2":
        selected_feeds = dict(RSS_FEED_CATEGORIES)
    elif extraction_mode == "3":
        selected_feeds = {RSS_MASA_URL: RSS_MASA_CATEGORY}
    else:  # "1" — استخراج كامل: فيد عدن تايم الحي + فيد الجزيرة (اليمن) + فيد المساء برس معاً
        selected_feeds = {
            RSS_ADEN_TM_FULL_URL: RSS_ADEN_TM_FULL_CATEGORY,
            RSS_ALJAZEERA_YEMEN_URL: RSS_ALJAZEERA_YEMEN_CATEGORY,
            RSS_MASA_URL: RSS_MASA_CATEGORY,
        }

    if RSS_MASA_URL in selected_feeds:
        masa_category = choose_category_name()
        selected_feeds[RSS_MASA_URL] = masa_category
        log.info(f"🗂️  قسم نشر RSS المساء: {masa_category}")
    log.info(f"📡 المصادر المختارة: {list(selected_feeds.keys())}")

    existing_urls = get_existing_source_urls()
    log.info(f"🗂️  عدد الروابط المنشورة مسبقاً (آخر 500): {len(existing_urls)}")

    blocked_links = load_blocked_links()
    if blocked_links:
        log.info(f"🚫 عدد الروابط الممنوعة دائماً من جلسات سابقة: {len(blocked_links)}")

    items = collect_recent_items(selected_feeds)
    new_items = [
        it for it in items
        if it["link"] not in existing_urls and it["link"] not in blocked_links
    ]
    log.info("─" * 60)
    log.info(f"✅ إجمالي الأخبار الجديدة المؤهلة للنشر: {len(new_items)}")
    log.info("─" * 60)

    if not new_items:
        log.info("لا يوجد أخبار جديدة حالياً.")
        return

    if extraction_mode == "1":
        log.info("─" * 60)
        log.info(f"🧲 استخراج النص الكامل لكل خبر من صفحته ({len(new_items)} خبر)...")
        apply_full_extraction(new_items)
        excluded_count = sum(1 for it in new_items if it.get("_excluded"))
        if excluded_count:
            new_items = [it for it in new_items if not it.get("_excluded")]
            log.info(f"🚫 استُبعد {excluded_count} خبر (قسم غير معروف/تعذّر اكتشافه من صفحته).")
        log.info("─" * 60)

    if not new_items:
        log.info("لا يوجد أخبار جديدة حالياً بعد الاستبعاد.")
        return

    for idx, it in enumerate(new_items, start=1):
        shown_category = it["category"] if auto_mode else category_name
        log.info(f"  {idx}) [{shown_category}] {it['title'][:65]}")

    excluded_indices = choose_excluded_items(new_items)

    session_skip_indices = choose_skipped_items(new_items)

    watermark_indices = choose_watermark_items(new_items)

    overrides = choose_category_overrides(new_items, auto_mode, category_name)

    schedule_indices = choose_scheduled_items(new_items)

    opinion_count = sum(1 for it in new_items if it["category"] in NO_REWRITE_CATEGORIES)
    non_opinion_count = len(new_items) - opinion_count

    print("\n" + "═" * 55)
    parts = []
    if non_opinion_count:
        if gemini_mode == "1":
            parts.append(f"{non_opinion_count} خبر تُعاد صياغته بالكامل (عنوان+متن) عبر Gemini")
        elif gemini_mode == "2":
            parts.append(f"{non_opinion_count} خبر يُعاد صياغة عنوانه فقط عبر Gemini، ومتنه كما استُخرج")
        else:
            parts.append(f"{non_opinion_count} خبر يُنشر حرفياً كما استُخرج (بدون أي استدعاء لـ Gemini)")
    if opinion_count:
        parts.append(f"{opinion_count} مقال رأي (عنوانه فقط يُعاد صياغته، نصه كما هو)")
    print("  " + "، و".join(parts))
    print("═" * 55)
    publish_mode_minutes = choose_publish_mode()
    if publish_mode_minutes is not None:
        # طريقة النشر: جدولة الكل بفارق زمني — تطغى على أي اختيار فردي أعلاه
        schedule_indices = set(range(1, len(new_items) + 1))
        interval_minutes = publish_mode_minutes
        print(f"  📅 سيُجدول كل الأخبار ({len(new_items)}) بفارق {interval_minutes} دقيقة بينها (status=scheduled)")
        print("  ⚠️  تأكد إن دالة publish-scheduled بمشروعك تُستدعى دورياً (cron)،")
        print("     وإلا الأخبار المجدولة ما راح تُنشر فعلياً.")
    elif schedule_indices:
        print(f"  📅 {len(schedule_indices)} خبر سيُجدول (status=scheduled)، والباقي سيُنشر مباشرة فوراً (published)")
        print("  ⚠️  تأكد إن دالة publish-scheduled بمشروعك تُستدعى دورياً (cron)،")
        print("     وإلا الأخبار المجدولة ما راح تُنشر فعلياً.")
        while True:
            raw = input("اكتب الفارق الزمني بالدقائق بين كل خبر مجدول والثاني (مثلاً 30 أو 60 أو 15د): ")
            interval_minutes = parse_interval_minutes(raw)
            if interval_minutes:
                break
            print("⚠️  قيمة غير صحيحة، جرّب مرة ثانية (رقم صحيح أكبر من صفر).")
    else:
        interval_minutes = None
        print("  سيُنشرون جميعاً مباشرة (status=published) بتواريخ النشر الأصلية من المصدر")
    choice = input("اكتب 'تأكيد' للبدء الفعلي، أو أي شيء آخر للإلغاء: ").strip()
    if choice != "تأكيد":
        log.info("⏹️  تم الإلغاء.")
        return

    ok = fail = skipped = excluded_count = session_skipped_count = 0
    schedule_cursor = datetime.now(timezone.utc)
    if schedule_indices and interval_minutes:
        # ✅ أول خبر مجدول (بترتيبه بالقائمة) ياخذ فارق واحد على الأقل من الآن،
        # عشان ما ينشر أول خبر فوراً بالغلط لحظة ما يوصله دور الـ Cron
        # (بدل ما يبدأ التوزيع من "الآن" نفسه، يبدأ من "الآن + فارق واحد")
        schedule_cursor += timedelta(minutes=interval_minutes)
    for idx, it in enumerate(new_items, start=1):
        if idx in excluded_indices:
            log.info(f"🚫 تم منع الخبر {idx} من النشر بناءً على اختيارك: {it['title'][:60]}")
            excluded_count += 1
            continue

        if idx in session_skip_indices:
            log.info(f"⏭️  تم تخطي الخبر {idx} بهذه الجلسة فقط (سيظهر طبيعياً بجلسة قادمة): {it['title'][:60]}")
            session_skipped_count += 1
            continue

        post_category = overrides.get(idx) or (it["category"] if auto_mode else category_name)

        is_opinion = it["category"] in NO_REWRITE_CATEGORIES

        if is_opinion:
            log.info(f"📝 إعادة صياغة العنوان فقط (مقال رأي منسوب — النص الأصلي بلا تعديل): {it['title'][:60]}")
            raw_body = it["raw_body"].strip()
            new_title = rewrite_title_only(it["title"], raw_body)
            final_title = new_title or it["title"].strip()
            final_excerpt = (raw_body[:200].rstrip() + "…") if len(raw_body) > 200 else raw_body
            final_content = raw_body
        elif gemini_mode == "3":
            log.info(f"📄 نشر حرفي كما استُخرج (بدون أي استدعاء لـ Gemini، لا للعنوان ولا للمتن): {it['title'][:60]}")
            raw_body = it["raw_body"].strip()
            final_title = it["title"].strip()
            final_excerpt = (raw_body[:200].rstrip() + "…") if len(raw_body) > 200 else raw_body
            final_content = raw_body
        elif gemini_mode == "2":
            log.info(f"📝 إعادة صياغة العنوان فقط عبر Gemini (المتن كما استُخرج حرفياً): {it['title'][:60]}")
            raw_body = it["raw_body"].strip()
            new_title = rewrite_title_only(it["title"], raw_body)
            final_title = new_title or it["title"].strip()
            final_excerpt = (raw_body[:200].rstrip() + "…") if len(raw_body) > 200 else raw_body
            final_content = raw_body
        else:
            log.info(f"✍️  إعادة صياغة: {it['title'][:60]}")
            try:
                rewritten = rewrite_article(it["title"], it["raw_body"], post_category,
                                             source_feed=it.get("source_feed"))
            except Exception as e:
                log.error(f"  ❌ فشلت إعادة الصياغة: {e}")
                fail += 1
                continue

            if not rewritten:
                skipped += 1
                continue

            final_title = rewritten["title"].strip()
            final_excerpt = rewritten["excerpt"].strip()
            final_content = rewritten["content"]

        formatted_content = format_content_paragraphs(final_content)
        item_date = it["pub_date"].isoformat()

        starts_with_ajel = _normalize_ar_for_blocking(it["title"]).lstrip().startswith("عاجل")

        if post_category in NO_IMAGE_CATEGORIES or starts_with_ajel:
            if starts_with_ajel and post_category not in NO_IMAGE_CATEGORIES:
                log.info(f"  🚫 العنوان يبدأ بـ«عاجل»: يُنشر بدون صورة — تم تجاوز جلب/رفع الصورة.")
            else:
                log.info(f"  🚫 قسم «{post_category}»: يُنشر بدون صورة دائماً — تم تجاوز جلب/رفع الصورة.")
            image_url = None
            image_url_square = None
        else:
            # ⚠️ خط احتياطي og:image يُفعَّل فقط لمصادر RSS عبر الإنترنت (مثل
            # RSS_MASA_URL)، ولا يُطبَّق إطلاقاً على ملفات XML المحلية
            # (aden-tm-*.xml) — تلك الملفات أصلاً فيها enclosure جاهز لو
            # الصورة متوفرة، وما نبي نضيف طلب شبكة إضافي غير ضروري عليها.
            is_remote_feed = str(it.get("source_feed", "")).startswith(("http://", "https://"))
            fallback_article_url = it.get("link") if is_remote_feed else None
            image_url, image_url_square = get_post_image_url(
                it.get("image_url"), fallback_article_url,
                apply_watermark=idx in watermark_indices,
            )

        # 🗂️ شمسان نيوز يخزّن القسم كـ category_id (UUID)، مو نص حر — لازم
        # نحله لمعرّف فعلي قبل النشر. لو ما انحل، نتخطى الخبر (بدل ما يُنشر
        # بلا قسم فيختفي من كل صفحات الموقع).
        category_id = get_category_id(post_category)
        if not category_id:
            fail += 1
            continue

        if idx in schedule_indices:
            publish_dt = schedule_cursor
            schedule_cursor += timedelta(minutes=interval_minutes)
            publish_time = publish_dt.isoformat()
            record_status = "scheduled"
            # ✅ created_at يحفظ تاريخ النشر الأصلي دائماً (مو وقت تشغيل البوت)
            created_updated = item_date
        else:
            publish_dt = datetime.fromisoformat(item_date)
            publish_time = item_date
            record_status = "published"
            created_updated = item_date

        # ✅ word_count/reading_time محسوبان تلقائياً من النص الفعلي (قبل تغليفه
        # بوسوم <p>)، وكذلك keywords تلقائية من العنوان والمحتوى — تعبئة
        # مباشرة للأعمدة الجديدة بجدول posts دون الحاجة لتدخّل يدوي بالأدمن.
        word_count, reading_time = word_stats(final_content)
        auto_keywords = extract_keywords(final_title, final_content)

        record = {
            "title": final_title,
            "slug": make_slug(final_title),
            "excerpt": final_excerpt,
            "content": formatted_content,
            "category_id": category_id,
            "source_url": it["link"],
            "status": record_status,
            "created_at": created_updated,
            "updated_at": created_updated,
            # ⚠️ published_at (وليس created_at) هو ما يعتمد عليه الموقع فعلياً
            # لترتيب الأخبار ولبناء رابط المقال /YYYY/MM/DD/slug (articlePath
            # بملف news.types.ts) — لهذا هو نفس قيمة publish_time دائماً، سواء
            # نشر فوري أو مجدول (بحالة الجدولة تكون تاريخاً مستقبلياً).
            "published_at": publish_time,
            "cover_image": image_url,
            # ✅ تُملأ تلقائياً بكل خبر — نفس عمودي seo_title/seo_description
            # اللي تقرأهما PostDetail.tsx بموقع شمسان نيوز لعرضهما لجوجل.
            "seo_title": generate_meta_title(final_title),
            "seo_description": generate_meta_description(final_excerpt),
            # ⭐ أي خبر بأحد أقسام FEATURED_SLIDER_CATEGORIES يُعلَّم مميّز تلقائياً
            # (نفس عمود is_featured اللي يقرأ منه السلايدر بالصفحة الرئيسية)
            "is_featured": post_category in FEATURED_SLIDER_CATEGORIES,
            # ⚠️ عمود is_featured بشمسان بديل عن featured بالجنوب فويس؛ وعمود
            # is_opinion (bool) هو البديل عن حقل author النصي المستقل — الموقع
            # يستخدمه لعرض تصميم صفحة المقال كمقال رأي.
            "is_opinion": is_opinion,
            # 🆕 الأعمدة الجديدة المضافة لجدول posts (سبق تعبئتها بالأدمن يدوياً
            # فقط) — الآن تُحسب تلقائياً بكل خبر ينشره البوت.
            "word_count": word_count,
            "reading_time": reading_time,
            "keywords": auto_keywords,
        }

        # مقالات الرأي المستثناة من الصياغة تُنسب صراحة لكاتبها الأصلي عبر
        # author_id (لا يوجد عمود author نصي مستقل بشمسان نيوز — فقط author_id)
        if is_opinion:
            opinion_author_name = it.get("author") or DEFAULT_OPINION_AUTHOR
            # ⭐ ربط المقال بجدول authors عبر author_id — بدونه لن تظهر بطاقة
            # "بقلم الكاتب" بالموقع (PostDetail.tsx يعرضها فقط عبر post.authors،
            # الناتج من join مع author_id).
            author_id = get_or_create_author_id(opinion_author_name)
            if author_id:
                record["author_id"] = author_id
            else:
                log.warning(
                    f"⚠️  تعذّر ربط/إنشاء الكاتب '{opinion_author_name}' بجدول authors — "
                    "سيُنشر المقال لكن دون بطاقة الكاتب بالموقع."
                )

        post_id = sb_insert(record)
        if post_id:
            ok += 1
            status_label = "جُدول" if record_status == "scheduled" else "نُشر"
            log.info(f"  ✅ {status_label}: {record['title'][:60]}")

            seed_views(post_id)

            # رابط واحد صحيح يُستخدم لتيليجرام وللأرشفة معاً (بدل رابط /share الميت)
            canonical_url = build_canonical_url(record["slug"], record["published_at"])

            if record_status == "scheduled":
                # ⏸️ وضع الجدولة: الخبر لسا status=scheduled وما نُشر فعلياً بالموقع بعد،
                # فما نرسل رابطه لتيليجرام الآن (سيكون رابط ميت مؤقتاً). بدلاً من هذا،
                # نضيفه لقائمة الانتظار (PENDING_SCHEDULED_FILE) ليُفحص تلقائياً بأول
                # تشغيلة قادمة للسكربت عبر check_and_notify_scheduled_posts، ويُرسل
                # رابطه لتيليجرام فقط بعد التأكد إن الـ Cron نشره فعلياً (published).
                pending = load_pending_scheduled()
                pending.append({"id": post_id, "title": record["title"], "slug": record["slug"]})
                save_pending_scheduled(pending)
                log.info("  ⏸️  تيليجرام: مؤجَّل لحين تأكيد النشر الفعلي بجلسة قادمة")
            else:
                if send_to_telegram(record["title"], canonical_url):
                    log.info("  📢 أُرسل لتليجرام")

                log_discovery_ready([canonical_url])
        else:
            fail += 1

    log.info("═" * 60)
    log.info(f"📊 نُشر: {ok} / فشل: {fail} / تُخُطّي: {skipped} / مُنع من النشر: {excluded_count} / تُخطّي بهذه الجلسة فقط: {session_skipped_count}")
    log.info("═" * 60)


if __name__ == "__main__":
    main()

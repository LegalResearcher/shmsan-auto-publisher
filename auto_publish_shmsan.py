import fcntl
import os
import sys

# ══════════════════════════════════════════════════════════════════════
#  🔒 نفس آلية القفل المستخدمة بسكربت حصاد اليوم (auto_publish_alittihad_
#  alkhabar.py) — تمنع تشغيلين متزامنين لهذا السكربت لو تشغيل سابق عبر
#  cron لسه شغّال ولم ينتهِ قبل بداية التشغيل التالي.
# ══════════════════════════════════════════════════════════════════════
# ملاحظة: على Render لا حاجة فعلية لهذا القفل — Render يضمن عدم تشغيل
# تشغيلتين متزامنتين لنفس Cron Job أصلاً (Single-run guarantee). أبقيناه هنا
# فقط لبقاء نفس السلوك لو شغّلت السكربت يدوياً من مكان آخر بالتوازي.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.environ.get("BOT_DATA_DIR", os.path.join(_SCRIPT_DIR, "shmsan_data"))
LOCK_FILE_PATH = os.path.join(_DATA_DIR, "auto_publish_shmsan.lock")

sys.path.insert(0, _SCRIPT_DIR)

from shmsan_news_bot import (
    log,
    RSS_MASA_URL,
    RSS_MASA_CATEGORY,
    RSS_ADEN_TM_FULL_URL,
    RSS_ADEN_TM_FULL_CATEGORY,
    RSS_ALQUDS_SPORT_URL,
    RSS_ALQUDS_SPORT_CATEGORY,
    RSS_YPAGENCY_FULL_URL,
    RSS_YPAGENCY_FULL_CATEGORY,
    RSS_YPAGENCY_YEMEN_URL,
    RSS_YPAGENCY_YEMEN_CATEGORY,
    NO_REWRITE_CATEGORIES,
    NO_IMAGE_CATEGORIES,
    FEATURED_SLIDER_CATEGORIES,
    DEFAULT_OPINION_AUTHOR,
    get_category_id,
    check_system_logs_size,
    check_and_notify_scheduled_posts,
    get_existing_source_urls,
    get_recent_published_titles,
    get_recent_published_titles_from_db,
    log_published_title,
    save_published_title_to_db,
    check_similar_published_title_db,
    load_blocked_links,
    save_blocked_link,
    collect_recent_items,
    remove_duplicate_news,
    apply_full_extraction,
    rewrite_article,
    rewrite_title_only,
    get_post_image_url,
    format_content_paragraphs,
    make_slug,
    get_or_create_author_id,
    generate_meta_title,
    generate_meta_description,
    sb_insert,
    seed_views,
    build_canonical_url,
    send_to_telegram,
    log_discovery_ready,
    word_stats,
    extract_keywords,
)

# ══════════════════════════════════════════════════════════════════════
#  🔒 نسخة تلقائية — تعمل فقط على المصادر التي لا تحتاج تحديث ملفات XML
#  يدوياً (فيد عدن تايم الحي الكامل + فيدا وكالة اليمن لقسمي المحافظات المحتلة
#  واليمن–سياسية + فيد المساء برس)، بنفس منطق وضع "1" (استخراج
#  الخبر كاملاً) + الوضع التلقائي (كل خبر
#  بقسمه الخاص) من shmsan_news_bot.py الأصلي، لكن بدون أي تفاعل يدوي (بدون
#  أسئلة استخراج/تصنيف/استثناء/جدولة، وبدون طلب كتابة "تأكيد") — تُنشر كل
#  الأخبار فوراً (status=published). تُشغَّل عبر cron كل عدة دقائق.
# ══════════════════════════════════════════════════════════════════════

SELECTED_FEEDS = {
    # ⏸️ فيد عدن تايم موقوف مؤقتاً — أعد السطر لتفعيله من جديد:
    # RSS_ADEN_TM_FULL_URL: RSS_ADEN_TM_FULL_CATEGORY,
    # ⏸️ فيد القدس الرياضية موقوف مؤقتاً — أعد السطر لتفعيله من جديد:
    # RSS_ALQUDS_SPORT_URL: RSS_ALQUDS_SPORT_CATEGORY,
    # 🆕 وكالة الصحافة اليمنية — قسم المحافظات المحتلة فقط، ويُنشر في
    # "أخبار وتقارير" كما هو محدد في المصدر.
    RSS_YPAGENCY_FULL_URL: RSS_YPAGENCY_FULL_CATEGORY,
    # ⏸️ فيد وكالة اليمن — اليمن السياسية موقوف مؤقتاً — أعد السطرين لتفعيله من جديد:
    # RSS_YPAGENCY_YEMEN_URL: RSS_YPAGENCY_YEMEN_CATEGORY,
    # ⏸️ المساء برس مستبعد مؤقتاً — أعد هذا السطر لتفعيله من جديد:
    # RSS_MASA_URL: RSS_MASA_CATEGORY,
}

# 🚫 أقسام مستبعدة كلياً من النشر التلقائي (تبقى متاحة بالوضع التفاعلي
# اليدوي بـjanoub_news_bot.py كما هي، هذا الاستبعاد خاص بالسكربت التلقائي فقط)
EXCLUDED_AUTO_CATEGORIES = {
    "آراء واتجاهات",
    "أسعار العملات والذهب",
}

# 🚫 كلمات مفتاحية تستبعد الخبر تلقائياً لو ظهرت بعنوانه أو نصه، بمعزل عن
# قسمه (نشرات متكررة قصيرة العمر لا تناسب أرشيف الموقع: عاجل/طقس/كهرباء/
# أذان/ذهب/صرف). خاص بالسكربت التلقائي فقط، مثل EXCLUDED_AUTO_CATEGORIES.
BLOCKED_AUTO_TOPIC_KEYWORDS = [
    "عاجل",
    "الطقس",
    "الكهرباء",
    "اذان",
    "أذان",
    "الذهب",
    "الصرف",
]


def _is_blocked_auto_topic(it: dict) -> bool:
    text = f"{it.get('title', '')} {it.get('raw_body', '')}"
    return any(kw in text for kw in BLOCKED_AUTO_TOPIC_KEYWORDS)


def run():
    log.info("═" * 60)
    log.info("  📰  شمسان نيوز — تشغيل تلقائي (عدن تايم + فيدا وكالة اليمن + المساء برس)")
    log.info("═" * 60)

    check_system_logs_size()
    check_and_notify_scheduled_posts()

    existing_urls = get_existing_source_urls()
    blocked_links = load_blocked_links()
    # 📋 دمج المصدرين معاً: السجل المحلي (سريع، بلا شبكة، لكن يتصفّر كل
    # تشغيلة على GitHub Actions) + سجل Supabase الدائم (يدوم بين كل
    # التشغيلات). فشل أحد المصدرين لا يوقف الآخر — كل دالة تتعامل مع فشلها
    # بنفسها وترجّع قائمة فارغة عند أي مشكلة.
    recent_published = get_recent_published_titles(hours=24) + get_recent_published_titles_from_db(hours=24)

    items = collect_recent_items(SELECTED_FEEDS)
    new_items = [
        it for it in items
        if it["link"] not in existing_urls and it["link"] not in blocked_links
    ]
    new_items = remove_duplicate_news(new_items, history_items=recent_published)

    blocked_topic_count = sum(1 for it in new_items if _is_blocked_auto_topic(it))
    if blocked_topic_count:
        new_items = [it for it in new_items if not _is_blocked_auto_topic(it)]
        log.info(f"🚫 استُبعد {blocked_topic_count} خبر (يحتوي كلمة ممنوعة: عاجل/طقس/كهرباء/أذان/ذهب/صرف).")

    log.info("─" * 60)
    log.info(f"✅ إجمالي الأخبار الجديدة المؤهلة للنشر: {len(new_items)}")
    log.info("─" * 60)

    if not new_items:
        log.info("لا يوجد أخبار جديدة حالياً.")
        return

    log.info(f"🧲 استخراج النص الكامل لكل خبر من صفحته ({len(new_items)} خبر)...")
    apply_full_extraction(new_items)
    excluded_count = sum(1 for it in new_items if it.get("_excluded"))
    if excluded_count:
        new_items = [it for it in new_items if not it.get("_excluded")]
        log.info(f"🚫 استُبعد {excluded_count} خبر (قسم غير معروف/تعذّر اكتشافه من صفحته).")

    # فلتر الأقسام المستبعدة كلياً من النشر التلقائي — بعد الاستخراج الكامل
    # مباشرة، لأن قسم أخبار عدن تايم يُصحَّح تلقائياً بهذه المرحلة تحديداً
    category_excluded_count = sum(1 for it in new_items if it["category"] in EXCLUDED_AUTO_CATEGORIES)
    if category_excluded_count:
        new_items = [it for it in new_items if it["category"] not in EXCLUDED_AUTO_CATEGORIES]
        log.info(
            f"🚫 استُبعد {category_excluded_count} خبر (قسم مستبعد من النشر التلقائي: "
            "آراء واتجاهات/أسعار العملات والذهب)."
        )

    if not new_items:
        log.info("لا يوجد أخبار جديدة حالياً بعد الاستبعاد.")
        return

    ok = fail = skipped = duplicate_count = 0

    for it in new_items:
        post_category = it["category"]
        is_opinion = post_category in NO_REWRITE_CATEGORIES

        if is_opinion:
            log.info(f"📝 إعادة صياغة العنوان فقط (مقال رأي منسوب — النص الأصلي بلا تعديل): {it['title'][:60]}")
            raw_body = it["raw_body"].strip()
            new_title = rewrite_title_only(it["title"], raw_body)
            final_title = new_title or it["title"].strip()
            final_excerpt = (raw_body[:200].rstrip() + "…") if len(raw_body) > 200 else raw_body
            final_content = raw_body
        else:
            log.info(f"✍️  إعادة صياغة: {it['title'][:60]}")
            try:
                rewritten = rewrite_article(
                    it["title"],
                    it["raw_body"],
                    post_category,
                    source_feed=it.get("source_feed"),
                )
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

        # 🔁 فحص تكرار عبر قاعدة البيانات مباشرة (check_similar_published_title):
        # نفس الفحص المضاف بـjanoub_news_bot.py — يسأل Supabase هل نُشر خبر
        # مشابه لهذا العنوان خلال آخر 48 ساعة، بمعزل تام عن السجل المحلي
        # (recent_published/remove_duplicate_news بالأعلى يفحصان فقط داخل هذا
        # الجهاز)، فيمسك التكرار حتى لو شُغّل البوت من جهاز آخر أو انحذف/تأخر
        # تحديث السجل المحلي.
        dup_match = check_similar_published_title_db(final_title)
        if dup_match:
            log.info(
                f"  🔁 تخطي — يشابه خبراً منشوراً سابقاً (تشابه "
                f"{dup_match['similarity_score']:.0%}): «{dup_match['title'][:60]}»"
            )
            duplicate_count += 1
            continue

        formatted_content = format_content_paragraphs(final_content)
        item_date = it["pub_date"].isoformat()

        # 🗂️ شمسان نيوز يخزّن القسم كـ category_id (UUID) — لازم نحله قبل
        # النشر، وإلا يُتخطى الخبر (بدل نشره بلا قسم فيختفي من كل الموقع).
        category_id = get_category_id(post_category)
        if not category_id:
            fail += 1
            continue

        if post_category in NO_IMAGE_CATEGORIES:
            log.info(f"  🚫 قسم «{post_category}»: يُنشر بدون صورة دائماً — تم تجاوز جلب/رفع الصورة.")
            image_url = None
            image_url_square = None
        else:
            # 🖼️ صورة الخبر الأصلية من المصدر (RSS) — استُخرجت مسبقاً وقت
            # جلب الفيد عبر extract_image_url() وخُزّنت بـit["image_url"].
            # لو فارغة: get_post_image_url تجرب og:image من صفحة الخبر (it["link"])
            # كخط احتياطي قبل الاستسلام.
            image_url, image_url_square = get_post_image_url(
                it.get("image_url"),
                headline_text=final_title,
                article_url=it.get("link"),
            )

        # ✅ نفس منطق shmsan_news_bot.py الرئيسي: word_count/reading_time
        # محسوبان تلقائياً من النص الفعلي (قبل تغليفه بوسوم <p>)، وkeywords
        # تلقائية من العنوان والمحتوى.
        word_count, reading_time = word_stats(final_content)
        auto_keywords = extract_keywords(final_title, final_content)

        record = {
            "title": final_title,
            "slug": make_slug(final_title),
            "excerpt": final_excerpt,
            "content": formatted_content,
            "category_id": category_id,
            "source_url": it["link"],
            "status": "published",
            "created_at": item_date,
            "updated_at": item_date,
            # ⚠️ published_at (وليس created_at) هو ما يعتمد عليه الموقع لترتيب
            # الأخبار ولبناء رابط المقال — نفس منطق shmsan_news_bot.py الرئيسي.
            "published_at": item_date,
            "cover_image": image_url,
            "seo_title": generate_meta_title(final_title),
            "seo_description": generate_meta_description(final_excerpt),
            "is_featured": post_category in FEATURED_SLIDER_CATEGORIES,
            "is_opinion": is_opinion,
            # 🆕 نفس تعبئة shmsan_news_bot.py الرئيسي للأعمدة الجديدة بجدول posts.
            "word_count": word_count,
            "reading_time": reading_time,
            "keywords": auto_keywords,
        }

        if is_opinion:
            opinion_author_name = it.get("author") or DEFAULT_OPINION_AUTHOR
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
            log.info(f"  ✅ نُشر: {record['title'][:60]}")
            log_published_title(record["title"], record["created_at"], embedding=it.get("_title_embedding"))
            save_published_title_to_db(record["title"], record["created_at"], embedding=it.get("_title_embedding"))
            save_blocked_link(it["link"])  # منع إعادة النشر مستقبلاً حتى لو حُذف الخبر من الموقع
            seed_views(post_id)
            canonical_url = build_canonical_url(record["slug"], record["published_at"])

            if send_to_telegram(record["title"], canonical_url):
                log.info("  📢 أُرسل لتليجرام")

            log_discovery_ready([canonical_url])
        else:
            fail += 1

    log.info("═" * 60)
    log.info(f"📊 نُشر: {ok} / فشل: {fail} / تُخُطّي: {skipped} / مكرر (قاعدة البيانات): {duplicate_count}")
    log.info("═" * 60)


def _acquire_lock_or_exit():
    """يفتح ملف القفل ويحاول مسكه بشكل غير محظر (LOCK_EX | LOCK_NB). لو
    تشغيل آخر ماسكه فعلاً، يطبع تحذيراً ويخرج فوراً بدون معالجة أي خبر.
    القفل يتحرر تلقائياً عند خروج العملية (نجاح أو فشل أو استثناء)."""
    os.makedirs(os.path.dirname(LOCK_FILE_PATH), exist_ok=True)
    lock_file = open(LOCK_FILE_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.warning(
            "  🔒 تشغيل سابق لهذا السكربت لسه شغّال (القفل ممسوك) — "
            "تخطي هذا التشغيل بالكامل لمنع معالجة نفس الأخبار مرتين."
        )
        sys.exit(0)
    return lock_file


if __name__ == "__main__":
    _lock_handle = _acquire_lock_or_exit()
    run()

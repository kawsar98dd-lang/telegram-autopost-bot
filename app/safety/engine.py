"""Evidence-based, bounded safety analysis for Telegram Auto Poster.

Rules are conservative heuristics, not Telegram moderation predictions. Only compact
findings are persisted; post text is never copied into notification storage.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

DEFAULT_POSTS_PER_HOUR = 5
DUPLICATE_WINDOW_HOURS = 24
OVERLAP_MINUTES = 15


def normalize_text(value: str | None) -> str:
    # Preserve actual URLs for exact comparison; URL repetition is analyzed separately.
    value = (value or "").casefold()
    value = re.sub(r"\s+", " ", value).strip()
    return value


def content_fingerprint(value: str | None) -> str:
    normalized = normalize_text(value)
    return hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest() if normalized else ""


def token_similarity(left: str | None, right: str | None) -> float:
    """Simple local token Jaccard similarity; not semantic language understanding."""
    a = set(re.findall(r"[\w]+", normalize_text(left)))
    b = set(re.findall(r"[\w]+", normalize_text(right)))
    if min(len(a), len(b)) < 8:
        return 0.0
    return len(a & b) / max(1, len(a | b))


def extract_urls(value: str | None) -> set[str]:
    return {u.rstrip(".,;!?)]}").casefold() for u in re.findall(r"(?:https?://|www\.)[^\s<>]+", value or "", flags=re.I)}


def language_for(text: str, selected: str = "") -> str:
    if selected in {"bn", "en", "hi", "ur", "ar"}:
        return selected
    for ch in text:
        code = ord(ch)
        if 0x0600 <= code <= 0x06FF:
            return "ar"  # Arabic/Urdu script; Urdu can be selected explicitly.
        if 0x0900 <= code <= 0x097F:
            return "hi"
        if 0x0980 <= code <= 0x09FF:
            return "bn"
    return "en"


MESSAGES = {
 "bn": {
  "title":"নিরাপত্তা পর্যবেক্ষণ", "intro":"এটি বাস্তব অ্যাপ ডেটার উপর ভিত্তি করে নিয়ম-নির্ভর সতর্কতা তৈরি করে; Telegram-এর সিদ্ধান্ত অনুমান করে না।",
  "empty":"এখনো কোনো সতর্কতা সংরক্ষিত নেই। সতর্কতা না থাকা নিরাপত্তার নিশ্চয়তা নয়।", "ack":"পড়া হয়েছে", "question":"আপনার প্রশ্ন লিখুন", "ask":"প্রশ্ন করুন",
  "help":"আমি পোস্টিং ফ্রিকোয়েন্সি, পুনরাবৃত্ত কনটেন্ট, শিডিউল ওভারল্যাপ, ব্যর্থ জব এবং রেকর্ড করা FloodWait সম্পর্কে উপলভ্য তথ্য ব্যাখ্যা করতে পারি।",
  "flood":"রেকর্ডে FloodWait দেখা গেলে Telegram-এর নির্দিষ্ট অপেক্ষার সময় শেষ হতে দিন। বারবার সঙ্গে সঙ্গে retry করবেন না এবং সীমা এড়ানোর চেষ্টা করবেন না।",
  "unknown":"এই প্রশ্নের নির্দিষ্ট উত্তর দিতে পর্যাপ্ত রেকর্ড নেই। সংশ্লিষ্ট schedule, job status এবং Logs পৃষ্ঠা পরীক্ষা করুন।",
  "refuse":"আমি অ্যাপ ব্যবহার, কার্যকলাপ এবং সমর্থিত ত্রুটি ব্যাখ্যায় সাহায্য করতে পারি; source code, গোপন তথ্য বা নিরাপত্তা/লাইসেন্স বাইপাসের নির্দেশ দিতে পারি না।",
  "count":"সক্রিয় সতর্কতা", "observed":"পর্যবেক্ষিত তথ্য", "recommend":"প্রস্তাবিত পদক্ষেপ", "status":"অবস্থা", "created":"তৈরির সময়", "acknowledged":"পড়া হয়েছে",
 },
 "en": {
  "title":"Safety Monitoring", "intro":"Deterministic warnings use recorded application evidence; they do not predict Telegram moderation decisions.",
  "empty":"No saved warnings yet. The absence of a warning does not guarantee safety.", "ack":"Mark as read", "question":"Ask a question", "ask":"Ask",
  "help":"I can explain available evidence about posting frequency, repeated content, schedule overlap, failed jobs, and recorded FloodWait errors.",
  "flood":"If a FloodWait is recorded, respect Telegram's stated wait duration. Do not repeatedly retry immediately or attempt to bypass the limit.",
  "unknown":"There is not enough recorded evidence to answer specifically. Check the relevant schedule, job status, and Logs page.",
  "refuse":"I can help with app usage, activity, and supported errors, but cannot provide source code, secrets, or security/licensing bypass instructions.",
  "count":"Active warnings", "observed":"Observed evidence", "recommend":"Recommended action", "status":"Status", "created":"Created", "acknowledged":"Read",
 },
 "hi": {"title":"सुरक्षा निगरानी", "intro":"ये नियम-आधारित चेतावनियाँ दर्ज ऐप डेटा पर आधारित हैं; ये Telegram के निर्णय की भविष्यवाणी नहीं करतीं।", "empty":"अभी कोई चेतावनी नहीं है। चेतावनी न होना सुरक्षा की गारंटी नहीं है।", "ack":"पढ़ा हुआ चिह्नित करें", "question":"अपना प्रश्न लिखें", "ask":"पूछें", "help":"मैं पोस्टिंग आवृत्ति, दोहराए गए कंटेंट, शेड्यूल ओवरलैप, विफल जॉब और दर्ज FloodWait समझा सकता हूँ।", "flood":"दर्ज FloodWait की अवधि का सम्मान करें। तुरंत बार-बार retry न करें और सीमा को बायपास न करें।", "unknown":"विशिष्ट उत्तर के लिए पर्याप्त रिकॉर्ड नहीं हैं। संबंधित शेड्यूल, जॉब और Logs देखें।", "refuse":"मैं ऐप उपयोग और त्रुटियों में मदद कर सकता हूँ, लेकिन स्रोत कोड, रहस्य या सुरक्षा/लाइसेंस बायपास निर्देश नहीं दे सकता।", "count":"सक्रिय चेतावनियाँ", "observed":"देखा गया प्रमाण", "recommend":"सुझाया गया कदम", "status":"स्थिति", "created":"बनाया गया", "acknowledged":"पढ़ा गया"},
 "ur": {"title":"حفاظتی نگرانی", "intro":"یہ اصولی انتباہات ریکارڈ شدہ ایپ ڈیٹا پر مبنی ہیں؛ یہ Telegram کے فیصلوں کی پیش گوئی نہیں کرتے۔", "empty":"ابھی کوئی محفوظ انتباہ نہیں۔ انتباہ نہ ہونا حفاظت کی ضمانت نہیں۔", "ack":"پڑھا ہوا نشان زد کریں", "question":"اپنا سوال لکھیں", "ask":"پوچھیں", "help":"میں پوسٹنگ کی رفتار، دہرائے گئے مواد، شیڈول اوورلیپ، ناکام جابز اور ریکارڈ شدہ FloodWait سمجھا سکتا ہوں۔", "flood":"درج FloodWait کے وقت کا احترام کریں۔ فوراً بار بار دوبارہ کوشش یا حد سے بچنے کی کوشش نہ کریں۔", "unknown":"مخصوص جواب کے لیے کافی ریکارڈ نہیں۔ متعلقہ شیڈول، جاب اور Logs دیکھیں۔", "refuse":"میں ایپ کے استعمال اور خرابیوں میں مدد کر سکتا ہوں، مگر سورس کوڈ، راز یا حفاظتی/لائسنس بائی پاس ہدایات نہیں دے سکتا۔", "count":"فعال انتباہات", "observed":"دیکھا گیا ثبوت", "recommend":"تجویز کردہ قدم", "status":"حالت", "created":"وقت", "acknowledged":"پڑھا گیا"},
 "ar": {"title":"مراقبة السلامة", "intro":"تعتمد التحذيرات القائمة على القواعد على بيانات التطبيق المسجلة، ولا تتنبأ بقرارات Telegram.", "empty":"لا توجد تحذيرات محفوظة بعد. عدم وجود تحذير لا يضمن السلامة.", "ack":"وضع علامة مقروء", "question":"اكتب سؤالك", "ask":"اسأل", "help":"يمكنني شرح الأدلة المتاحة حول تكرار النشر والمحتوى المتكرر وتداخل الجداول والمهام الفاشلة وأخطاء FloodWait المسجلة.", "flood":"إذا سُجل FloodWait، فاحترم مدة الانتظار التي حددها Telegram. لا تكرر المحاولة فوراً ولا تحاول تجاوز الحد.", "unknown":"لا توجد سجلات كافية لإجابة محددة. راجع الجدول والمهمة وصفحة السجلات ذات الصلة.", "refuse":"يمكنني المساعدة في استخدام التطبيق والأخطاء المدعومة، لكن لا يمكنني تقديم الشيفرة أو الأسرار أو تعليمات تجاوز الأمان/الترخيص.", "count":"التحذيرات النشطة", "observed":"الأدلة المرصودة", "recommend":"الإجراء المقترح", "status":"الحالة", "created":"وقت الإنشاء", "acknowledged":"مقروء"},
}


def answer_question(question: str, language: str) -> str:
    q = question.casefold()
    msg = MESSAGES[language]
    if any(x in q for x in ("source code", "secret", "license bypass", "লাইসেন্স বাইপাস", "সোর্স কোড", "كود المصدر", "سورس کوڈ")):
        return msg["refuse"]
    if "floodwait" in q or "flood wait" in q or "ফ্লাডওয়েট" in q:
        return msg["flood"]
    if any(x in q for x in ("help", "সাহায্য", "ayuda", "मदद", "مدد", "مساعدة")):
        return msg["help"]
    if any(x in q for x in ("schedule", "শিডিউল", "failed", "ব্যর্থ", "risk", "ঝুঁকি", "warning", "সতর্কতা", "post", "পোস্ট")):
        return msg["help"] + " " + msg["unknown"]
    return msg["unknown"]


class SafetyMonitor:
    """Periodic, bounded server-side analysis. Does not mutate posts or schedules."""
    def __init__(self, db, clock=lambda: datetime.now(timezone.utc)):
        self.db = db
        self.clock = clock

    async def _notify(self, user_id: str, rule: str, entity_key: str, title: str, severity: str,
                      evidence: str, recommendation: str, *, window_hours: int = 6) -> bool:
        now = self.clock()
        bucket = int(now.timestamp() // (window_hours * 3600))
        dedupe = hashlib.sha256(f"{rule}:{entity_key}".encode()).hexdigest()
        row = await self.db.fetchrow(
            "INSERT INTO safety_notifications (id,user_id,rule_key,dedupe_key,window_bucket,title,severity,evidence,recommendation,status,created_at,updated_at) "
            "VALUES (gen_random_uuid(),$1,$2,$3,$4,$5,$6,$7,$8,'new',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) "
            "ON CONFLICT (user_id,dedupe_key,window_bucket) DO NOTHING RETURNING id",
            user_id, rule, dedupe, bucket, title[:140], severity, evidence[:400], recommendation[:500])
        return row is not None

    async def scan(self) -> dict[str, int]:
        users = await self.db.fetch("SELECT id FROM users WHERE is_active=TRUE ORDER BY id LIMIT 500")
        counts = {"users": 0, "created_candidates": 0, "errors": 0}
        for user in users:
            uid = str(user["id"])
            counts["users"] += 1
            try:
                jobs = await self.db.fetch(
                    "SELECT id, schedule_id, group_id, group_title, text_snapshot, status, error_code, error_message, "
                    "scheduled_for, posted_at, created_at FROM posting_jobs WHERE user_id=$1 "
                    "AND created_at >= CURRENT_TIMESTAMP - INTERVAL '7 days' ORDER BY created_at DESC LIMIT 200", uid)
                # Duplicate/repeated content: compare normalized local snapshots, scoped to same destination.
                seen: dict[tuple[str, str], Any] = {}
                seen_texts: dict[str, list[Any]] = defaultdict(list)
                seen_urls: dict[tuple[str, str], Any] = {}
                duplicate_notified = False
                url_notified = False
                for job in jobs:
                    body = job.get("text_snapshot") or ""
                    content = content_fingerprint(body)
                    gid = str(job.get("group_id") or job.get("group_title") or "unknown")
                    if not content:
                        continue
                    moment = job.get("posted_at") or job.get("scheduled_for") or job.get("created_at")
                    key = (gid, content)
                    prior = seen.get(key)
                    if prior is not None and not duplicate_notified:
                        prev_moment = prior.get("posted_at") or prior.get("scheduled_for") or prior.get("created_at")
                        try:
                            delta = abs((moment - prev_moment).total_seconds()) if moment and prev_moment else 0
                        except (AttributeError, TypeError):
                            delta = 0
                        if delta <= DUPLICATE_WINDOW_HOURS * 3600:
                            created = await self._notify(uid, "repeated_content", gid + ":" + content[:24],
                                "Repeated content deserves review", "review",
                                "Two recent job snapshots for the same destination have identical normalized text within 24 hours. Content is not stored in this warning.",
                                "Review the destination and posting intent; vary or reduce repeated messages when appropriate.")
                            counts["created_candidates"] += int(created)
                            duplicate_notified = True
                    elif prior is None:
                        seen[key] = job
                    if not duplicate_notified:
                        for previous in seen_texts[gid]:
                            prev_moment = previous.get("posted_at") or previous.get("scheduled_for") or previous.get("created_at")
                            try:
                                near_delta = abs((moment - prev_moment).total_seconds()) if moment and prev_moment else DUPLICATE_WINDOW_HOURS * 3600 + 1
                            except (AttributeError, TypeError):
                                near_delta = DUPLICATE_WINDOW_HOURS * 3600 + 1
                            if near_delta <= DUPLICATE_WINDOW_HOURS * 3600 and token_similarity(body, previous.get("text_snapshot")) >= 0.90:
                                created = await self._notify(uid, "near_duplicate_content", gid + ":" + content[:24],
                                    "Very similar content deserves review", "review",
                                    "Two recent job snapshots for the same destination have at least 90% token-set similarity. This is a local heuristic, not semantic analysis.",
                                    "Review whether repeating this message is necessary and allowed by the destination.")
                                counts["created_candidates"] += int(created)
                                duplicate_notified = True
                                break
                    seen_texts[gid].append(job)
                    for url in extract_urls(body):
                        url_key = (gid, hashlib.sha256(url.encode()).hexdigest())
                        previous_url = seen_urls.get(url_key)
                        previous_moment = (previous_url.get("posted_at") or previous_url.get("scheduled_for") or previous_url.get("created_at")) if previous_url is not None else None
                        try:
                            url_delta = abs((moment - previous_moment).total_seconds()) if moment and previous_moment else DUPLICATE_WINDOW_HOURS * 3600 + 1
                        except (AttributeError, TypeError):
                            url_delta = DUPLICATE_WINDOW_HOURS * 3600 + 1
                        if previous_url is not None and url_delta <= DUPLICATE_WINDOW_HOURS * 3600 and not url_notified:
                            created = await self._notify(uid, "repeated_url", gid + ":" + url_key[1][:24],
                                "Repeated link deserves review", "low",
                                "The same URL appears in multiple recent job snapshots for one destination. The URL itself is not stored in this warning.",
                                "Confirm that the link is relevant and permitted, and avoid unnecessary repetition.")
                            counts["created_candidates"] += int(created)
                            url_notified = True
                        else:
                            seen_urls[url_key] = job
                # Per-destination posting frequency: configurable heuristic, not an official Telegram limit.
                try:
                    threshold = max(2, min(100, int(os.getenv("SAFETY_POSTS_PER_DESTINATION_PER_HOUR", str(DEFAULT_POSTS_PER_HOUR)))))
                except ValueError:
                    threshold = DEFAULT_POSTS_PER_HOUR
                freq = await self.db.fetch(
                    "SELECT COALESCE(group_id::text,group_title) AS destination, MAX(group_title) AS group_title, COUNT(*) AS n "
                    "FROM posting_jobs WHERE user_id=$1 AND status='posted' AND posted_at >= CURRENT_TIMESTAMP - INTERVAL '1 hour' "
                    "GROUP BY COALESCE(group_id::text,group_title) HAVING COUNT(*) >= $2 LIMIT 50", uid, threshold)
                for row in freq:
                    created = await self._notify(uid, "posting_frequency", str(row["destination"]), "Posting frequency deserves review", "review",
                        f"{int(row['n'])} successful jobs were recorded for one destination in the last hour; local review threshold is {threshold}.",
                        "Confirm the destination permits this frequency and reduce unnecessary repetition if appropriate.")
                    counts["created_candidates"] += int(created)
                failures = await self.db.fetch(
                    "SELECT COALESCE(group_id::text,group_title) AS destination, MAX(group_title) AS group_title, COUNT(*) AS n, "
                    "MAX(COALESCE(error_code,'')) AS error_code FROM posting_jobs WHERE user_id=$1 AND status='failed' "
                    "AND updated_at >= CURRENT_TIMESTAMP - INTERVAL '6 hours' GROUP BY COALESCE(group_id::text,group_title) HAVING COUNT(*) >= 3 LIMIT 50", uid)
                for row in failures:
                    created = await self._notify(uid, "repeated_failures", str(row["destination"]), "Repeated posting failures", "high",
                        f"{int(row['n'])} failed jobs were recorded for one destination in the last six hours. The underlying cause is not inferred from count alone.",
                        "Open Logs and inspect the recorded error code/message. Avoid repeated immediate retries when a rate limit is indicated.")
                    counts["created_candidates"] += int(created)
                flood = await self.db.fetch(
                    "SELECT id,group_title,error_code,updated_at FROM posting_jobs WHERE user_id=$1 AND updated_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours' "
                    "AND (LOWER(COALESCE(error_code,'')) LIKE '%floodwait%' OR LOWER(COALESCE(error_message,'')) LIKE '%floodwait%' "
                    "OR LOWER(COALESCE(error_message,'')) LIKE '%flood wait%') ORDER BY updated_at DESC LIMIT 20", uid)
                for row in flood:
                    created = await self._notify(uid, "floodwait", str(row["id"]), "Recorded Telegram FloodWait", "high",
                        "A recent job record contains a FloodWait-related error. The exact wait duration is shown only if present in the original error details.",
                        "Respect Telegram's stated wait duration; do not retry repeatedly or attempt to bypass the restriction.", window_hours=24)
                    counts["created_candidates"] += int(created)
                overlaps = await self.db.fetch(
                    "SELECT st1.group_id::text AS group_id, g.title AS group_title, s1.id::text AS schedule_a, s2.id::text AS schedule_b, "
                    "s1.next_run_at AS time_a,s2.next_run_at AS time_b FROM schedules s1 "
                    "JOIN schedule_targets st1 ON st1.schedule_id=s1.id AND st1.user_id=s1.user_id "
                    "JOIN schedules s2 ON s2.user_id=s1.user_id AND s2.id>s1.id AND s2.status='active' "
                    "JOIN schedule_targets st2 ON st2.schedule_id=s2.id AND st2.user_id=s2.user_id AND st2.group_id=st1.group_id "
                    "LEFT JOIN telegram_groups g ON g.id=st1.group_id AND g.user_id=s1.user_id "
                    "WHERE s1.user_id=$1 AND s1.status='active' AND s1.next_run_at IS NOT NULL AND s2.next_run_at IS NOT NULL "
                    "AND ABS(EXTRACT(EPOCH FROM (s1.next_run_at-s2.next_run_at))) <= $2 LIMIT 50", uid, OVERLAP_MINUTES * 60)
                for row in overlaps:
                    created = await self._notify(uid, "schedule_overlap", str(row["group_id"])+":"+str(row["schedule_a"])+":"+str(row["schedule_b"]),
                        "Schedules may overlap", "review", f"Two active schedules target the same destination with next runs within {OVERLAP_MINUTES} minutes.",
                        "Review both schedules and confirm the timing is intentional; no schedule was changed.")
                    counts["created_candidates"] += int(created)
            except Exception:
                counts["errors"] += 1
                # Do not log post text, raw SQL values, or customer data here.
        return counts

# Localized notification text is generated from the rule identifier, never by translating
# untrusted post content. Numeric evidence remains available in English if not localized.
_NOTIFICATION_TEXT = {
 "bn": {
  "repeated_content": ("পুনরাবৃত্ত কনটেন্ট পর্যালোচনা করুন", "একই গন্তব্যের সাম্প্রতিক দুটি job snapshot-এ ২৪ ঘণ্টার মধ্যে একই normalized text পাওয়া গেছে। সতর্কতায় মূল কনটেন্ট রাখা হয়নি।", "গন্তব্য ও পোস্টের উদ্দেশ্য পর্যালোচনা করুন; প্রয়োজন হলে পুনরাবৃত্তি কমান।"),
  "near_duplicate_content": ("খুব মিলযুক্ত কনটেন্ট পর্যালোচনা করুন", "একই গন্তব্যের দুটি job snapshot-এর token-set similarity অন্তত ৯০%। এটি স্থানীয় heuristic, semantic analysis নয়।", "বার্তাটি আবার পাঠানো প্রয়োজন কি না এবং গন্তব্যে অনুমোদিত কি না যাচাই করুন।"),
  "repeated_url": ("একই লিংক পুনরাবৃত্ত হয়েছে", "একই গন্তব্যের একাধিক সাম্প্রতিক job snapshot-এ একই URL পাওয়া গেছে। সতর্কতায় URL রাখা হয়নি।", "লিংকটি প্রাসঙ্গিক ও অনুমোদিত কি না যাচাই করুন এবং অপ্রয়োজনীয় পুনরাবৃত্তি এড়ান।"),
  "posting_frequency": ("পোস্টিংয়ের ঘনত্ব পর্যালোচনা করুন", "গত এক ঘণ্টায় একই গন্তব্যে একাধিক সফল পোস্ট রেকর্ড হয়েছে; এটি স্থানীয় পর্যালোচনা-সীমা, Telegram-এর official limit নয়।", "গন্তব্যের নিয়ম যাচাই করুন এবং অপ্রয়োজনীয় পোস্ট কমান।"),
  "repeated_failures": ("বারবার পোস্ট ব্যর্থ হয়েছে", "গত ছয় ঘণ্টায় একই গন্তব্যে একাধিক ব্যর্থ job রেকর্ড হয়েছে। শুধু সংখ্যা দেখে কারণ নিশ্চিত করা যায় না।", "Logs-এ error code/message দেখুন; rate limit হলে বারবার তাৎক্ষণিক retry করবেন না।"),
  "floodwait": ("Telegram FloodWait রেকর্ড হয়েছে", "সাম্প্রতিক job record-এ FloodWait-সম্পর্কিত ত্রুটি আছে। মূল record-এ থাকলেই অপেক্ষার সঠিক সময় জানা যাবে।", "Telegram-এর নির্দিষ্ট অপেক্ষার সময় মেনে চলুন; বারবার retry বা সীমা এড়ানোর চেষ্টা করবেন না।"),
  "schedule_overlap": ("শিডিউল ওভারল্যাপ হতে পারে", "একই গন্তব্যে দুটি সক্রিয় schedule-এর পরবর্তী run ১৫ মিনিটের মধ্যে নির্ধারিত।", "দুটি schedule পর্যালোচনা করুন। কোনো schedule স্বয়ংক্রিয়ভাবে পরিবর্তন করা হয়নি।"),
 },
 "en": {},
 "hi": {
  "repeated_content": ("दोहराई गई सामग्री की समीक्षा करें", "एक ही गंतव्य के हाल के दो job snapshots में 24 घंटे के भीतर समान normalized text मिला। चेतावनी में मूल सामग्री संग्रहीत नहीं है।", "गंतव्य और पोस्ट के उद्देश्य की समीक्षा करें; आवश्यक हो तो दोहराव कम करें।"),
  "near_duplicate_content": ("बहुत मिलती-जुलती सामग्री की समीक्षा करें", "एक ही गंतव्य के दो job snapshots में token-set similarity कम से कम 90% है। यह स्थानीय heuristic है, semantic analysis नहीं।", "जाँचें कि संदेश दोहराना आवश्यक है और गंतव्य के नियमों के अनुसार है।"),
  "repeated_url": ("लिंक दोहराया गया है", "एक ही गंतव्य के कई हाल के job snapshots में समान URL मिला। चेतावनी में URL संग्रहीत नहीं है।", "लिंक की प्रासंगिकता और अनुमति जाँचें तथा अनावश्यक दोहराव से बचें।"),
  "posting_frequency": ("पोस्टिंग आवृत्ति की समीक्षा करें", "पिछले एक घंटे में एक गंतव्य के लिए कई सफल पोस्ट दर्ज हुए; यह स्थानीय समीक्षा सीमा है, Telegram की आधिकारिक सीमा नहीं।", "गंतव्य के नियम जाँचें और अनावश्यक पोस्ट कम करें।"),
  "repeated_failures": ("बार-बार पोस्ट विफल हुए", "पिछले छह घंटों में एक गंतव्य के लिए कई विफल job दर्ज हुए। केवल गिनती से कारण की पुष्टि नहीं होती।", "Logs में error code/message देखें; rate limit होने पर तुरंत बार-बार retry न करें।"),
  "floodwait": ("Telegram FloodWait दर्ज हुआ", "हाल के job record में FloodWait-संबंधी त्रुटि है। सटीक प्रतीक्षा समय तभी ज्ञात है जब मूल record में हो।", "Telegram की बताई प्रतीक्षा अवधि का पालन करें; बार-बार retry या सीमा को बायपास न करें।"),
  "schedule_overlap": ("शेड्यूल ओवरलैप हो सकते हैं", "एक ही गंतव्य के दो सक्रिय शेड्यूल के अगले run 15 मिनट के भीतर हैं।", "दोनों शेड्यूल की समीक्षा करें। कोई शेड्यूल अपने आप नहीं बदला गया।"),
 },
 "ur": {
  "repeated_content": ("دہرائے گئے مواد کا جائزہ لیں", "ایک ہی منزل کے حالیہ دو job snapshots میں 24 گھنٹوں کے اندر یکساں normalized متن ملا۔ اصل مواد انتباہ میں محفوظ نہیں۔", "منزل اور پوسٹ کے مقصد کا جائزہ لیں؛ ضرورت ہو تو تکرار کم کریں۔"),
  "near_duplicate_content": ("ملتے جلتے مواد کا جائزہ لیں", "ایک ہی منزل کے دو job snapshots میں token-set similarity کم از کم 90% ہے۔ یہ مقامی heuristic ہے، معنوی تجزیہ نہیں۔", "دیکھیں کہ پیغام دہرانا ضروری اور منزل کے قواعد کے مطابق ہے۔"),
  "repeated_url": ("لنک دہرایا گیا ہے", "ایک ہی منزل کے متعدد حالیہ job snapshots میں ایک ہی URL ملا۔ URL انتباہ میں محفوظ نہیں۔", "لنک کی مناسبت اور اجازت کی تصدیق کریں اور غیر ضروری تکرار سے بچیں۔"),
  "posting_frequency": ("پوسٹنگ کی رفتار کا جائزہ لیں", "گزشتہ ایک گھنٹے میں ایک منزل کے لیے متعدد کامیاب پوسٹس ریکارڈ ہوئیں؛ یہ مقامی جائزہ حد ہے، Telegram کی سرکاری حد نہیں۔", "منزل کے قواعد دیکھیں اور غیر ضروری پوسٹس کم کریں۔"),
  "repeated_failures": ("پوسٹس بار بار ناکام ہوئیں", "گزشتہ چھ گھنٹوں میں ایک منزل کے لیے متعدد ناکام jobs ریکارڈ ہوئیں۔ صرف تعداد سے وجہ ثابت نہیں ہوتی۔", "Logs میں error code/message دیکھیں؛ rate limit ہو تو فوراً بار بار retry نہ کریں۔"),
  "floodwait": ("Telegram FloodWait ریکارڈ ہوا", "حالیہ job record میں FloodWait سے متعلق خرابی ہے۔ درست انتظار کا وقت صرف اصل record میں موجود ہونے پر معلوم ہے۔", "Telegram کے بتائے ہوئے انتظار کا احترام کریں؛ بار بار retry یا حد سے بچنے کی کوشش نہ کریں۔"),
  "schedule_overlap": ("شیڈول اوورلیپ ہو سکتے ہیں", "ایک ہی منزل کے دو فعال شیڈول کے اگلے run میں 15 منٹ سے کم فرق ہے۔", "دونوں شیڈول دیکھیں؛ کوئی شیڈول خودکار طور پر تبدیل نہیں ہوا۔"),
 },
 "ar": {
  "repeated_content": ("راجع المحتوى المتكرر", "تم العثور على نص مطبّع متطابق في سجلّي مهمة حديثين للوجهة نفسها خلال 24 ساعة. لم يُحفظ المحتوى الأصلي في التحذير.", "راجع الوجهة والغرض من المنشور، وقلّل التكرار عند الحاجة."),
  "near_duplicate_content": ("راجع المحتوى شديد التشابه", "تبلغ درجة تشابه مجموعات الكلمات في سجلّي المهمة للوجهة نفسها 90% على الأقل. هذا تقدير محلي وليس فهماً دلالياً.", "تحقق من ضرورة تكرار الرسالة ومن سماح قواعد الوجهة بذلك."),
  "repeated_url": ("تم تكرار الرابط", "ظهر الرابط نفسه في عدة سجلات مهام حديثة للوجهة نفسها. لم يُحفظ الرابط في التحذير.", "تحقق من ملاءمة الرابط والسماح به وتجنب التكرار غير الضروري."),
  "posting_frequency": ("راجع وتيرة النشر", "سُجلت عدة منشورات ناجحة لوجهة واحدة خلال الساعة الماضية؛ هذه عتبة مراجعة محلية وليست حداً رسمياً من Telegram.", "راجع قواعد الوجهة وقلل المنشورات غير الضرورية."),
  "repeated_failures": ("تكررت إخفاقات النشر", "سُجلت عدة مهام فاشلة لوجهة واحدة خلال ست ساعات. العدد وحده لا يؤكد السبب.", "راجع رمز الخطأ والرسالة في السجلات؛ لا تكرر المحاولة فوراً عند وجود حد للمعدل."),
  "floodwait": ("تم تسجيل FloodWait من Telegram", "يتضمن سجل مهمة حديث خطأ متعلقاً بـ FloodWait. لا يُعرف وقت الانتظار الدقيق إلا إذا ورد في السجل الأصلي.", "احترم مدة الانتظار المحددة من Telegram ولا تكرر المحاولة أو تحاول تجاوز الحد."),
  "schedule_overlap": ("قد تتداخل الجداول", "موعد التشغيل التالي لجدولين نشطين للوجهة نفسها يفصل بينهما 15 دقيقة أو أقل.", "راجع الجدولين؛ لم يتم تغيير أي جدول تلقائياً."),
 },
}


def localize_notification(row: Any, language: str) -> dict[str, Any]:
    result = dict(row)
    translated = _NOTIFICATION_TEXT.get(language, {}).get(result.get("rule_key"))
    if translated:
        result["title"], result["evidence"], result["recommendation"] = translated
    return result

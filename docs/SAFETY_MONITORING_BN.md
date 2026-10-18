# Telegram Auto Poster — Safety Monitoring (বাংলা নির্দেশিকা)

## এটি কী করে

Safety Monitoring একটি স্থানীয়, নিয়ম-ভিত্তিক সার্ভার-সাইড ব্যবস্থা। এটি বিদ্যমান PostgreSQL ডেটা থেকে নির্দিষ্ট সংকেত খুঁজে in-app notification তৈরি করে। কোনো paid AI API বা গ্রাহকের API key প্রয়োজন হয় না। এটি generative AI নয় এবং Telegram-এর moderation decision বা account-ban পূর্বাভাস দিতে পারে না।

## যে সতর্কতাগুলো বর্তমানে সমর্থিত

- একই destination-এ গত ২৪ ঘণ্টার job snapshot-এ একই normalized text পাওয়া গেলে repeated-content review warning। অন্তত ৮টি token থাকলে token-set similarity ৯০% বা বেশি হলেও near-duplicate warning হতে পারে। কনটেন্টের কপি notification-এ রাখা হয় না।
- একই destination-এর সাম্প্রতিক job snapshot-এ একই URL বারবার পাওয়া গেলে repeated-link warning; URL নিজে notification-এ রাখা হয় না।
- গত এক ঘণ্টায় একই destination-এ সফল posting সংখ্যা `SAFETY_POSTS_PER_DESTINATION_PER_HOUR`-এর configured threshold ছুঁলে frequency review warning। ডিফল্ট threshold ৫; এটি Telegram-এর কোনো official limit নয়। প্রয়োজন হলে পরিবেশ ভেরিয়েবল হিসেবে ২–১০০ এর মধ্যে সেট করা যায়।
- একই destination-এ দুইটি active schedule-এর `next_run_at` ১৫ মিনিটের মধ্যে হলে সম্ভাব্য overlap warning।
- একই destination-এ ছয় ঘণ্টায় অন্তত ৩টি failed job হলে repeated-failure warning।
- job-এর recorded error code/message-এ FloodWait থাকলে FloodWait warning। সঠিক অপেক্ষার সময় শুধু মূল error record-এ থাকলেই জানা যায়।

এগুলো review signal, নিশ্চিত spam determination নয়। একই লেখা বৈধ কারণেও ব্যবহার হতে পারে। কোনো সতর্কতা না থাকাও নিরাপত্তার নিশ্চয়তা নয়।

## Notification কীভাবে তৈরি হয়

সক্রিয় worker সর্বোচ্চ প্রতি পাঁচ মিনিটে safety scan চালায়। scan ব্যর্থ হলেও posting cycle চালু রাখার জন্য monitoring best-effort হিসেবে আলাদা try/except-এ চলে। worker বন্ধ থাকলে background scan-ও চলবে না; deployment-এ web service ছাড়াও worker process চালু আছে নিশ্চিত করুন। প্রথম scan worker-এর প্রথম eligible cycle-এ হয়।

একই rule/entity-র notification নির্দিষ্ট সময়ের bucket-এ deduplicate হয়, যাতে প্রতিটি cycle-এ একই সতর্কতা বারবার তৈরি না হয়। Notification `/safety` পৃষ্ঠায় দেখা যায়। “পড়া হয়েছে” চাপলে status `read` হয়; এতে ঝুঁকি সমাধান হয়েছে বলে ধরা হয় না। কোনো schedule/post স্বয়ংক্রিয়ভাবে পরিবর্তন হয় না।

## Database update

নতুন additive migration: `migrations/0007_safety_notifications.sql`। এটি `safety_notifications` টেবিল ও index তৈরি করে; পুরোনো টেবিল বা রেকর্ড মুছে না। আপনার deployment-এ `AUTO_MIGRATE=true` থাকলে স্বাভাবিক startup-এ migration প্রয়োগ হবে। যদি `AUTO_MIGRATE=false` হয়, তবে production পরিবর্তনের আগে আপনার স্বাভাবিক release/migration প্রক্রিয়ায় migration চালাতে হবে। লাইসেন্সিং schema বা licensing code পরিবর্তন করা হয়নি।

## গোপনীয়তা ও নিরাপত্তা

- পৃষ্ঠা ও notification query authenticated user ID দিয়ে scope করা হয়। acknowledgement-ও notification ID এবং authenticated user ID দুটোই যাচাই করে।
- প্রশ্নে সর্বোচ্চ ৫০০ অক্ষর নেওয়া হয় এবং বিদ্যমান rate limiter ব্যবহার করা হয়।
- কোনো external AI service-এ post content পাঠানো হয় না।
- notification-এ raw post text রাখা হয় না; duplicate rule কেবল normalized text fingerprint তুলনা করে।
- assistant read-only: post, schedule, account বা license পরিবর্তন করে না; shell/SQL/filesystem tool নেই।
- এই প্রকল্পের source code গ্রাহকের নিজস্ব hosting-এ থাকলে, server নিয়ন্ত্রণকারী ব্যক্তি source inspect করতে পারেন। এই ব্যবস্থা সেই বাস্তব সীমাবদ্ধতা দূর করে না।

## ভাষা

বাংলা, English, हिन्दी, اردو ও العربية-র জন্য মৌলিক localized text এবং সমর্থিত safety-rule notification-এর অনুবাদ আছে। কিছু সাধারণ প্রশ্নের উত্তর অনুবাদ করা হয়েছে; সব ধরনের natural-language প্রশ্নের পূর্ণাঙ্গ বোঝাপড়া বা অনুবাদ দাবি করা হচ্ছে না। Language dropdown দিয়ে ভাষা বেছে নিন। RTL ভাষার জন্য পৃষ্ঠায় right-to-left direction প্রয়োগ করা হয়।

## Deployment / Android ব্যবহার

1. মূল ZIP আলাদা করে সংরক্ষণ করুন।
2. নতুন ZIP প্রথমে staging বা test deployment-এ ব্যবহার করুন; সরাসরি live service প্রতিস্থাপন করবেন না।
3. web service এবং বিদ্যমান worker দুটোই চালু আছে নিশ্চিত করুন। নতুন আলাদা paid service প্রয়োজন নেই; বিদ্যমান worker-ই safety scan চালায়।
4. migration `0007_safety_notifications.sql` প্রয়োগ হয়েছে কি না deployment log-এ যাচাই করুন।
5. `/safety` খুলে নিজের account-এর notification দেখুন এবং প্রশ্নের form পরীক্ষা করুন।
6. টেস্ট শেষে posting, schedule edit, pause/resume, logs এবং licensing flow আলাদাভাবে regression-test করুন।

Rollback: deployment থেকে আগের release/artifact পুনরায় deploy করুন। নতুন টেবিলটি additive; rollback-এর জন্য টেবিল drop করার দরকার নেই। পুরোনো release যদি নতুন টেবিল না চেনে, সেটি সাধারণত উপেক্ষা করবে। তবে পুরোনো release-এ ফিরে যাওয়ার আগে আপনার hosting-এর normal database-backup procedure অনুসরণ করুন।

## সীমাবদ্ধতা ও যাচাই

- Content matching exact normalized text matching; semantic/near-duplicate AI detection নয়। URL-গুলো normalization-এ একই placeholder হয়, তাই একই body-র URL বদলালেও warning হতে পারে।
- overlap কেবল stored `next_run_at` ও shared schedule target থেকে পরীক্ষা করা হয়; custom schedule recurrence-এর সব ভবিষ্যৎ occurrence বিশ্লেষণ করা হয় না।
- frequency threshold heuristic, official Telegram limit নয়।
- deployment provider dashboard, Telegram moderation state, private server metrics বা external logs পড়া হয় না।
- real Telegram, production PostgreSQL, concurrent worker deployment ও Android viewport manual verification আবশ্যক।

# Step 6 GitHub-এ আপডেট করার গাইড (Android ফোন থেকে)

আপনাকে কোনো কমান্ড চালাতে হবে না। ব্রাউজারেই হবে।

## পদ্ধতি ক: ফাইল আপলোড (সবচেয়ে সহজ)
1. ফোনে `telegram-auto-poster-step6.zip` ডাউনলোড করে "Files" অ্যাপে Extract করুন।
2. ব্রাউজারে (Chrome, "Desktop site" চালু) আপনার রিপোজিটরি খুলুন: github.com/kawsar98dd-lang/telegram-autopost-bot
3. **Add file → Upload files** চাপুন। Extract করা ফোল্ডারের **ভেতরের সব ফাইল ও ফোল্ডার** (`app`, `migrations`, `tests`, `docs`, `scripts`, `requirements.txt`, `README.md`, `CHANGELOG.md` ইত্যাদি) সিলেক্ট করে আপলোড করুন। ফোল্ডার আপলোড না হলে পদ্ধতি খ দেখুন।
4. নিচে "Commit changes" এ লিখুন: `Step 6 scheduler and worker`, তারপর **Commit directly to main** → **Commit changes**।

## পদ্ধতি খ: ডেস্কটপ-সাইট ছাড়া ফোল্ডার আপলোড না হলে
GitHub মোবাইলে ফোল্ডার আপলোড অনেক সময় কাজ করে না। তখন একটি কম্পিউটার/বন্ধুর পিসি, অথবা GitHub Codespaces ব্যবহার করুন; অথবা ZIP-এর `step6-scheduler-worker.patch` ফাইলটি কোনো ডেভেলপারকে দিয়ে `git apply step6-scheduler-worker.patch` চালিয়ে নিন।

## আপডেটের পর
1. রিপোজিটরির **Actions** ট্যাব খুলুন। তিনটি চেক সবুজ হওয়ার অপেক্ষা করুন: *All verifications must have run and passed*, *Docker image and compose stack*, *Tests (real PostgreSQL, real FastAPI)*।
2. কোনোটি লাল হলে সেটি খুলে লেখার স্ক্রিনশট নিয়ে পাঠান (কোনো পাসওয়ার্ড/কী পাঠাবেন না)।
3. Render: web service Deploy করুন। **গুরুত্বপূর্ণ:** শুধু web service পোস্ট পাঠায় না। Render-এ একটি *Background Worker* তৈরি করুন (একই GitHub repo, Docker, Start command: `python -m app.workers.main`), এবং web service-এর `DATABASE_URL`, `SESSION_ENCRYPTION_KEY`, `APP_SECRET`, `APP_ENV`, `LICENSE_ENFORCEMENT` মানগুলো Render ড্যাশবোর্ড থেকেই কপি করে বসান (এগুলো এখানে পাঠাবেন না)।
4. `docs/STEP6_SCHEDULER.md` এর "Manual verification" তালিকা অনুযায়ী পরীক্ষা করুন।

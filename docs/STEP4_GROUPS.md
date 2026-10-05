# Step 4 - Telegram groups: discovery, posting-permission check, selection

This step lets a signed-in user load the groups their **own connected Telegram account** belongs to, see where Telegram
currently allows that account to post, and choose posting targets. It does **not** post, schedule, compose or run any
worker job (later steps). The Telegram Bot API is not used; everything goes through the user's account over MTProto (Telethon).

## How groups are discovered
* Groups page -> **Refresh groups** reads the account's chat list (dialogs) from Telegram with the stored, encrypted session.
  No code or password is asked again and no second session is created.
* Basic groups, supergroups and forum supergroups are listed, public or private. A group does not need to be public and no
  invite link is needed; only chats the account is already a member of appear. Private chats and broadcast channels are not shown.
* The client connects, reads, and disconnects again for every refresh. The refresh only reads: it never sends, joins or leaves.
* At most 1,500 chats are read per refresh; if the account has more, the list is marked as truncated in the service result.

## How posting permission is determined
For every group the application uses only the rights **Telegram itself reports for this account**:

| Telegram says | Shown as |
|---|---|
| member, group allows sending | Can post |
| group-wide "members cannot send messages" (and the account is not owner/admin) | Cannot post |
| owner or admin | Can post (not bound by the group-wide restriction) |
| this account is muted/restricted there (restriction not yet expired) | Restricted |
| this account is banned from the group | Restricted |
| Telegram restricts the chat for this account/region | Restricted |
| account left, group deactivated/upgraded, or Telegram denies access | Unavailable |

Only groups marked **Can post** are selectable (checked again on the server; a crafted request is refused). The verdict is
Telegram's own state at refresh time. Slow mode and anti-spam limits are **not** detected here; they still apply when posting
(later steps must respect them). The application never tries to bypass a ban, restriction, slow mode or rate limit.

## Selecting groups
* Tick the groups to use and press **Save selected groups**. The saved selection replaces the previous one for that account,
  is stored in PostgreSQL (`telegram_groups.is_enabled`), and survives page reloads and refreshes. At most 200 groups can be selected.
* Several connected Telegram accounts are supported: each group belongs to exactly one account and every account has its own
  list and selection (switch accounts with the links at the top of the page).

## Refresh / resync and permission changes
* **Refresh groups** updates names, usernames, types and verdicts in place (no duplicates) and records the refresh time.
* If a selected group is no longer postable (permissions changed, muted, banned) it is **deselected automatically**.
* If the account has left or lost access to a group, the row is kept but flagged **Unavailable** and unselected. If the account joins
  again, the group becomes available again, **not** automatically selected.
* If Telegram answers with a wait time (FloodWait) the wait is shown and stored: the application sends **no** further request to
  Telegram for that account until it has passed. Refreshes are also limited to 6 per 10 minutes per user.
* If Telegram no longer accepts the session (for example it was ended in the Telegram app), the account is marked **session expired**
  and must be connected again on the Telegram accounts page; the last stored group list stays visible but nothing can be refreshed.

## Security and privacy
* Every query is scoped by the signed-in user **and** the account; ids from the browser (URL, form fields, hidden inputs) are untrusted.
  Another user's account or group id behaves exactly like an unknown id (404) and never changes anything. The database also refuses
  a group row that does not belong to the account's user (composite foreign key).
* Only title, public username, type, Telegram chat id and the verdict are stored. No member lists, no messages, no phone numbers
  (the account's phone is shown masked), no session or credentials are shown or logged. Titles are cleaned and always HTML-escaped.
* The page uses the existing authentication, CSRF protection, secure cookies, rate limiting and error pages.

## Known limitations
* Broadcast channels are not supported as targets. Forum supergroups are listed, but posting to a specific topic is not part of this step.
* Slow mode, per-message rights (media, links, polls) and anti-spam outcomes are only known when a message is actually sent.
* A very large account may exceed the 1,500-chat read limit.
* No automatic background refresh: press **Refresh groups** (the verdict can change at any time in Telegram).

## Manual verification on the Render development environment
Use your own account; **never send codes, passwords, API ID/hash, sessions or `.env` content to anyone**. Report only PASS/FAIL and visible messages.

1. Open the dashboard on the Render URL and sign in as the administrator. Open **Groups**. Expected: your connected Telegram
   account and **No groups loaded yet** (the old "Available in an upcoming release" text is gone).
2. Tap **Refresh groups**. Expected: "Groups refreshed from Telegram." and the real groups of your account (name, type,
   Public/Private, chat id, posting verdict). If the wait time is shown, wait and retry once.
3. Compare with the Telegram app: a group where you can write must show **Can post** with a checkbox.
4. If you have a group where you cannot write (announcement-only, muted, or where you are restricted), it must show
   **Cannot post** / **Restricted** and have **no** checkbox ("Not selectable").
5. Tick one test group and tap **Save selected groups**. Expected: "Selection saved." and "1 selected".
6. Reload the page (and open it again after a few minutes): the checkbox is still ticked.
7. Tap **Refresh groups** again: the selection is still there and no group appears twice.
8. Second-user isolation: create a second application user (the app has no registration page yet, so this needs a database
   console and is **not available on Render Free**: report "not run"). The automated tests cover this case.
9. In Telegram, leave the test group (or have its permission changed), refresh: it must become **Unavailable**/**Cannot post** and unselected.
10. Check Render **Logs** for secrets: search the first 6 characters of your API hash and your phone digits: no results.

PASS = steps 1-7 and 10 behave as described (step 9 if you can arrange it); report any message that appears on the page for FAIL.

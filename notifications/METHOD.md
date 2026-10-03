# Stage 4.3 — Opt-in Daily Brief desktop delivery (local Windows only)

One local Windows notification when a **new** completed session appears in stored data, carrying the counts of that
session's Stage 4.2 Daily Brief. Not AI, not a market alert, not a recommendation, not a scanner trigger, not an order
system, not email / SMS / webhook / cloud push. Nothing leaves this computer.

## Flow

```
Stage 3.7 scheduler check (00:15 ET by default; also "Check automation now")
  -> forward capture (if on) -> saved-scan checks (if any alerts on) -> the cycle's summary is stored
  -> "after_cycle" extensions: Daily Brief delivery
       latest stored session S (brief.daily.stored_sessions) -> nothing if S <= the last handled session
       -> brief.daily.build(S) (read-only, unchanged) -> compose() -> PENDING claim -> OS adapter -> final status
```

* **Opt-in**: OFF by default (`daily_brief_delivery_settings.enabled`). Enabling records the latest stored session as
  `BASELINE` (no notification for it or anything older). Re-enabling after a pause baselines again — no replay.
* **At most once**: `UNIQUE (brief_session, channel)` + a `PENDING` claim written before the OS call, under the lease
  `daily_brief_delivery` (Stage 3.7 `app_leases`). A second check, a restart or a second server never re-sends; an
  interrupted `PENDING` row is never re-sent either. `FAILED` is final — no automatic retry.
* **No backfill**: only the latest stored session is considered; sessions stored while the server was off or while
  delivery was off are never sent later.
* **Activity** (`notify_only_if_activity`, default ON): a Stage 4.2 count above zero — RULES MET change events, forward
  captures, forward sessions not captured (MISSED), completed reference cycles, data / continuity issues. A saved scan
  checked without a change is not activity. No activity -> `SKIPPED_NO_ACTIVITY` row, no notification.
* **Timing**: delivery is an `after_cycle` extension, so the brief reflects the whole cycle — including a saved-scan
  `PARTIAL_FAILURE` stored in the cycle's summary (shown as a data / continuity issue). Delivery enabled on its own keeps
  the scheduler running; the check then only reads stored data (no market work).
* **Never from the page**: opening, refreshing or navigating the Daily Brief sends nothing; there is no "send the
  current brief" button. Stage 4.1 alerts stay in-app (no per-alert notification).

## Message (deterministic; Windows limits: title 63, body 255 characters)

```
Stock Agent · Daily Strategy Brief
Sep 30 close
2 rule-state changes · 3 forward captures · 1 completed cycle
1 data / continuity issue                                   (only when > 0)
AMD newly meets the saved entry rules (Pullback v1).        (at most 2 change lines,
MU is no longer in RULES MET (Pullback v1); now INCOMPLETE DATA.   in the brief's order)
+1 more change
Open Daily Brief for details.
```

Fingerprint = sha256 of {brief_session, channel, title, body} (no timestamps). The test notification uses fixed server
text ("Stock Agent test notification" / "Desktop notifications are working. TEST — this is not a Daily Brief.") and is
stored as kind `TEST` (no brief session). No endpoint accepts notification text.

## OS adapter (`notifications/windows.py`)

`send(title, body) -> {status: DELIVERED | FAILED | UNSUPPORTED_PLATFORM, error_code}` — standard-library `ctypes`
calling the native Win32 `Shell_NotifyIconW` API (Windows 10 / 11 show it as a toast). No dependency, no process, no
network. Windows ties a notification to a hidden message-only window and a notification-area icon; one icon per calling
thread is kept (the scheduler thread's lives as long as the server) and every icon is removed at exit. Windows labels
the source as the Python host. Non-Windows: `UNSUPPORTED_PLATFORM`. Tests and the browser harness use a fake adapter
and fail on any real call.

## Stage 4.4 — click to open, and the Stock Agent identity

**Activation.** Clicking a Daily Brief notification — or the test notification — opens exactly one fixed local URL:

    http://127.0.0.1:8000/#daily-brief        (http://<FASTAPI_HOST>:<FASTAPI_PORT>/#daily-brief from config.py)

Windows sends `NIN_BALLOONUSERCLICK` to the adapter's hidden window; the adapter's message loop calls `on_click()`, which
compares the URL against a two-entry allow-list (`127.0.0.1` / `localhost`, the configured port, path `/`, no query,
fragment `daily-brief` — exact string match, no parsing) and opens it with the standard-library `webbrowser.open`.
Anything else (another host, scheme, port, path, a query, a non-loopback `FASTAPI_HOST`) is refused and nothing opens.
The target never comes from the browser, the API, the database, a strategy name or the notification text (a strategy
named `https://evil.example` is inert text). A click writes nothing and creates no history row; clicking several times
simply opens the Daily Brief several times. The page reads the fixed fragment once at load and uses the existing
navigation (Strategy Lab tab + the Daily Brief workspace, latest stored session), then clears it — no new router. If the
server is not running, the browser shows its normal "cannot connect" page; nothing tries to start the server.

**Adapter lifecycle.** One adapter per process, started by the first notification: one daemon thread
(`stock-agent-notify`) owns one hidden message-only window and runs its message loop; the notification-area icon is added
with the first notification and reused. `shutdown()` (server shutdown via the FastAPI lifespan, and `atexit`) removes the
icon, closes the window (`WM_CLOSE` -> `DestroyWindow` -> `WM_QUIT`), joins the thread and unregisters the window class.
Start / stop is idempotent; tests start and stop it repeatedly and find no thread, window or icon left.

**Identity.** AppUserModelID `StockAgent.Local`, display name `Stock Agent` (static, code-owned). Windows shows an
unpackaged desktop app's notifications under the display name registered for its AppUserModelID, which needs one
per-user registry value (approved for Stage 4.4):

    HKEY_CURRENT_USER\Software\Classes\AppUserModelId\StockAgent.Local    DisplayName (REG_SZ) = "Stock Agent"

It is written when desktop notifications are turned ON (and at server start when they already are) and deleted when they
are turned OFF — only that key, per user, no administrator rights, no installer, shortcut or service. To remove it
manually: delete that key in the Registry Editor. While it exists, the adapter sets the process AppUserModelID
(`SetCurrentProcessExplicitAppUserModelID`) before creating its window; otherwise the process keeps its default identity
and the card says "Python · branding deferred". Whether a given Windows build shows "Stock Agent" is for the user to
confirm — the adapter only reports that the registration exists (`branding_active`).

**Security.** No subprocess, shell, PowerShell, `cmd.exe`, `os.startfile`, `ShellExecute` or `CreateProcess` in this
code (the only opener is `webbrowser.open` with the allow-listed URL); no arbitrary URL, browser command or app id; no
HTML in notifications; the registry is touched only by `notifications/identity.py`, only that one key and value.

## Tables (additive; `app_settings` and `PRAGMA user_version` untouched)

`daily_brief_delivery_settings` (enabled, notify_only_if_activity) and `daily_brief_deliveries` (append-only; only
PENDING -> final once) — see `database/delivery_migrations.py`.

## Known limitations

Stage 4.4: a stale notification clicked while the server is off opens an unavailable localhost page; the browser may
open a new tab or window; Windows controls notification-center retention; branding depends on Windows honouring the
registered display name for a legacy notification-area toast.

Local Windows only; the Stock Agent server must be running and the computer awake (no service, no scheduled task); no
historical backfill; one brief summary per eligible session; no email / SMS / push; no AI; no ranking or
recommendations; the same daily-close limitations as the Daily Brief; a delivery made at the check time does not wait
for a later forward-capture retry of the same session.

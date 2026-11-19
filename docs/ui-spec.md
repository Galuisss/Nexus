# UI specification

Interface behavior and frontend implementation. See the [API contract](api-contract.md) for requests and events.

## Language

Settings → General provides English and Simplified Chinese. The browser language sets the initial choice (`zh*` selects Simplified Chinese; other languages select English); `nexus-language` in localStorage stores an explicit choice. Switching updates the current UI, dates, accessibility labels, offline page, and push notifications while preserving drafts, attachments, pending answers, and expanded tool records.

Chinese source messages are translation keys in `i18n.js`; `en.js` contains English text and counted labels. Use `t()` in renderers and `data-i18n` / `data-i18n-{attribute}` for DOM bindings. Include the locale in render signatures that cache localized output. Session messages, names, file contents, and model-provided text retain their original language.

English terminology follows the [Codex App Server](https://learn.chatgpt.com/docs/app-server) and the session behavior exposed by Nexus: *session*, *turn*, *fork*, *reasoning effort*, *context compaction*, and *approval*. Side chats share context with their parent session. `never` is **Never ask**, `on-request` is **On request**, and file sandbox modes are **Read-only**, **Workspace write**, and **Full access**. Model names, effort levels, Fast, and Ultra retain their native labels.

## Layout and composer

Main and side composers display the native Goal above the input. The primary action stops a running turn, pauses an idle active goal, or resumes an inactive goal. The action menu contains editing, clearing and, during a running turn, pausing/resuming the goal. Stop pauses the goal before interrupting its turn. An unfinished goal uses Goal controls in place of ordinary turn recovery controls. `/goal`, `/goal <objective>`, `/goal pause|resume|stop|clear` use the same operations. Native notifications and ordered snapshots synchronize clients; each side conversation owns its goal.

In narrow panes, the Goal shares the input's border and shows its status, a two-line objective and one primary action. Expanding reveals the full objective, token usage/budget and elapsed time. Goal settings keep the budget and status in two aligned columns with equal control heights on desktop and mobile.

Enter submits message composers, Codex answers, tool responses and inline message edits; Shift+Enter inserts a newline. Ctrl/Cmd+Enter also submits. IME confirmation preserves the draft. Single-line form fields retain native Enter submission.

The Codex-style application uses a neutral sidebar, flat session list, expandable tools, a bottom composer, and a task drawer. The composer exposes keyboard-navigable model/effort menus with official English effort labels, model-supported speed tiers and an Ultra toggle, editable permissions, context usage, and slash commands. Ultra sends native `effort:ultra`, displays disabled Auto effort, and restores the browser's saved ordinary effort when switched off. Plan mode is available through `/plan`. Narrow layouts place model/effort and labeled controls on separate rows. Turns display native duration and a live running timer. Subagents stay out of session lists, including live updates and search. Subagent activities and collaboration calls follow CLI status labels, wait results, and navigable agent paths. Native events update messages, reasoning summaries, tool progress and activity; selected threads bootstrap from ordered snapshots and continue through SSE events, preserving drafts, scroll position and open tool records. Native turn summaries merge by item ID; full snapshots replace the loaded history range. Background command completion events refresh the task panel. No external asset/API dependency is required to render the UI. Demo is explicitly labeled.

Inline HTML visualizations run in opaque-origin sandboxed iframes. Nexus maintains the Apache-2.0 template and `viz-calendar` implementation in `static/vendor/visualize-template.html` and `visualize-calendar.js`; the template provides semantic styles, tabs, tooltips and carousels. Calendar events support overlapping layouts, reactive attributes and `eventselect`. Lucide 1.17.0 is bundled locally with its ISC/MIT notices. Widget state persists per message and file; theme changes and streamed replies preserve live frames.

Composer text drafts persist per machine/thread in browser `localStorage` on every input. Selecting a conversation restores its draft across reloads and app restarts. Clearing the composer or successfully sending its text removes the draft; text entered while submission is pending remains available for the next message.

The session header shows its six-character ID beneath the title; clicking it copies those six characters and shows the session-ID copied toast. The ID is the lowercase suffix of the native thread ID. The More menu copies the full ID.

Developer input opens a side panel with three stacked selection rows: base model instructions, runtime developer instructions, and custom developer instructions. Selecting a row shows its full content below; one section is visible at a time. Up/Down and Home/End navigate the rows. Each section retains its scroll position while switching, and refreshes preserve the selection and existing content. Custom instructions include the session's Nexus Tool call rules and configured developer instructions. Copy actions retain complete instruction text and tool definitions.

At widths up to 720px, the session header shows Back, the single-line session title and ID, Side Chat, and More; icon buttons have 44px targets. Background tasks, project terminal, and group membership actions appear in More. The compaction count stays out of the mobile header; the context-usage popover includes the completed compaction count. Desktop keeps its header shortcuts.

Submissions appear immediately in main and side conversations, reconcile by `clientUserMessageId` against events and snapshots, and retain failed messages with a retry action.

Attachments upload original files before sending. The composer has separate file and image pickers. Drops default to file mode; Shift-dropping images and pasting screenshots use image mode. History displays image previews and file download links. Input encoding is defined in the [message API](api-contract.md#other-endpoints).

Incoming agent and group deliveries align left with the group chat's sender name and Identicon, retaining the user-bubble styling. Group names appear beside the sender; CC recipients appear as separate avatar chips below the body. Prompt headers stay in model context; the transcript, copy action and editor show only the body. Editing and resending preserve delivery metadata and restore the prompt header. Historical Chinese and English headers use the same presentation. Direct user messages align right.

## Projects and session lists

The sidebar groups registered projects by machine and exact working directory, sorts each machine's projects by latest session time, and retains machine collapse choices in browser storage. Connection dots are green when connected and red otherwise. Each machine's add button opens a path input and directory browser with parent navigation and breadcrumbs.

Project rows expose an `×` removal button on hover or keyboard focus; touch devices show it continuously with a 44px target. Removal offers a ten-second Undo toast. Machine updates synchronize tracking across clients, close details from removed projects while preserving composer drafts, and return an affected project view to the main session list.

All session indexes are scoped to registered projects. The default Recent view merges loaded sessions with paginated archived and unarchived history, sorts by conversation activity, and initially displays ten sessions. Scrolling loads ten more. Background refreshes leave the pagination footer unchanged; loading more waits for any current refresh. Working excludes sessions waiting for input or approval; Waiting includes both. Project lists group unarchived and archived sessions, with only unarchived expanded initially. Group choices survive refreshes. Archive and restore actions are available on list rows and in the detail menu; reading archived sessions leaves them archived, and sending a message restores them. The project list header's add button directly creates and opens an empty conversation, sending only its working directory and retaining remote model, effort, and permission defaults.

Displayed dates use native `recencyAt`, falling back to `updatedAt` and `createdAt`. Search refresh requests `POST /api/search/sync`.

Adding a missing project directory offers a neutral creation confirmation. Canceling leaves the filesystem and project list unchanged.

## Account limits

The sidebar's Account limits entry opens accounts grouped by user and workspace, with associated machine chips. Pro badges map native `prolite`, `pro`, and `promax` to `Pro 10x`, `Pro 20x`, and `Pro 50x`, preferring the latest usage snapshot's plan over the login identity. Each quota bucket shows remaining five-hour and weekly percentages and local reset times, matched by native window duration. Missing values display `—`. Credits show the rechargeable point balance with locale separators and up to two decimal places. Reset credits show the available count and expiration dates, grouped by date. A zero count hides expiration rows. Non-OAuth and signed-out machines appear as separate authentication rows.

The dialog refreshes on opening, every minute while visible, and after account or connection events. Manual refresh bypasses the server cache. Refreshes retain existing content and errors until results arrive; unchanged cards keep their DOM nodes. Failed reads preserve the last snapshot and show its timestamp. The dialog height stays fixed during loading; cards stack on phones.

## Side conversations

`/side <prompt>` and `/btw <prompt>` create a saved side conversation in the rightmost pane. Bare commands reopen the selected side chat, or create one when none exists. Tabs switch between saved side chats; closing the pane preserves them. The delete button removes the selected side chat and selects its neighbor, or closes the pane when none remain. Deletion events synchronize other clients and clear the deleted draft. A collapsible, read-only shared-context section stays above the side transcript, with a separator marking the start of side-only content. Initial slash-command messages appear immediately on submission and stay visible through side creation, delivery and snapshot loading; the loading screen gives way to the pending message. Delivery keeps its target across session changes or pane closure; failed sends preserve the source draft and attachments. Each pane owns its draft, snapshot/event cursor, scroll and execution state. Desktop panes resize; narrow screens show the side pane over the main conversation. Refresh and reconnection restore the selected side chat.

## Transcript and message actions

Each command, MCP, dynamic or function tool occupies one collapsible row. Expanding it shows adjacent call and result cards, with arguments or commands in the call card and output, errors and exit status in the result card. Updates preserve expansion and each card's scroll position.

Context compaction is a standalone status row with the same 25px spacing as replies and tool groups in main and side transcripts. It displays `正在压缩 · 12s` with a spinner and a live elapsed timer, then `压缩完成 · 1m 20s` with the final duration. Timing uses the item's native `startedAtMs` and `completedAtMs`; records without timing show the status alone. Timer ticks update text in place. Main and side activity labels follow the active compaction. Snapshots and reconnection restore its state and timing; the header count includes completed compactions.

Settings tabs are General, Features and About. Features contains the Tool call ID switch, off by default. A conversation captures this capability at creation. ID-enabled main and side composers show a Tool call compact toggle with a persistent enabled state. Each compacted source occupies one tool row identifying its call and source (`rN`), with status and duration inline. The status distinguishes compacted, kept original, failed fallback and token-growth fallback. Expanding it shows the call and original return as compaction starts, adding a projected return only when compaction changes the retained content. Keep and fallback outcomes show the original once. Snapshots restore the stored outcome and results. Tool-call compactions stay inside consecutive “运行了命令” groups and do not increment the context-compaction count.

User messages expose full-text copying and inline editing with cancel and resend actions. Escape cancels editing. Completed assistant messages expose icon-only copy, fork and regenerate actions, including during an active turn. Every assistant message owns a bottom-right toolbar and uses the same visibility rules. A separate footer below the messages contains only turn statistics, outcome and a `本轮操作` menu for copying all replies or regenerating from the turn start. Whitespace and a subtle divider separate these regions. On hover-capable devices, message actions appear while hovered or keyboard-focused; pointer clicks never pin them open. On touch devices, actions toggle on a short tap or keyboard Enter/Space without changing message spacing. Long presses, text selection, selection dismissal and scrolling preserve their visibility. Pointer gestures starting with selected text preserve the controls even when the browser clears the selection before the click event. Snapshot/stream updates preserve the selected message’s open controls. Fork includes the selected message and immediately opens the new conversation; side forks select the new sibling tab under the same main conversation; regeneration replaces that message and subsequent history, preserving the preceding same-turn context. Regeneration is disabled while sending or editing. Turns without assistant messages offer a turn-end fork in that menu. Empty compaction-only turns disable reply copying and regeneration. Editing and regeneration preserve the ordinary composer draft. Failed user-message replacement and whole-turn regeneration with user input retain an editor for retry. The turn menu closes after a choice, outside click or Escape and remains open across footer updates. Main and side conversations share these controls. `thread/reverted` replaces the history view and invalidates pending history reads and pagination.

Completed-turn footers use 16px action icons and 14px metadata, including the native `completedAt` formatted as `YYYY/MM/DD HH:mm` in the browser's local timezone. Consecutive tool calls and reasoning summaries form a collapsible “运行了命令” section once the segment ends and all its items finish. Running segments stay visible. Empty records do not split groups; message boundaries do. Groups start collapsed, preserve their expanded state through streaming and snapshot updates, and retain each item's detail toggle. Status dots appear only on individual tool rows and reflect each call's own status. These rules also apply to side conversations and their shared context.

## Context usage and TPS

The context-usage sidebar shows a used-token donut and category percentages; estimated requests are labelled in its title. Unused capacity and diagnostic ledgers remain in the API and JSON export.

TPS sampling and display are enabled only when `settings.summary` is `"none"`. Turns containing reasoning summaries also hide saved TPS. With TPS enabled, the running indicator includes a TPS field. While assistant text streams, it shows estimated output TPS over a rolling three-second window, using UTF-8 bytes / 4 as the token estimate. The first chunk establishes the sampling baseline; live sampling starts after 500 ms and falls to zero during output pauses. Between replies, the indicator shows the current turn's measured average; before any output it shows `— TPS`. Completed-turn footers show the same average beside elapsed time. Averages divide sampled assistant tokens by their combined message durations, excluding tool time; unobserved replies are skipped. Native measurements replace estimates; `≈` marks estimates when completion telemetry is absent. Browser snapshots retain ongoing samples and completion metrics.

## Terminals and files

The UI lists terminals from every machine together. Terminal numbers are unique across machines; creation uses the lowest available positive number, and existing terminals keep their numbers. Project and folder actions create terminals in their directory; a session header reuses its bound terminal, and terminal-page machine buttons create home-directory shells. Sessions retain their conversation navigation. Terminal views use the main content pane and offer a return action for bound conversations.

Terminal views render buffered output through the `ready.end` offset before fitting to the current viewport.

The file browser exposes renaming through right-click or touch long-press, preserves an open editor's draft under the new path, updates preview links and navigation history, and reloads the listing.

Each machine retains a navigation history of directories and open files. Local file links, including links across directories, add one entry. Back and forward restore the directory, file, source/preview mode and scroll positions. Refreshing, switching source/preview mode and reopening the current location preserve the history; navigating from an earlier entry replaces its forward branch. The parent action opens the parent directory. On mobile, opening a file from a session makes the preview's return button restore that session, including its draft and reading position; further file links retain this return target. Opening files through the Files navigation makes the button return to the current directory's file list. Directory and file content remain visible during navigation until the destination is ready. History traversal prunes entries whose directories return `directory_not_found` and retains entries on other errors.

The text editor preserves the UTF-8 BOM and CRLF/CR line endings, retains drafts on errors and while switching to sessions, and confirms before discarding unsaved changes.

File previews reload when reopened or refreshed; active editor drafts are retained. Images are fetched and decoded before replacing the current preview.

## Group chat

The Group Chat client places selection and group management in the middle pane and the public transcript in the main pane. Group rows show the latest message with single-line ellipsis. The list’s top selector offers activity time first (default) and creation time, both newest first; activity ties use newest creation order. Subagents, side conversations and archived sessions are excluded from new group memberships. The member picker has two columns. The left column retains original members and every session selected during this opening, including later deselections, with no filters. Existing row positions stay fixed; newly selected sessions append at the bottom, and saving preserves this order. The right column shows remaining eligible candidates, offers project and title/ID filters, sorts waiting sessions before working sessions and then by recent activity, and ignores the project filter for ID suffix searches. The thread header’s group button manages the current session’s memberships. Agent names are botIds with deterministic mirrored 5×5 SVG Identicons. User bubbles align right. The client retains per-group drafts, reconciles optimistic submissions by message ID, merges delivery revisions, paginates history and restores messages after SSE resync. Native replies enter the public transcript through explicit group sends.

## Loading and event recovery

Session results render as each machine responds. Browser requests time out after 30 seconds, or 60 seconds for connection actions; the deadline includes reading the response body. SSE starts before session lists finish loading.

Browser SSE reconnects automatically and reloads current state/history while preserving the composer. History pages arrive newest first; the client reverses them for display and supports loading older pages.

On resync, connection recovery, or return to the page, the browser replaces its loaded history range with a fresh snapshot and renews its pagination cursor. Pending reads from the previous history generation are discarded; turns arriving during the reload are retained.

`thread/deleted` removes the conversation from cached lists and closes its detail view. Loaded pages are retained; list and history reads started before deletion are discarded.

## PWA and notifications

`/manifest.webmanifest`, root-scoped `/sw.js`, and PNG/touch icons support PWA installation over HTTPS. The service worker caches the interface and serves `/offline.html` on failed navigation; API and SSE traffic bypass its cache. Loaded pages show a full-screen offline dialog on browser offline events or SSE failures, preserving drafts and page state. A `/api/config` probe every 10 seconds with a 5-second timeout detects unresponsive connections and restarts SSE after recovery. The offline navigation page also retries every 10 seconds. Installation help is in appearance settings.

The notification test waits up to 15 seconds for a `nexus/push-test` receipt before reporting a timeout. System notification display depends on OS notification and do-not-disturb settings.

## Frontend modules

Each browser pane uses `conversation-controller.js` for snapshot loading, ordered event replay, history pagination and request invalidation. `conversation-actions.js` shares submission state, drafts, attachments, settings, interruption, resume, history editing, regeneration and request responses; each operation retains its original selection across navigation. Panes retain navigation and provide rendering callbacks. `conversation-state.js` merges turns and applies native events; `conversation.js` preserves unchanged message DOM nodes during streaming.

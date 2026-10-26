# Native macOS accessibility backend: probe notes

Recorded on a MacBook (Darwin 25.6) in October 2026 against Google Chrome and Dia.

## Permission
- `AXIsProcessTrusted()` is answered for the *responsible* app (the terminal / host app that started
  jevd), not for Python. Grant it in System Settings > Privacy & Security > Accessibility.
  `voyager doctor` checks it; without trust jevd does not start.
- Screen Recording is answered for the responsible app too (`CGPreflightScreenCaptureAccess()`).
  Without it, ScreenCaptureKit fails in ~50 ms with `SCStreamErrorDomain -3801` and
  `screencapture -l` with "could not create image from window"; observations then carry no image.

## Listing and capture (`macwin.py`)
- `NSWorkspace.runningApplications` only updates on the main run loop, which jevd does not run: an
  app launched after start never appeared, so binding a freshly launched app failed. Live lookups:
  `NSRunningApplication.runningApplicationWithProcessIdentifier_` (None once the app quits),
  `runningApplicationsWithBundleIdentifier_`, and window owners from `CGWindowListCopyWindowInfo`.
- Window titles come from AX (`kCGWindowName` needs Screen Recording). There is no public API from an
  AX window to its CG window number; frames match exactly (both global points, top-left origin).
  `list_targets`: 124 targets in ~360 ms on this Mac. Bind of a not-yet-running Calculator: 0.78 s.
- Capture: ScreenCaptureKit `SCScreenshotManager` with a desktop-independent window filter at the
  filter's `pointPixelScale` (2x here), JPEG via ImageIO;
  `screencapture -x -o -l<id>` as the fallback. Calculator (230x408 pt): 460x816 JPEG, ~44 KB,
  343 ms cold then 104-151 ms. An AX element center mapped through the window frame lands on
  the same element in the image, so screenshot pixels map to screen points by the backing scale.
- On macOS 26.6.2 (2026-10-26) `SCShareableContent` still answered in ~50 ms, but
  `SCScreenshotManager.captureImageWithFilter` never called its completion handler, with or without
  the main run loop serviced, while `screencapture -l` took ~150-180 ms. Every capture was paying the
  old 5 s timeout before falling back (about 175 s across one 11-task benchmark run). The capture now
  gives ScreenCaptureKit 1.5 s once and, after a timeout, uses `screencapture` for the rest of the
  daemon's life.

## Reads
| page | native, action scope | native, full |
|---|---|---|
| Hacker News thread (10k AX nodes) | 184-285 ms | 1,700 ms |
| local test page (~300 nodes) | 50-90 ms | — |

- Chromium/Electron build their web tree only after `AXManualAccessibility` / `AXEnhancedUserInterface`
  is set on the app element; the first read after enabling can show an empty web area. `cu_open`
  waits until the web content has elements and has settled.
- Chromium clips the frame of an element scrolled out of its scroll container to a 0-1 px sliver at
  the container's edge (and off-page elements to the window edge). Frames therefore do not say how
  far away an element is; the action-scope read only drops elements whose frame is non-empty and
  outside the window ± margin, so it is a superset, never a subset, of what is on screen.
- `AXUIElementCopyMultipleAttributeValues` is ~0.17 ms per node on Chrome; one call per node.

## Actions (window in the background, another app frontmost)
| action | Chrome | Dia |
|---|---|---|
| `AXPress` | works, effect visible in < 50 ms | **not advertised; returns 0 and does nothing** |
| `AXFocused = true` on inputs and contenteditable | works | works |
| `AXValue` set (input, plain contenteditable) | works | works |
| `AXSelectedText` set | returns 0, no effect | — |
| key events via `CGEventPostToPid` (Tab, arrows) | delivered | delivered |
| text via `CGEventPostToPid` (unicode strings, Cmd+V) | **ignored while inactive** | ignored |
| mouse / scroll-wheel events via `CGEventPostToPid` | ignored | ignored |
| `AXScrollToVisible` | works (used for paging) | works |

So: press only when `AXPress` is in the action names; text insertion and pointer clicks activate the
app first. `NSRunningApplication.activateWithOptions` is ignored from a background process (macOS
cooperative activation); `open -a <bundle path>` activates in ~80-250 ms.

- `NSRunningApplication.isActive` and `NSWorkspace.frontmostApplication` are stale off the main run
  loop (the backend's worker thread); the app element's `AXFrontmost` attribute is live. The
  system-wide `AXFocusedApplication` returned -25204 here.
- Pointer clicks are guarded: the element under the point (`AXUIElementCopyElementAtPosition`, walked up
  through `AXParent`) must be the grounded element or inside it, else the click is refused.
- Measured per step through the pipeline (Chrome, Jev eyes): click 0.41 s, type 0.55 s, find (item
  out of view in a list) 0.94 s. A 7-step plan from `cu_open` to `ready_to_commit`: 3.5 s.
